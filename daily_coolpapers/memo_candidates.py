"""Transactional, lightweight memo candidate queries.

The caller owns the connection and transaction.  This module only registers
SQLite scalar functions and runs bounded queries on the supplied connection;
it never opens a second connection or commits.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Mapping

from . import db


COUNT_KEYS = (
    "source_total",
    "source_favorites",
    "preselected",
    "not_favorite",
    "pending_possible",
    "rejected",
    "missing_fulltext",
    "not_effective",
)
_SORTS = {"favorite_desc", "favorite_asc", "score_desc", "title"}
_MAX_PAGE_SIZE = 100


def _casefold(value: Any) -> str:
    return str(value or "").casefold()


def _memo_score(score_type: Any, score_scalar: Any) -> float:
    """Return memo score semantics: only JSON numbers are valid scores."""
    if score_type not in {"integer", "real"} or isinstance(score_scalar, bool):
        return -1
    try:
        score = float(score_scalar)
    except (TypeError, ValueError, OverflowError):
        return -1
    return score if math.isfinite(score) else -1


def register_functions(conn: sqlite3.Connection) -> None:
    """Register the deterministic helpers used by candidate SQL."""
    conn.create_function("memo_casefold", 1, _casefold)
    conn.create_function("memo_score", 2, _memo_score)


def _source_parts(source: Mapping[str, Any] | None) -> tuple[str, int | None]:
    source = source or {}
    mode = str(source.get("mode") or "manual")
    try:
        source_id = int(source["id"]) if source.get("id") is not None else None
    except (TypeError, ValueError):
        source_id = None
    if mode not in {"manual", "attention_direction", "investment_theme"}:
        mode = "manual"
        source_id = None
    return mode, source_id


def _filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(filters or {})
    return {
        "query": str(values.get("query") or ""),
        "direction_id": values.get("direction_id"),
        "theme_id": values.get("theme_id"),
        "author": str(values.get("author") or ""),
        "organization": str(values.get("organization") or ""),
        "favorite_from": str(values.get("favorite_from") or ""),
        "favorite_to": str(values.get("favorite_to") or ""),
        "min_score": values.get("min_score"),
        "sort": values.get("sort") if values.get("sort") in _SORTS else "favorite_desc",
    }


def _integer(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _preselection_sql(mode: str, source_id: int | None) -> tuple[str, list[Any]]:
    if mode == "investment_theme" and source_id is not None:
        return (
            "EXISTS (SELECT 1 FROM paper_investment_themes sm "
            "WHERE sm.paper_id=p.id AND sm.theme_id=?)",
            [source_id],
        )
    if mode == "attention_direction" and source_id is not None:
        return (
            "EXISTS (SELECT 1 FROM paper_direction_results sr "
            "WHERE sr.paper_id=p.id AND sr.direction_id=? "
            "AND (sr.manual_decision='confirmed' "
            "OR (sr.manual_decision IS NULL AND sr.model_decision='matched'))) ",
            [source_id],
        )
    return "0", []


def _candidate_cte(
    source: Mapping[str, Any] | None,
    filters: Mapping[str, Any] | None,
) -> tuple[str, list[Any]]:
    mode, source_id = _source_parts(source)
    values = _filters(filters)
    preselected_sql, preselected_params = _preselection_sql(mode, source_id)
    clauses = ["1=1"]
    params: list[Any] = [*preselected_params]

    query = values["query"]
    if query:
        clauses.append(
            "instr(memo_casefold(COALESCE(p.title,'') || ' ' || COALESCE(p.arxiv_id,'')), "
            "memo_casefold(?)) > 0"
        )
        params.append(query)

    direction_id = _integer(values["direction_id"])
    if direction_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_direction_results fd "
            "WHERE fd.paper_id=p.id AND fd.direction_id=? "
            "AND (fd.manual_decision='confirmed' "
            "OR (fd.manual_decision IS NULL AND fd.model_decision='matched')))"
        )
        params.append(direction_id)

    theme_id = _integer(values["theme_id"])
    if theme_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_investment_themes ft "
            "WHERE ft.paper_id=p.id AND ft.theme_id=?)"
        )
        params.append(theme_id)

    if values["author"]:
        clauses.append("instr(memo_casefold(COALESCE(a.name,'')), memo_casefold(?)) > 0")
        params.append(values["author"])
    if values["organization"]:
        clauses.append("instr(memo_casefold(COALESCE(o.name,'')), memo_casefold(?)) > 0")
        params.append(values["organization"])
    if values["favorite_from"]:
        clauses.append("substr(COALESCE(d.created_at,''),1,10) >= ?")
        params.append(values["favorite_from"])
    if values["favorite_to"]:
        clauses.append("substr(COALESCE(d.created_at,''),1,10) <= ?")
        params.append(values["favorite_to"])
    if values["min_score"] is not None:
        clauses.append("memo_score(e.score_type,e.score_scalar) >= ?")
        params.append(values["min_score"])

    return (
        f"""
        SELECT
            p.id,
            p.title,
            p.arxiv_id,
            memo_score(e.score_type,e.score_scalar) AS score,
            d.created_at AS favorited_at,
            CASE WHEN {preselected_sql} THEN 1 ELSE 0 END AS preselected
        FROM papers p
        JOIN paper_dispositions d
          ON d.paper_id=p.id AND d.decision='favorite'
        JOIN fulltext_evaluation_projection e
          ON e.evaluation_id=(
              SELECT ep.evaluation_id
              FROM fulltext_evaluation_projection ep
              WHERE ep.paper_id=p.id
              ORDER BY ep.created_at DESC, ep.evaluation_id DESC
              LIMIT 1
          )
        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id
        LEFT JOIN research_authors a ON a.id=t.lead_author_id
        LEFT JOIN research_organizations o ON o.id=t.organization_id
        WHERE {' AND '.join(clauses)}
        """,
        params,
    )


def _order_sql(sort: str) -> str:
    if sort == "title":
        return "memo_casefold(title) ASC, id ASC"
    if sort == "score_desc":
        return "score DESC, id ASC"
    if sort == "favorite_asc":
        return "favorited_at ASC, id ASC"
    return "favorited_at DESC, id DESC"


def _source_counts(conn: sqlite3.Connection, source: Mapping[str, Any] | None) -> dict[str, int]:
    counts = {key: 0 for key in COUNT_KEYS}
    mode, source_id = _source_parts(source)
    if mode == "manual" or source_id is None:
        return counts

    if mode == "investment_theme":
        success = (
            "EXISTS (SELECT 1 FROM fulltext_evaluation_projection ep "
            "WHERE ep.paper_id=x.paper_id)"
        )
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS source_total,
                   COALESCE(SUM(x.decision='favorite'),0) AS source_favorites,
                   COALESCE(SUM(NOT {success}),0) AS missing_fulltext,
                   COALESCE(SUM(x.decision='favorite' AND {success}),0) AS preselected
            FROM (
                SELECT m.paper_id, d.decision
                FROM paper_investment_themes m
                LEFT JOIN paper_dispositions d ON d.paper_id=m.paper_id
                WHERE m.theme_id=?
            ) x
            """,
            (source_id,),
        ).fetchone()
        counts["source_total"] = int(row["source_total"] or 0)
        counts["source_favorites"] = int(row["source_favorites"] or 0)
        counts["missing_fulltext"] = int(row["missing_fulltext"] or 0)
        counts["preselected"] = int(row["preselected"] or 0)
        counts["not_favorite"] = counts["source_total"] - counts["source_favorites"]
        return counts

    success = (
        "EXISTS (SELECT 1 FROM fulltext_evaluation_projection ep "
        "WHERE ep.paper_id=r.paper_id)"
    )
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS source_total,
               COALESCE(SUM(d.decision='favorite'),0) AS source_favorites,
               COALESCE(SUM(NOT {success}),0) AS missing_fulltext,
               COALESCE(SUM(r.model_decision='possible' AND r.manual_decision IS NULL),0)
                   AS pending_possible,
               COALESCE(SUM(r.manual_decision='rejected'),0) AS rejected,
               COALESCE(SUM(CASE WHEN r.manual_decision='confirmed'
                   OR (r.manual_decision IS NULL AND r.model_decision='matched')
                   THEN 0 ELSE 1 END),0)
                   AS not_effective,
               COALESCE(SUM(d.decision='favorite' AND {success}
                   AND (r.manual_decision='confirmed'
                   OR (r.manual_decision IS NULL AND r.model_decision='matched'))),0)
                   AS preselected
        FROM paper_direction_results r
        LEFT JOIN paper_dispositions d ON d.paper_id=r.paper_id
        WHERE r.direction_id=?
        """,
        (source_id,),
    ).fetchone()
    for key in (
        "source_total",
        "source_favorites",
        "missing_fulltext",
        "pending_possible",
        "rejected",
        "not_effective",
        "preselected",
    ):
        counts[key] = int(row[key] or 0)
    counts["not_favorite"] = counts["source_total"] - counts["source_favorites"]
    return counts


def _hydrate_page(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [int(row["id"]) for row in rows]
    directions = {paper_id: [] for paper_id in ids}
    themes = {paper_id: [] for paper_id in ids}
    teams: dict[int, dict[str, Any] | None] = {paper_id: None for paper_id in ids}
    for chunk in db._chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT r.*,d.name,d.scope_text,d.status AS direction_status,
                   (r.manual_decision='confirmed' OR
                    (r.manual_decision IS NULL AND r.model_decision='matched')) AS effective
            FROM paper_direction_results r
            JOIN attention_directions d ON d.id=r.direction_id
            WHERE r.paper_id IN ({marks})
            ORDER BY r.paper_id,r.direction_id
            """,
            chunk,
        ):
            item = dict(row)
            item["effective"] = bool(item["effective"])
            directions[int(row["paper_id"])].append(item)
        for row in conn.execute(
            f"""
            SELECT r.paper_id,t.*
            FROM paper_investment_themes r
            JOIN investment_themes t ON t.id=r.theme_id
            WHERE r.paper_id IN ({marks})
            ORDER BY r.paper_id,t.id
            """,
            chunk,
        ):
            themes[int(row["paper_id"])].append(dict(row))
        for row in conn.execute(
            f"""
            SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,
                   a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,
                   o.notes AS organization_notes,o.status AS organization_status
            FROM paper_team_tracking t
            JOIN research_authors a ON a.id=t.lead_author_id
            JOIN research_organizations o ON o.id=t.organization_id
            WHERE t.paper_id IN ({marks})
            """,
            chunk,
        ):
            teams[int(row["paper_id"])] = dict(row)
    for row in rows:
        paper_id = int(row["id"])
        row["directions"] = directions[paper_id]
        row["themes"] = themes[paper_id]
        row["team"] = teams[paper_id] or {}
        row["preselected"] = bool(row["preselected"])
    return rows


