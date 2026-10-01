import unittest

from daily_coolpapers import db, memo_db, memo_drafts, memos
from tests import test_memo_foundation as foundation


class MemoDraftTests(unittest.TestCase):
    """Focused draft contract checks; the existing memo suite is not inherited."""

    setUp = foundation.MemoFoundationTests.setUp
    paper = foundation.MemoFoundationTests.paper
    evaluate = foundation.MemoFoundationTests.evaluate
    favorite = foundation.MemoFoundationTests.favorite
    state = foundation.MemoFoundationTests.state

    def context(self, **changes):
        value = {
            "command": {
                "title": "Draft memo",
                "source_mode": "manual",
                "source_id": None,
                "prompt_id": None,
                "profile_id": self.profile_id,
                "series_id": None,
                "previous_version_id": None,
                "copy_judgment": False,
            },
            "filters": {},
            "page_size": 30,
            "page": 1,
        }
        value.update(changes)
        return value

    def create_draft(self, owner="owner-a", token="draft-token-a", *, ids, shown=None, selected=None, orders=None):
        shown = ids if shown is None else shown
        selected = ids if selected is None else selected
        return memo_drafts.save(
            owner, token, 0, self.context(), shown, selected,
            orders or {paper_id: index + 1 for index, paper_id in enumerate(selected)},
            initial_ids=ids,
        )

    def test_get_is_read_only_and_returns_only_visible_page_orders(self):
        first, second = self.favorite(), self.favorite(2)
        self.create_draft(ids=[first], shown=[first, second], selected=[first], orders={first: 1})
        with db.connect() as conn:
            before = conn.total_changes
            data = memo_drafts.read(conn, "draft-token-a", "owner-a", page_ids=[second])
            after = conn.total_changes
        self.assertEqual(before, after)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["selected_count"], 1)
        self.assertEqual(data["selected_orders"], {})
        self.assertNotIn("paper_ids", data)

    def test_owner_version_and_expiry_conflicts_are_explicit(self):
        paper = self.favorite()
        self.create_draft(ids=[paper])
        with db.connect() as conn:
            with self.assertRaises(memo_db.MemoConflictError):
                memo_drafts.read(conn, "draft-token-a", "other-owner", page_ids=[paper])
        with self.assertRaises(memo_db.MemoConflictError):
            memo_drafts.save("owner-a", "draft-token-a", 2, self.context(), [paper], [paper], {paper: 1})
        with db.connect() as conn:
            conn.execute("UPDATE memo_drafts SET updated_at=? WHERE token=?", ("2020-01-01 00:00:00", "draft-token-a"))
        with db.connect() as conn:
            with self.assertRaises(memo_db.MemoConflictError):
                memo_drafts.read(conn, "draft-token-a", "owner-a", page_ids=[paper])
        self.assertEqual(memo_drafts.cleanup_expired(), 1)

    def test_three_pages_merge_cancel_and_global_duplicate_order(self):
        first, second, third = self.favorite(), self.favorite(2), self.favorite(3)
        self.create_draft(ids=[first], shown=[first], selected=[first], orders={first: 1})
        saved = memo_drafts.save(
            "owner-a", "draft-token-a", 1, self.context(), [first, second], [first, second],
            {first: 1, second: 2},
        )
        saved = memo_drafts.save(
            "owner-a", "draft-token-a", saved["version"], self.context(page=2), [third], [third], {third: 3},
        )
        saved = memo_drafts.save(
            "owner-a", "draft-token-a", saved["version"], self.context(page=3), [second], [], {},
        )
        with db.connect() as conn:
            data = memo_drafts.read(conn, "draft-token-a", "owner-a", page_ids=[first, third], include_ids=True)
        self.assertEqual(data["paper_ids"], [first, third])
        self.assertEqual(data["selected_orders"], {first: 1, third: 2})
        with self.assertRaises(memo_db.MemoConflictError):
            memo_drafts.save(
                "owner-a", "draft-token-a", saved["version"], self.context(page=3),
                [third], [third], {third: 1},
            )

    def test_clear_and_apply_source_replace_the_ordered_set(self):
        first, second, third = self.favorite(), self.favorite(2), self.favorite(3)
        self.create_draft(ids=[first, second])
        cleared = memo_drafts.save(
            "owner-a", "draft-token-a", 1, self.context(), [first], [], {}, action="clear",
        )
        with db.connect() as conn:
            self.assertEqual(memo_drafts.read(conn, "draft-token-a", "owner-a", include_ids=True)["paper_ids"], [])
        applied = memo_drafts.save(
            "owner-a", "draft-token-a", cleared["version"], self.context(), [], [], {},
            initial_ids=[second, third], action="apply_source",
        )
        self.assertEqual(applied["version"], 3)
        with db.connect() as conn:
            self.assertEqual(memo_drafts.read(conn, "draft-token-a", "owner-a", include_ids=True)["paper_ids"], [second, third])

    def test_preview_guard_rejects_draft_change_without_writing(self):
        paper = self.favorite()
        self.create_draft(ids=[paper])
        request = memos.command("owner-a", "draft-token-a", 1, check_version=True)
        before = self.state()
        preview = memos.preview_memo(request)
        self.assertEqual(preview["snapshot"]["papers"][0]["paper"]["id"], paper)
        self.assertEqual(self.state(), before)
        memo_drafts.save("owner-a", "draft-token-a", 1, self.context(page=2), [paper], [paper], {paper: 1})
        with self.assertRaises(memo_db.MemoConflictError):
            memos.preview_memo(request)
        self.assertEqual(self.state(), before)

    def test_confirm_guard_is_atomic_when_draft_changes(self):
        paper = self.favorite()
        self.create_draft(ids=[paper])
        request = memos.command("owner-a", "draft-token-a", 1, idempotency_key="draft-atomic-key-01")
        memo_drafts.save("owner-a", "draft-token-a", 1, self.context(page=2), [paper], [paper], {paper: 1})
        before = self.state()
        with self.assertRaises(memo_db.MemoConflictError):
            memos.create_memo_version(request)
        self.assertEqual(self.state(), before)

    def test_confirm_duplicate_key_returns_existing_after_draft_clear(self):
        paper = self.favorite()
        self.create_draft(ids=[paper])
        key = "draft-idempotency-key-01"
        created = memos.create_memo_version(memos.command("owner-a", "draft-token-a", 1, idempotency_key=key))
        cleared = memo_drafts.save("owner-a", "draft-token-a", 1, self.context(), [paper], [], {}, action="clear")
        repeated = memos.command("owner-a", "draft-token-a", cleared["version"], idempotency_key=key, check_version=False)
        result = memos.create_memo_version(repeated)
        self.assertEqual(result["id"], created["id"])
        self.assertFalse(result["created"])


if __name__ == "__main__":
    unittest.main()
