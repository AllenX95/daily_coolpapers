"""Research-entity and team-tracking SQL over caller-owned connections."""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .domain_errors import ResearchEntityConflictError, ResearchEntityNotFoundError
from .domain_utils import chunks
from .form_commands import (
    AUTHOR_CATEGORIES,
    ORGANIZATION_TYPES,
    ResearchEntityCommand,
    TeamTrackingCommand,
    normalized_research_name,
    parse_choice,
    parse_int,
)


RESEARCH_ENTITY_TABLES = {'author': 'research_authors', 'organization': 'research_organizations'}


def research_table(kind: str) -> str:
    return RESEARCH_ENTITY_TABLES[parse_choice(kind, 'kind', set(RESEARCH_ENTITY_TABLES))]


def require_research_entity(conn: sqlite3.Connection, kind: str, entity_id: int) -> dict[str, Any]:
    table = research_table(kind)
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (entity_id,)).fetchone() if 0 < entity_id <= 2**63-1 else None
    if row is None:
        raise ResearchEntityNotFoundError('作者或机构不存在，请刷新后重新选择')
    return dict(row)


def research_conflict(kind: str, row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {'kind': kind, 'id': row['id'], 'name': row['name'], 'status': row['status'], 'reason': reason}


def insert_research_entity(
    conn: sqlite3.Connection,
    command: ResearchEntityCommand,
    now_iso: Callable[[], str],
) -> int:
    values = {**command.values, 'created_at': now_iso(), 'updated_at': now_iso()}
    columns = ','.join(values)
    placeholders = ','.join('?' for _ in values)
    return int(conn.execute(f'INSERT INTO {research_table(command.kind)}({columns}) VALUES ({placeholders})',
                            list(values.values())).lastrowid)


def save_paper_team_tracking(
    conn: sqlite3.Connection,
    paper_id: int,
    command: TeamTrackingCommand,
    now_iso: Callable[[], str],
) -> int:
    ids, conflicts = {}, []
    # Resolve both entities before the first DML, including exact duplicates.
    for kind in ('author', 'organization'):
        value = getattr(command, kind)
        if isinstance(value, int):
            row = require_research_entity(conn, kind, value)
            if row['status'] != 'active':
                conflicts.append(research_conflict(kind, row, 'archived'))
            ids[kind] = value
        else:
            row = conn.execute(f'SELECT * FROM {research_table(kind)} WHERE normalized_name=?',
                               (value.values['normalized_name'],)).fetchone()
            if row:
                conflicts.append(research_conflict(kind, row, 'duplicate'))
    if conflicts:
        raise ResearchEntityConflictError(conflicts)
    for kind in ('author', 'organization'):
        if kind not in ids:
            ids[kind] = insert_research_entity(conn, getattr(command, kind), now_iso)
    now = now_iso()
    conn.execute("""INSERT INTO paper_team_tracking(paper_id,lead_author_id,organization_id,notes,created_at,updated_at)
        VALUES (?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET
            lead_author_id=excluded.lead_author_id,organization_id=excluded.organization_id,
            notes=excluded.notes,status='tracking',updated_at=excluded.updated_at
        WHERE paper_team_tracking.lead_author_id != excluded.lead_author_id
            OR paper_team_tracking.organization_id != excluded.organization_id
            OR paper_team_tracking.notes != excluded.notes OR paper_team_tracking.status != 'tracking'""",
        (paper_id, ids['author'], ids['organization'], command.notes, now, now))
    return int(conn.execute('SELECT id FROM paper_team_tracking WHERE paper_id=?', (paper_id,)).fetchone()[0])


def archive_paper_team_tracking(
    conn: sqlite3.Connection,
    paper_id: int,
    now_iso: Callable[[], str],
) -> None:
    conn.execute("UPDATE paper_team_tracking SET status='archived',updated_at=? WHERE paper_id=? AND status='tracking'",
                 (now_iso(), paper_id))


def update_research_entity(
    conn: sqlite3.Connection,
    kind: str,
    entity_id: int,
    action: str,
    command: ResearchEntityCommand | None,
    now_iso: Callable[[], str],
) -> None:
    table = research_table(kind)
    old = require_research_entity(conn, kind, entity_id)
    if command:
        row = conn.execute(f'SELECT * FROM {table} WHERE normalized_name=? AND id!=?',
                           (command.values['normalized_name'], entity_id)).fetchone()
        if row:
            raise ResearchEntityConflictError([research_conflict(kind, row, 'duplicate')])
        values = command.values
    else:
        values = {'status': 'archived' if action == 'archive' else 'active'}
    if any(old[key] != value for key, value in values.items()):
        updates = {**values, 'updated_at': now_iso()}
        conn.execute(f"UPDATE {table} SET {','.join(key+'=?' for key in updates)} WHERE id=?",
                     [*updates.values(), entity_id])


def get_paper_team_tracking(conn: sqlite3.Connection, paper_id: int) -> dict[str, Any] | None:
    row = conn.execute("""SELECT t.*,a.name AS author_name,a.status AS author_status,
        a.author_category,o.name AS organization_name,o.status AS organization_status,o.organization_type
        FROM paper_team_tracking t JOIN research_authors a ON a.id=t.lead_author_id
        JOIN research_organizations o ON o.id=t.organization_id WHERE t.paper_id=?""", (paper_id,)).fetchone()
    return dict(row) if row is not None else None


def research_entity_options(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    table = research_table(kind)
    return [dict(row) for row in conn.execute(f"SELECT id,name FROM {table} WHERE status='active' ORDER BY normalized_name,id")]


def research_filters(author_category: str, organization_type: str) -> tuple[str, str]:
    if author_category:
        author_category = parse_choice(author_category, 'author_category', set(AUTHOR_CATEGORIES))
    if organization_type:
        organization_type = parse_choice(organization_type, 'organization_type', set(ORGANIZATION_TYPES))
    return author_category, organization_type


@dataclass(frozen=True)
class TeamTrackingListQuery:
    where: str
    params: tuple[Any, ...]


def prepare_team_tracking_list_query(
    *,
    query: str = '',
    status: str = 'tracking',
    author_category: str = '',
    organization_type: str = '',
    author_id: int | None = None,
    organization_id: int | None = None,
) -> TeamTrackingListQuery:
    status = parse_choice(status, 'status', {'all', 'tracking', 'archived'})
    author_category, organization_type = research_filters(author_category, organization_type)
    clauses, params = [], []
    if status != 'all':
        clauses.append('t.status=?')
        params.append(status)
    if query.strip():
        clauses.append('(instr(a.normalized_name,?)>0 OR instr(o.normalized_name,?)>0 OR instr(lower(p.title),?)>0)')
        normalized = normalized_research_name(query)
        params.extend([normalized, normalized, query.strip().lower()])
    for column, value in [('a.author_category', author_category), ('o.organization_type', organization_type)]:
        if value:
            clauses.append(column+'=?')
            params.append(value)
    for column, value in [('t.lead_author_id', author_id), ('t.organization_id', organization_id)]:
        if value is not None:
            clauses.append(column+'=?')
            params.append(parse_int(value, column, minimum=1, maximum=2**63-1))
    return TeamTrackingListQuery(' AND '.join(clauses) or '1=1', tuple(params))


def list_team_tracking(conn: sqlite3.Connection, query: TeamTrackingListQuery) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(f"""SELECT t.*,p.title,p.arxiv_id,p.published_at,
        a.name AS author_name,a.author_category,a.status AS author_status,
        o.name AS organization_name,o.organization_type,o.region,o.status AS organization_status
        FROM paper_team_tracking t JOIN papers p ON p.id=t.paper_id
        JOIN research_authors a ON a.id=t.lead_author_id
        JOIN research_organizations o ON o.id=t.organization_id
        WHERE {query.where} ORDER BY t.updated_at DESC,t.id DESC""", query.params)]


@dataclass(frozen=True)
class ResearchEntityListQuery:
    table: str
    foreign_key: str
    other_key: str
    other_table: str
    where: str
    params: tuple[Any, ...]


def prepare_research_entity_list_query(
    kind: str,
    *,
    query: str = '',
    status: str = 'active',
    author_category: str = '',
    organization_type: str = '',
) -> ResearchEntityListQuery:
    table = research_table(kind)
    status = parse_choice(status, 'status', {'all', 'active', 'archived'})
    author_category, organization_type = research_filters(author_category, organization_type)
    foreign_key, other_key, other_table = (('lead_author_id', 'organization_id', 'research_organizations')
        if kind == 'author' else ('organization_id', 'lead_author_id', 'research_authors'))
    clauses, params = [], []
    if status != 'all':
        clauses.append('e.status=?')
        params.append(status)
    if query.strip():
        clauses.append('instr(e.normalized_name,?)>0')
        params.append(normalized_research_name(query))
    for column, value, own in [('author_category', author_category, kind == 'author'),
                                ('organization_type', organization_type, kind == 'organization')]:
        if value:
            clauses.append(f'e.{column}=?' if own else f'EXISTS(SELECT 1 FROM paper_team_tracking tf JOIN {other_table} ot ON ot.id=tf.{other_key} WHERE tf.{foreign_key}=e.id AND ot.{column}=?)')
            params.append(value)
    return ResearchEntityListQuery(table, foreign_key, other_key, other_table,
                                   ' AND '.join(clauses) or '1=1', tuple(params))


def list_research_entities(conn: sqlite3.Connection, query: ResearchEntityListQuery) -> list[dict[str, Any]]:
    rows = [dict(row) for row in conn.execute(f"""WITH counts AS (
        SELECT {query.foreign_key} AS entity_id,COUNT(*) AS paper_count,COUNT(DISTINCT {query.other_key}) AS related_count,
            SUM(status='tracking') AS tracking_count FROM paper_team_tracking GROUP BY {query.foreign_key})
        SELECT e.*,COALESCE(c.paper_count,0) AS paper_count,COALESCE(c.related_count,0) AS related_count,
            COALESCE(c.tracking_count,0) AS tracking_count
        FROM {query.table} e LEFT JOIN counts c ON c.entity_id=e.id WHERE {query.where}
        ORDER BY e.updated_at DESC,e.id DESC""", query.params)]
    by_id = {row['id']: row for row in rows}
    for row in rows:
        row.update(recent_papers=[], related_entities=[])
    for chunk in chunks(list(by_id)):
        placeholders = ','.join('?' for _ in chunk)
        recent = conn.execute(f"""WITH ranked AS (
            SELECT t.{query.foreign_key} AS entity_id,p.id,p.title,p.published_at,t.status,
                ROW_NUMBER() OVER(PARTITION BY t.{query.foreign_key} ORDER BY COALESCE(p.published_at,'') DESC,t.created_at DESC,t.id DESC) AS rn
            FROM paper_team_tracking t JOIN papers p ON p.id=t.paper_id WHERE t.{query.foreign_key} IN ({placeholders}))
            SELECT * FROM ranked WHERE rn<=3 ORDER BY entity_id,rn""", chunk)
        for row in recent:
            by_id[row['entity_id']]['recent_papers'].append(dict(row))
        partners = conn.execute(f"""WITH links AS (
            SELECT DISTINCT t.{query.foreign_key} AS entity_id,o.id,o.name,o.normalized_name,o.status
            FROM paper_team_tracking t JOIN {query.other_table} o ON o.id=t.{query.other_key}
            WHERE t.{query.foreign_key} IN ({placeholders})),ranked AS (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY entity_id ORDER BY normalized_name,id) AS rn FROM links)
            SELECT * FROM ranked WHERE rn<=3 ORDER BY entity_id,rn""", chunk)
        for row in partners:
            by_id[row['entity_id']]['related_entities'].append(dict(row))
    return rows
