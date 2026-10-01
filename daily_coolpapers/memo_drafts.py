"""Short-lived, server-side selection drafts for paginated memo forms.

The draft tables deliberately contain only a browser owner, a signed-form
token, a small context object, and an ordered set of paper IDs.  Paper IDs are
not foreign keys: a later preview/confirm must be able to report that a
previously selected paper lost its eligibility instead of silently deleting it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any

from . import db
from .memo_db import MemoConflictError


DRAFT_TTL = timedelta(hours=24)
DRAFT_TABLE = "memo_drafts"
DRAFT_PAPER_TABLE = "memo_draft_papers"


@dataclass(frozen=True)
class DraftGuard:
    """Read-time draft state carried into preview/confirm validation."""

    token: str
    owner: str
    expected_version: int
    context: dict[str, Any]
    paper_ids: tuple[int, ...]


def init_schema(conn: Any) -> None:
    """Create only the new draft tables and their supporting indexes."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memo_drafts (
            token TEXT PRIMARY KEY,
            owner TEXT NOT NULL CHECK(length(owner) > 0),
            version INTEGER NOT NULL CHECK(version >= 1),
            context_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memo_draft_papers (
            token TEXT NOT NULL REFERENCES memo_drafts(token) ON DELETE CASCADE,
            paper_id INTEGER NOT NULL CHECK(paper_id > 0),
            display_order INTEGER NOT NULL CHECK(display_order > 0),
            PRIMARY KEY(token, paper_id),
            UNIQUE(token, display_order)
        );
        CREATE INDEX IF NOT EXISTS idx_memo_drafts_updated
            ON memo_drafts(updated_at);
        CREATE INDEX IF NOT EXISTS idx_memo_draft_papers_order
            ON memo_draft_papers(token, display_order);
        """
    )


def _token(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise MemoConflictError("草稿标识无效，请重新打开选择页面")
    return value


def _owner(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise MemoConflictError("当前浏览器会话无效，请重新打开选择页面")
    return value


def _positive_id(value: Any, field: str = "paper_ids") -> int:
    if isinstance(value, bool):
        raise MemoConflictError(f"{field} 包含无效论文标识")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MemoConflictError(f"{field} 包含无效论文标识") from exc
    if parsed <= 0 or parsed > 2**63 - 1:
        raise MemoConflictError(f"{field} 包含无效论文标识")
    return parsed


def _ids(values: Sequence[Any] | None, field: str) -> list[int]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, bytearray)):
        raise MemoConflictError(f"{field} 格式无效")
    result = [_positive_id(value, field) for value in values]
    if len(result) != len(set(result)):
        raise MemoConflictError(f"{field} 不能重复")
    return result


def _orders(values: Mapping[Any, Any] | None) -> dict[int, int]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise MemoConflictError("论文顺序格式无效")
    result: dict[int, int] = {}
    for raw_id, raw_order in values.items():
        paper_id = _positive_id(raw_id, "orders")
        if paper_id in result:
            raise MemoConflictError("论文顺序不能重复")
        if isinstance(raw_order, bool):
            raise MemoConflictError("论文顺序必须是正整数")
        try:
            order = int(raw_order)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MemoConflictError("论文顺序必须是正整数") from exc
        if order <= 0:
            raise MemoConflictError("论文顺序必须是正整数")
        result[paper_id] = order
    return result


def _context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoConflictError("草稿上下文格式无效，请重新打开选择页面")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 256_000:
            raise MemoConflictError("草稿上下文过大，请重新打开选择页面")
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise MemoConflictError("草稿上下文格式无效，请重新打开选择页面") from exc


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MemoConflictError("草稿时间戳无效，请重新打开选择页面") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _now() -> tuple[str, datetime]:
    text = db.now_iso()
    return text, _timestamp(text)


def _expired(updated_at: str, *, now: datetime | None = None) -> bool:
    current = now or _now()[1]
    return _timestamp(updated_at) <= current - DRAFT_TTL


def _load_draft(
    conn: Any,
    token: str,
    owner: str,
    *,
    version: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM memo_drafts WHERE token = ?", (token,)).fetchone()
    if row is None or row["owner"] != owner:
        raise MemoConflictError("草稿不存在或已绑定其他浏览器会话")
    current = now or _now()[1]
    if _expired(row["updated_at"], now=current):
        raise MemoConflictError("草稿已过期，请重新选择论文")
    if version is not None and int(row["version"]) != version:
        raise MemoConflictError("草稿版本已变化，请刷新后重试")
    try:
        context = json.loads(row["context_json"])
    except (TypeError, ValueError) as exc:
        raise MemoConflictError("草稿上下文已损坏，请重新选择论文") from exc
    if not isinstance(context, dict):
        raise MemoConflictError("草稿上下文已损坏，请重新选择论文")
    return {**dict(row), "context": context}


def _selected_ids(conn: Any, token: str) -> list[int]:
    return [
        int(row["paper_id"])
        for row in conn.execute(
            "SELECT paper_id FROM memo_draft_papers WHERE token = ? ORDER BY display_order",
            (token,),
        )
    ]


def _cleanup_expired_sql(conn: Any, *, now: datetime, exclude_token: str | None = None) -> int:
    cutoff = (now - DRAFT_TTL).replace(microsecond=0).isoformat(sep=" ")
    if exclude_token is None:
        result = conn.execute("DELETE FROM memo_drafts WHERE updated_at <= ?", (cutoff,))
    else:
        result = conn.execute(
            "DELETE FROM memo_drafts WHERE updated_at <= ? AND token <> ?",
            (cutoff, exclude_token),
        )
    return int(result.rowcount)


def read(
    conn: Any,
    token: str,
    owner: str,
    *,
    version: int | None = None,
    page_ids: Sequence[Any] = (),
    include_ids: bool = False,
) -> dict[str, Any]:
    """Read draft metadata and only the selected order for the visible page.

    ``include_ids`` is reserved for preview/confirm callers.  Normal GETs pass
    the current page IDs and therefore never load the whole selected set.
    """

    token = _token(token)
    owner = _owner(owner)
    requested_version = None if version is None else _positive_id(version, "version")
    visible_ids = _ids(page_ids, "page_ids")
    draft = _load_draft(conn, token, owner, version=requested_version)
    selected_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM memo_draft_papers WHERE token = ?", (token,)
        ).fetchone()[0]
    )
    selected_orders: dict[int, int] = {}
    if visible_ids:
        marks = ",".join("?" for _ in visible_ids)
        selected_orders = {
            int(row["paper_id"]): int(row["display_order"])
            for row in conn.execute(
                f"SELECT paper_id, display_order FROM memo_draft_papers "
                f"WHERE token = ? AND paper_id IN ({marks}) ORDER BY display_order",
                [token, *visible_ids],
            )
        }
    result: dict[str, Any] = {
        "token": token,
        "version": int(draft["version"]),
        "context": draft["context"],
        "selected_count": selected_count,
        "selected_orders": selected_orders,
    }
    if include_ids:
        result["paper_ids"] = _selected_ids(conn, token)
    return result


def _apply_page_update(
    current: list[int],
    shown_ids: list[int],
    selected_ids: list[int],
    orders: dict[int, int],
) -> list[int]:
    shown = set(shown_ids)
    selected = set(selected_ids)
    if not selected.issubset(shown):
        raise MemoConflictError("当前页之外的论文不能由当前页表单修改")
    if any(paper_id not in selected for paper_id in orders):
        raise MemoConflictError("未选中的论文不能设置顺序")

    old_positions = {paper_id: index + 1 for index, paper_id in enumerate(current)}
    kept = [paper_id for paper_id in current if paper_id not in shown or paper_id in selected]
    for paper_id in shown_ids:
        if paper_id in selected and paper_id not in old_positions:
            kept.append(paper_id)

    assigned: dict[int, int] = {
        paper_id: old_positions[paper_id]
        for paper_id in kept
        if paper_id in old_positions
    }
    for paper_id, order in orders.items():
        if paper_id not in kept:
            raise MemoConflictError("顺序中包含当前未选论文")
        assigned[paper_id] = order
    used: dict[int, int] = {}
    for paper_id, order in assigned.items():
        previous = used.get(order)
        if previous is not None and previous != paper_id:
            raise MemoConflictError("已选论文的顺序数字不能重复（包括其他页面）")
        used[order] = paper_id

    next_order = max(assigned.values(), default=0) + 1
    for paper_id in kept:
        if paper_id not in assigned:
            assigned[paper_id] = next_order
            next_order += 1
    position = {paper_id: index for index, paper_id in enumerate(kept)}
    ordered = sorted(kept, key=lambda paper_id: (assigned[paper_id], position[paper_id]))
    return ordered


def _write_selected(conn: Any, token: str, paper_ids: Sequence[int]) -> None:
    conn.execute("DELETE FROM memo_draft_papers WHERE token = ?", (token,))
    conn.executemany(
        "INSERT INTO memo_draft_papers(token, paper_id, display_order) VALUES (?, ?, ?)",
        [(token, paper_id, index) for index, paper_id in enumerate(paper_ids, 1)],
    )


def save(
    owner: str,
    token: str,
    expected_version: int,
    context: Mapping[str, Any],
    shown_ids: Sequence[Any],
    selected_ids: Sequence[Any],
    orders: Mapping[Any, Any] | None,
    *,
    initial_ids: Sequence[Any] | None = None,
    action: str = "save",
) -> dict[str, Any]:
    """Create or update one draft in a single immediate transaction."""

    owner = _owner(owner)
    token = _token(token)
    if isinstance(expected_version, bool):
        raise MemoConflictError("草稿版本无效，请刷新后重试")
    try:
        expected = int(expected_version)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MemoConflictError("草稿版本无效，请刷新后重试") from exc
    if expected < 0:
        raise MemoConflictError("草稿版本无效，请刷新后重试")
    if action not in {"save", "currentpage", "clear", "apply_source"}:
        raise MemoConflictError("无效的草稿操作")
    normalized_context = _context(context)
    visible_ids = _ids(shown_ids, "shown_ids")
    page_selected = _ids(selected_ids, "selected_ids")
    normalized_orders = _orders(orders)
    normalized_initial = None if initial_ids is None else _ids(initial_ids, "initial_ids")

    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM memo_drafts WHERE token = ?", (token,)).fetchone()
        now_text, now_datetime = _now()
        # Expiry cleanup is a write-side maintenance action.  Keep the token
        # being edited until its explicit owner/version/expiry checks below so
        # an expired form receives a conflict instead of silently recreating a
        # draft under the same token.
        _cleanup_expired_sql(conn, now=now_datetime, exclude_token=token)
        if row is None:
            if expected != 0:
                raise MemoConflictError("草稿不存在或已过期，请重新打开选择页面")
            if normalized_initial is None:
                raise MemoConflictError("首次保存草稿必须提供来源预选集合")
            current = list(normalized_initial)
            conn.execute(
                "INSERT INTO memo_drafts(token, owner, version, context_json, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?)",
                (
                    token,
                    owner,
                    json.dumps(normalized_context, ensure_ascii=False, separators=(",", ":")),
                    now_text,
                    now_text,
                ),
            )
            if action == "clear":
                if page_selected or normalized_orders:
                    raise MemoConflictError("清空操作不能包含选中论文或顺序")
                current = []
            elif action == "apply_source":
                current = list(normalized_initial)
                if normalized_orders:
                    current = _apply_page_update(current, current, current, normalized_orders)
            else:
                current = _apply_page_update(current, visible_ids, page_selected, normalized_orders)
            _write_selected(conn, token, current)
            return {"token": token, "version": 1, "context": normalized_context}

        if row["owner"] != owner:
            raise MemoConflictError("草稿不存在或已绑定其他浏览器会话")
        if _expired(row["updated_at"], now=now_datetime):
            raise MemoConflictError("草稿已过期，请重新选择论文")
        if int(row["version"]) != expected:
            raise MemoConflictError("草稿版本已变化，请刷新后重试")
        if action == "apply_source":
            if normalized_initial is None:
                raise MemoConflictError("重新应用来源预选必须提供来源集合")
            current = list(normalized_initial)
            if normalized_orders:
                current = _apply_page_update(current, current, current, normalized_orders)
        else:
            if normalized_initial is not None:
                raise MemoConflictError("来源预选集合只能在首次保存或应用来源时提供")
            current = _selected_ids(conn, token)
            if action == "clear":
                if page_selected or normalized_orders:
                    raise MemoConflictError("清空操作不能包含选中论文或顺序")
                current = []
            else:
                current = _apply_page_update(current, visible_ids, page_selected, normalized_orders)
        next_version = int(row["version"]) + 1
        conn.execute(
            "UPDATE memo_drafts SET version = ?, context_json = ?, updated_at = ? WHERE token = ?",
            (
                next_version,
                json.dumps(normalized_context, ensure_ascii=False, separators=(",", ":")),
                now_text,
                token,
            ),
        )
        _write_selected(conn, token, current)
        return {"token": token, "version": next_version, "context": normalized_context}


def verify_guard(
    conn: Any,
    guard: DraftGuard | None,
    *,
    paper_ids: Sequence[Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> None:
    """Re-check a command's draft snapshot in its original read/write transaction."""

    if guard is None:
        return
    token = _token(guard.token)
    owner = _owner(guard.owner)
    expected = _positive_id(guard.expected_version, "version")
    draft = _load_draft(conn, token, owner, version=expected)
    current_ids = tuple(_selected_ids(conn, token))
    if current_ids != tuple(guard.paper_ids):
        raise MemoConflictError("草稿选择已变化，请刷新后重新预览")
    if draft["context"] != _context(guard.context):
        raise MemoConflictError("草稿上下文已变化，请刷新后重新预览")
    if paper_ids is not None and tuple(_ids(paper_ids, "paper_ids")) != tuple(guard.paper_ids):
        raise MemoConflictError("确认命令中的论文集合与草稿不一致，请刷新后重试")
    if context is not None and _context(context) != _context(guard.context):
        raise MemoConflictError("确认命令中的草稿上下文与预览不一致，请刷新后重试")


def verify_owner(conn: Any, guard: DraftGuard | None) -> None:
    """Authenticate a draft guard without requiring its old optimistic version."""

    if guard is None:
        return
    token = _token(guard.token)
    owner = _owner(guard.owner)
    row = conn.execute(
        "SELECT owner, updated_at FROM memo_drafts WHERE token = ?", (token,)
    ).fetchone()
    if row is None or row["owner"] != owner:
        raise MemoConflictError("草稿不存在或已绑定其他浏览器会话")
    if _expired(row["updated_at"]):
        raise MemoConflictError("草稿已过期，请重新选择论文")


def cleanup_expired(conn: Any | None = None) -> int:
    """Delete only expired draft rows and their ordered selection rows."""

    owns_connection = conn is None
    if owns_connection:
        conn = db.connect()
        conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now()[1]
        cutoff = (now - DRAFT_TTL).replace(microsecond=0).isoformat(sep=" ")
        deleted = int(
            conn.execute("DELETE FROM memo_drafts WHERE updated_at <= ?", (cutoff,)).rowcount
        )
        if owns_connection:
            conn.commit()
        return deleted
    except BaseException:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        if owns_connection:
            conn.close()


purge_expired = cleanup_expired
