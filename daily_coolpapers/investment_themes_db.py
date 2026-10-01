"""Investment-theme SQL over a caller-owned connection and transaction."""

import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from .domain_errors import ArchivedThemeError, InvestmentThemeNotFoundError
from .domain_utils import chunks
from .form_commands import FormValidationError, InvestmentThemeCommand


def require_theme(conn: sqlite3.Connection, theme_id: int) -> dict[str, Any]:
    row = conn.execute('SELECT * FROM investment_themes WHERE id=?', (theme_id,)).fetchone() if 0 < theme_id <= 2**63-1 else None
    if row is None:
        raise InvestmentThemeNotFoundError('投资主题不存在')
    return dict(row)


def list_investment_themes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("""SELECT t.*, COALESCE(m.paper_count,0) AS paper_count
        FROM investment_themes t LEFT JOIN
            (SELECT theme_id, COUNT(*) AS paper_count FROM paper_investment_themes GROUP BY theme_id) m
            ON m.theme_id=t.id ORDER BY t.status, t.updated_at DESC, t.id DESC""")]


def check_theme_name(conn: sqlite3.Connection, normalized_name: str, theme_id: int | None = None) -> None:
    row = conn.execute('SELECT id FROM investment_themes WHERE normalized_name=?', (normalized_name,)).fetchone()
    if row and row['id'] != theme_id:
        raise FormValidationError({'name': '同名投资主题已存在（包含已归档主题），请复用或恢复原主题'})


def create_investment_theme(
    conn: sqlite3.Connection,
    command: InvestmentThemeCommand,
    now_iso: Callable[[], str],
) -> int:
    check_theme_name(conn, command.normalized_name)
    now = now_iso()
    return int(conn.execute("""INSERT INTO investment_themes(name,normalized_name,description,created_at,updated_at)
        VALUES (?,?,?,?,?)""", (command.name, command.normalized_name, command.description, now, now)).lastrowid)


def update_investment_theme(
    conn: sqlite3.Connection,
    theme_id: int,
    action: str,
    command: InvestmentThemeCommand | None,
    now_iso: Callable[[], str],
) -> None:
    old = require_theme(conn, theme_id)
    if command:
        check_theme_name(conn, command.normalized_name, theme_id)
        if (old['name'], old['description']) != (command.name, command.description):
            conn.execute('UPDATE investment_themes SET name=?,normalized_name=?,description=?,updated_at=? WHERE id=?',
                         (command.name, command.normalized_name, command.description, now_iso(), theme_id))
    else:
        status = 'archived' if action == 'archive' else 'active'
        if old['status'] != status:
            conn.execute('UPDATE investment_themes SET status=?,updated_at=? WHERE id=?', (status, now_iso(), theme_id))


def list_paper_investment_themes(
    conn: sqlite3.Connection,
    paper_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    result = {paper_id: [] for paper_id in paper_ids}
    for chunk in chunks(paper_ids):
        placeholders = ','.join('?' for _ in chunk)
        rows = conn.execute(f"""SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at
            FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id
            WHERE m.paper_id IN ({placeholders}) ORDER BY t.status,m.created_at DESC,t.id""", chunk)
        for row in rows:
            result[row['paper_id']].append(dict(row))
    return result


def set_paper_investment_themes(
    conn: sqlite3.Connection,
    paper_id: int,
    theme_ids: list[int],
    now_iso: Callable[[], str],
) -> None:
    requested = {}
    for chunk in chunks(theme_ids):
        placeholders = ','.join('?' for _ in chunk)
        requested.update({row['id']: row['status'] for row in conn.execute(
            f'SELECT id,status FROM investment_themes WHERE id IN ({placeholders})', chunk)})
    if len(requested) != len(theme_ids):
        raise InvestmentThemeNotFoundError('所选投资主题不存在，请刷新后重试')
    if any(status != 'active' for status in requested.values()):
        raise ArchivedThemeError('所选主题已归档，请刷新后重试；已有归档关系不会被普通保存删除')
    current = {row['theme_id'] for row in conn.execute("""SELECT m.theme_id FROM paper_investment_themes m
        JOIN investment_themes t ON t.id=m.theme_id WHERE m.paper_id=? AND t.status='active'""", (paper_id,))}
    desired = set(theme_ids)
    conn.executemany('DELETE FROM paper_investment_themes WHERE paper_id=? AND theme_id=?',
                     [(paper_id, theme_id) for theme_id in current-desired])
    now = now_iso()
    conn.executemany('INSERT INTO paper_investment_themes(paper_id,theme_id,created_at) VALUES (?,?,?)',
                     [(paper_id, theme_id, now) for theme_id in desired-current])


def paper_investment_theme_options(conn: sqlite3.Connection, paper_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("""SELECT t.id,t.name,t.description,t.status,m.created_at AS added_at
        FROM investment_themes t LEFT JOIN paper_investment_themes m ON m.theme_id=t.id AND m.paper_id=?
        WHERE t.status='active' OR m.paper_id IS NOT NULL ORDER BY t.status,t.normalized_name,t.id""", (paper_id,))]


def remove_paper_investment_theme(conn: sqlite3.Connection, paper_id: int, theme_id: int) -> None:
    require_theme(conn, theme_id)
    conn.execute('DELETE FROM paper_investment_themes WHERE paper_id=? AND theme_id=?', (paper_id, theme_id))