def page_data(
    conn: sqlite3.Connection,
    source: Mapping[str, Any] | None,
    filters: Mapping[str, Any] | None,
    page: int = 1,
    page_size: int = 30,
) -> dict[str, Any]:
    """Return one bounded candidate page on the caller's transaction."""
    register_functions(conn)
    page = max(1, int(page))
    page_size = min(_MAX_PAGE_SIZE, max(1, int(page_size)))
    values = _filters(filters)
    candidate_sql, params = _candidate_cte(source, values)
    total = int(
        conn.execute(
            f"WITH candidates AS ({candidate_sql}) SELECT COUNT(*) AS total FROM candidates",
            params,
        ).fetchone()["total"]
    )
    offset = (page - 1) * page_size
    rows = [
        dict(row)
        for row in conn.execute(
            f"WITH candidates AS ({candidate_sql}) "
            f"SELECT id,title,arxiv_id,score,favorited_at,preselected "
            f"FROM candidates ORDER BY {_order_sql(values['sort'])} LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
    ]
    rows = _hydrate_page(conn, rows)
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "candidates": rows,
        "counts": _source_counts(conn, source),
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "has_previous": page > 1,
        "has_next": page * page_size < total,
    }


def selection_ids(
    conn: sqlite3.Connection,
    source: Mapping[str, Any] | None,
    filters: Mapping[str, Any] | None = None,
    source_only: bool = False,
) -> list[int]:
    """Return global ordered IDs; source-only ignores filters except sort."""
    register_functions(conn)
    mode, source_id = _source_parts(source)
    if source_only and (mode == "manual" or source_id is None):
        return []
    values = _filters(filters if not source_only else {"sort": _filters(filters)["sort"]})
    candidate_sql, params = _candidate_cte(source, values)
    source_clause = " WHERE preselected=1" if source_only else ""
    rows = conn.execute(
        f"WITH candidates AS ({candidate_sql}) "
        f"SELECT id FROM candidates{source_clause} ORDER BY {_order_sql(values['sort'])}",
        params,
    ).fetchall()
    return [int(row[0]) for row in rows]


