# Codex Project Instructions

## Project Overview

Daily Cool Papers is a local Flask + SQLite web app that fetches top papers from
papers.cool, stores them locally, and evaluates abstracts or full text with
OpenAI-compatible or Anthropic-style LLM profiles.

Key runtime paths:

- `daily_coolpapers/`: application code, routes, services, jobs, database, LLM, and templates.
- `tests/`: unittest coverage for app and core service behavior.
- `data/daily_coolpapers.sqlite3`: local application database.
- `cache/pdf/` and `cache/markdown/`: downloaded and converted paper artifacts.
- `logs/current.log`: current runtime log.
- `instance/`: local secrets and key material; never commit it.

## Preflight

Before editing, do a short preflight:

- Confirm the working directory is the repository root for this project. It should contain
  `AGENTS.md`, `daily_coolpapers/`, and `tests/`.
- Do not assume a fixed drive letter or absolute parent path. This project may live at
  different local clone paths on different machines; treat paths in these instructions as
  relative to the repository root unless an absolute path is explicitly provided by the user.
- This directory may not be a Git repository; do not rely on `git diff` unless it is available.
- Check whether the target file or PRD exists before implementing from it.
- Search with `rg` and exclude `cache`, `data`, `instance`, `logs`, `tmp`, `__pycache__`, and generated Markdown/PDF artifacts unless the task explicitly needs them.
- Keep local API keys, encrypted profile data, databases, logs, and cache files out of any produced patch or summary.

## Common Commands

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

Run the app:

```powershell
python run.py
```

Open:

```text
http://127.0.0.1:8765
```

Run tests:

```powershell
python -B -m unittest discover -s tests
```

If test discovery behaves unexpectedly, also try:

```powershell
python -B -m unittest
```

## Development Rules

- Prefer the existing Flask route/service/database split over adding new layers.
- Keep network calls injectable or mockable; tests must not require live LLM or papers.cool calls.
- For date parsing, job status, database writes, cache cleanup, and LLM provider behavior, add focused tests because these paths have caused regressions.
- For performance work, check for N+1 SQLite queries, repeated HTTP client construction, repeated large prompt rendering, and polling paths that touch write locks.
- Do not run broad cleanup on `cache`, `data`, `instance`, or `logs` unless the user explicitly asks.
- When changing UI templates, keep the app lightweight and verify the route renders without requiring a browser automation dependency.

## Task Flow

Use this default flow:

1. For broad requests, first produce a read-only Top 5 review with evidence and priority.
2. For specific bugs or UI adjustments, implement directly after preflight.
3. After code edits, run the narrowest relevant tests; prefer the full unittest suite for database, service, or route changes.
4. If a validation command is blocked by local permissions or missing dependencies, state exactly what failed and what remains unverified.

## Sub-Agent Use

Use sub-agents for independent read-only work only:

- One agent can inspect Flask/routes/templates.
- One agent can inspect services/jobs/database performance.
- One agent can inspect tests and regression coverage.

The main thread should integrate conclusions and make file edits. Do not let multiple agents edit overlapping files.

## Handoff Format

End substantial tasks with:

- `cwd`:
- Goal:
- Files read:
- Files changed:
- Commands run:
- Validation result:
- Unverified areas:
- Key decisions:
- Recommended next prompt:
