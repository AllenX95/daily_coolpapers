from __future__ import annotations

from html.parser import HTMLParser
import json
from urllib.parse import parse_qs, urlencode, urlsplit
import unittest
from unittest.mock import patch

from daily_coolpapers import db, memo_candidates, memo_db, memo_drafts, memos
from tests import test_memo_foundation as foundation
from werkzeug.datastructures import MultiDict


class _FormParser(HTMLParser):
    """Small HTML form reader used to exercise the no-JavaScript contract."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._form = None
        self._select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._form = {"attrs": attrs, "controls": [], "buttons": []}
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input":
            self._form["controls"].append({"tag": tag, **attrs})
        elif tag == "select":
            self._select = {"tag": tag, **attrs, "options": []}
            self._form["controls"].append(self._select)
        elif tag == "option" and self._select is not None:
            self._select["options"].append({"attrs": attrs, "text": ""})
        elif tag == "button":
            self._form["buttons"].append(attrs)

    def handle_data(self, data):
        if self._select is not None and self._select["options"]:
            self._select["options"][-1]["text"] += data

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "form":
            self._form = None


def _forms(html):
    parser = _FormParser()
    parser.feed(html)
    return parser.forms


def _control_values(form, *, selected_ids=(), orders=None, action=None, target_page=None, overrides=None):
    """Return a Flask test-client payload from the rendered selection form."""

    selected_ids = {str(value) for value in selected_ids}
    orders = {str(key): str(value) for key, value in (orders or {}).items()}
    overrides = {str(key): value for key, value in (overrides or {}).items()}
    result = []
    for control in form["controls"]:
        name = control.get("name")
        if not name:
            continue
        tag = control["tag"]
        if tag == "select":
            options = control.get("options") or []
            selected = next((item for item in options if "selected" in item["attrs"]), None)
            if selected is None and options:
                selected = options[0]
            value = (selected or {}).get("attrs", {}).get("value")
            if value is None and selected is not None:
                value = selected.get("text", "").strip()
            result.append((name, overrides.get(name, value or "")))
            continue
        input_type = (control.get("type") or "text").lower()
        if input_type in {"submit", "button", "reset"}:
            continue
        if input_type == "checkbox" and name != "paper_ids" and "checked" not in control:
            continue
        value = control.get("value", "")
        if name == "paper_ids":
            if str(value) not in selected_ids:
                continue
        elif name.startswith("order_"):
            paper_id = name[len("order_"):]
            if paper_id not in selected_ids:
                continue
            value = orders.get(paper_id, value)
        if name in overrides:
            value = overrides[name]
        result.append((name, value))
    if action is not None:
        result = [(name, value) for name, value in result if name != "action"]
        result.append(("action", action))
    if target_page is not None:
        result = [(name, value) for name, value in result if name != "target_page"]
        result.append(("target_page", str(target_page)))
    return MultiDict(result)


class MemoSelectionRouteTests(unittest.TestCase):
    """Route-only acceptance tests; foundation helpers are imported by alias."""

    setUp = foundation.MemoFoundationTests.setUp
    paper = foundation.MemoFoundationTests.paper
    evaluate = foundation.MemoFoundationTests.evaluate
    favorite = foundation.MemoFoundationTests.favorite
    command = foundation.MemoFoundationTests.command

    def _selection_form(self, html):
        forms = _forms(html)
        for form in forms:
            action = form["attrs"].get("action", "")
            if action.endswith("/investment-memos/select"):
                return form
        self.fail("rendered memo page did not contain the selection form")

    def _preview_form(self, html):
        forms = _forms(html)
        for form in forms:
            action = form["attrs"].get("action", "")
            if action.endswith("/investment-memos"):
                return form
        self.fail("rendered preview did not contain the confirmation form")

    def _draft_token(self, location):
        values = parse_qs(urlsplit(location).query)
        self.assertEqual(len(values.get("draft_token", [])), 1)
        return values["draft_token"][0]

    def _owner(self):
        with self.client.session_transaction() as session:
            return session["_memo_draft_owner"]

    def _paper_ids(self, form):
        return [
            int(control["value"])
            for control in form["controls"]
            if control.get("name") == "paper_ids"
        ]

    def _draft_ids(self, token, *, owner=None):
        with db.connect() as conn:
            return memo_drafts.read(
                conn, token, owner or self._owner(), include_ids=True
            )["paper_ids"]

    def _post_page(self, form, *, selected_ids=(), orders=None, action="save", target_page=None, overrides=None):
        return self.client.post(
            "/investment-memos/select",
            data=_control_values(
                form,
                selected_ids=selected_ids,
                orders=orders,
                action=action,
                target_page=target_page,
                overrides=overrides,
            ),
        )

    def _get_following(self, response):
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return self.client.get(response.location)

    def test_html_form_merges_three_pages_cancel_return_filter_and_empty_title(self):
        ids = [self.favorite(number) for number in range(1, 7)]
        # A first GET is a read-only, bounded candidate read.  It must not
        # hydrate snapshots or ask for the global selection list.
        with patch.object(memo_db, "paper_snapshots", side_effect=AssertionError("snapshot hydrate on GET")), \
             patch.object(memo_candidates, "selection_ids", side_effect=AssertionError("global IDs on GET")):
            response = self.client.get("/investment-memos/new?page_size=2&sort=favorite_asc")
        self.assertEqual(response.status_code, 200)
        page1 = response.get_data(as_text=True)
        form1 = self._selection_form(page1)
        page1_ids = self._paper_ids(form1)
        self.assertEqual(len(page1_ids), 2)
        first_id = page1_ids[0]
        page2_redirect = self._post_page(
            form1, selected_ids=[first_id], orders={first_id: 1}, action="page", target_page=2,
        )
        page2_response = self._get_following(page2_redirect)
        token = self._draft_token(page2_redirect.location)
        form2 = self._selection_form(page2_response.get_data(as_text=True))
        page2_ids = self._paper_ids(form2)
        self.assertEqual(len(page2_ids), 2)
        second_id = page2_ids[0]
        page3_response = self._get_following(
            self._post_page(form2, selected_ids=[second_id], orders={second_id: 2}, action="page", target_page=3)
        )
        form3 = self._selection_form(page3_response.get_data(as_text=True))
        page3_ids = self._paper_ids(form3)
        self.assertEqual(len(page3_ids), 2)
        third_id = page3_ids[0]
        page2_again = self._get_following(
            self._post_page(form3, selected_ids=[third_id], orders={third_id: 3}, action="page", target_page=2)
        )
        form2_again = self._selection_form(page2_again.get_data(as_text=True))
        checked_again = {
            str(control.get("value"))
            for control in form2_again["controls"]
            if control.get("name") == "paper_ids" and "checked" in control
        }
        self.assertIn(str(second_id), checked_again)

        # Cancel the page-2 choice, return to page 1, then apply a literal
        # title filter and clear it again.  The title is intentionally empty
        # throughout, so pagination remains available before preview.
        page1_again = self._get_following(
            self._post_page(form2_again, selected_ids=[], action="save")
        )
        form1_again = self._selection_form(page1_again.get_data(as_text=True))
        page1_filtered = self._get_following(
            self._post_page(
                form1_again,
                selected_ids=[first_id],
                orders={first_id: 1},
                action="filter",
                overrides={"query": f"Library Paper {first_id}"},
            )
        )
        self.assertIn(f"Library Paper {first_id}", page1_filtered.get_data(as_text=True))
        self.assertNotIn(f"Library Paper {second_id}", page1_filtered.get_data(as_text=True))
        form_filtered = self._selection_form(page1_filtered.get_data(as_text=True))
        cleared_filter = self._get_following(
            self._post_page(
                form_filtered,
                selected_ids=[first_id],
                orders={first_id: 1},
                action="filter",
                overrides={"query": ""},
            )
        )
        self.assertEqual(cleared_filter.status_code, 200)
        self.assertEqual(self._draft_ids(token), [first_id, third_id])

    def test_source_preselection_is_global_clear_stays_empty_and_apply_source_is_explicit(self):
        ids = [self.favorite(number) for number in range(1, 4)]
        direction = db.create_attention_direction("Route source", "route source scope")
        now = db.now_iso()
        with db.connect() as conn:
            for paper_id in ids:
                conn.execute(
                    "INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,manual_decision,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (paper_id, direction, "matched", None, now, now),
                )
        response = self.client.get(
            f"/investment-memos/new?source_mode=attention_direction&source_direction_id={direction}&page_size=2&sort=favorite_asc"
        )
        self.assertEqual(response.status_code, 200)
        form = self._selection_form(response.get_data(as_text=True))
        visible_ids = self._paper_ids(form)
        checked = [
            int(control["value"])
            for control in form["controls"]
            if control.get("name") == "paper_ids" and "checked" in control
        ]
        self.assertEqual(checked, visible_ids)
        self.assertEqual(len(visible_ids), 2)
        first_redirect = self._post_page(
            form,
            selected_ids=checked,
            orders={paper_id: index + 1 for index, paper_id in enumerate(checked)},
            action="page",
            target_page=2,
        )
        first_page = self._get_following(first_redirect)
        token = self._draft_token(first_redirect.location)
        self.assertEqual(self._draft_ids(token), ids)

        clear_response = self._get_following(
            self._post_page(self._selection_form(first_page.get_data(as_text=True)), action="clear")
        )
        self.assertEqual(self._draft_ids(token), [])
        clear_form = self._selection_form(clear_response.get_data(as_text=True))
        self.assertEqual(
            [control for control in clear_form["controls"]
             if control.get("name") == "paper_ids" and "checked" in control],
            [],
        )

        applied = self._get_following(
            self._post_page(self._selection_form(clear_response.get_data(as_text=True)), action="apply_source")
        )
        self.assertEqual(self._draft_ids(token), ids)
        self.assertEqual(applied.status_code, 200)

    def test_preview_contains_complete_draft_and_confirmation_is_idempotent(self):
        ids = [self.favorite(number) for number in range(1, 4)]
        initial = self.client.get("/investment-memos/new?title=Route+preview&page_size=2&sort=favorite_asc")
        form1 = self._selection_form(initial.get_data(as_text=True))
        page1_ids = self._paper_ids(form1)
        self.assertEqual(len(page1_ids), 2)
        page2 = self._get_following(
            self._post_page(
                form1,
                selected_ids=page1_ids,
                orders={paper_id: index + 1 for index, paper_id in enumerate(page1_ids)},
                action="page",
                target_page=2,
            )
        )
        form2 = self._selection_form(page2.get_data(as_text=True))
        page2_ids = self._paper_ids(form2)
        self.assertEqual(len(page2_ids), 1)
        last_id = page2_ids[0]
        with db.connect() as conn:
            jobs_before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            versions_before = conn.execute("SELECT COUNT(*) FROM investment_memo_versions").fetchone()[0]
        preview = self._post_page(form2, selected_ids=[last_id], orders={last_id: 3}, action="preview")
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        preview_html = preview.get_data(as_text=True)
        for paper_id in ids:
            self.assertIn(f"Library Paper {paper_id}", preview_html)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], jobs_before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM investment_memo_versions").fetchone()[0], versions_before)
        confirm_form = self._preview_form(preview_html)
        confirm = _control_values(confirm_form)
        self.assertEqual(
            set(confirm.keys()),
            {"csrf_token", "draft_token", "draft_version", "idempotency_key"},
        )
        first = self.client.post("/investment-memos", data=confirm)
        self.assertEqual(first.status_code, 302, first.get_data(as_text=True))
        second = self.client.post("/investment-memos", data=confirm)
        self.assertEqual(second.status_code, 302, second.get_data(as_text=True))
        self.assertEqual(second.location, first.location)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], jobs_before + 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM investment_memo_versions").fetchone()[0], versions_before + 1)

    def test_selection_post_rejects_csrf_stale_version_other_owner_and_expiry(self):
        paper_id = self.favorite()
        initial = self.client.get("/investment-memos/new?page_size=2&sort=favorite_asc")
        form = self._selection_form(initial.get_data(as_text=True))
        valid = _control_values(form, selected_ids=[paper_id], orders={paper_id: 1}, action="save")
        missing_csrf = MultiDict([(name, value) for name, value in valid.items(multi=True) if name != "csrf_token"])
        self.assertEqual(self.client.post("/investment-memos/select", data=missing_csrf).status_code, 403)
        saved = self.client.post("/investment-memos/select", data=valid)
        self.assertEqual(saved.status_code, 302)
        token = self._draft_token(saved.location)
        current = self.client.get(saved.location)
        current_form = self._selection_form(current.get_data(as_text=True))

        self.assertEqual(self.client.post("/investment-memos/select", data=valid).status_code, 409)
        with self.client.session_transaction() as session:
            owner = session["_memo_draft_owner"]
            session["_memo_draft_owner"] = "other-browser-owner"
        try:
            other = self.client.post(
                "/investment-memos/select",
                data=_control_values(current_form, selected_ids=[paper_id], orders={paper_id: 1}, action="save"),
            )
            self.assertEqual(other.status_code, 409)
        finally:
            with self.client.session_transaction() as session:
                session["_memo_draft_owner"] = owner
        with db.connect() as conn:
            conn.execute("UPDATE memo_drafts SET updated_at=? WHERE token=?", ("2020-01-01 00:00:00", token))
        expired = self.client.post(
            "/investment-memos/select",
            data=_control_values(current_form, selected_ids=[paper_id], orders={paper_id: 1}, action="save"),
        )
        self.assertEqual(expired.status_code, 409)
        self.assertIn("过期", json.loads(expired.get_data(as_text=True))["error"])

    def test_old_version_filter_hides_paper_without_marking_it_ineligible(self):
        first, second = self.favorite(), self.favorite(2)
        created = memos.create_memo_version(
            self.command([first, second], idempotency_key="old-route-version-01")
        )
        base = f"/investment-memos/{created['series_id']}/versions/{created['id']}/new-version"
        filtered = self.client.get(base + "?" + urlencode({"query": "Library Paper 2", "page_size": 1}))
        self.assertEqual(filtered.status_code, 200)
        filtered_html = filtered.get_data(as_text=True)
        self.assertNotIn("Library Paper 1", filtered_html)
        self.assertIn("Library Paper 2", filtered_html)
        self.assertNotIn("当前资格或筛选条件未被预选", filtered_html)
        restored = self.client.get(base + "?page_size=2")
        self.assertEqual(restored.status_code, 200)
        form = self._selection_form(restored.get_data(as_text=True))
        checked = {
            int(control["value"])
            for control in form["controls"]
            if control.get("name") == "paper_ids" and "checked" in control
        }
        self.assertEqual(checked, {first, second})


if __name__ == "__main__":
    unittest.main()