def preselected_orders(
    conn: sqlite3.Connection,
    source: Mapping[str, Any] | None,
    filters: Mapping[str, Any] | None,
    page_ids: Any,
) -> dict[int, int]:
    """Return global 1-based orders for the supplied page IDs only.

    The source-qualified candidate set is ranked in SQL before the outer
    ``page_ids`` filter.  This keeps initial GET rendering bounded while
    preserving the global preselection order across pages.
    """
    register_functions(conn)
    mode, source_id = _source_parts(source)
    if mode == "manual" or source_id is None:
        return {}
    ids = db._unique_ints(page_ids)
    if not ids:
        return {}
    values = _filters(filters)
    candidate_sql, params = _candidate_cte(source, {"sort": values["sort"]})
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        WITH candidates AS ({candidate_sql}),
        preselected AS (
            SELECT id,
                   ROW_NUMBER() OVER (ORDER BY {_order_sql(values['sort'])}) AS global_order
            FROM candidates
            WHERE preselected=1
        )
        SELECT id,global_order
        FROM preselected
        WHERE id IN ({marks})
        """,
        [*params, *ids],
    ).fetchall()
    return {int(row["id"]): int(row["global_order"]) for row in rows}


def eligible_ids(conn: sqlite3.Connection, ids: Any) -> set[int]:
    """Return IDs that are still favorite and have any historical success."""
    normalized = list(dict.fromkeys(db._unique_ints(ids)))
    eligible: set[int] = set()
    for chunk in db._chunks(normalized):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT d.paper_id
            FROM paper_dispositions d
            WHERE d.decision='favorite'
              AND d.paper_id IN ({marks})
              AND EXISTS (
                  SELECT 1 FROM evaluations e
                  WHERE e.paper_id=d.paper_id
                    AND e.evaluation_type='fulltext_review'
                    AND e.status='success'
              )
            """,
            chunk,
        ).fetchall()
        eligible.update(int(row[0]) for row in rows)
    return eligible
