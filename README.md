# Daily Cool Papers

A lightweight, local-first web service that helps early-stage VC investors and researchers track and digest arXiv papers from [papers.cool](https://papers.cool). It uses **papers.cool reading stars** (PDF + Kimi clicks) as a crowd-sourced quality signal, then leverages large language models to evaluate abstracts and, on demand, read full texts from a VC investment perspective.

[![GitHub](https://img.shields.io/badge/GitHub-AllenX95%2Fdaily__coolpapers-blue)](https://github.com/AllenX95/daily_coolpapers)

---

## What It Does

1. **Scrapes daily top papers** from `https://papers.cool/arxiv/{category}?sort=1` for configurable arXiv categories (CS.AI, CS.CL, CS.CV, CS.LG, etc.).
2. **Captures popularity signals**: rank, PDF clicks, Kimi clicks, and combined **reading stars**.
3. **Stores everything locally** in SQLite so you can browse, search, and filter historical papers offline.
4. **Evaluates abstracts with LLMs** using prompts tuned for VC-style screening: score, attention level, novelty, practical value, market relevance, startup opportunities, and investment risks.
5. **Reads full texts on demand**: downloads the PDF, converts it to Markdown (MarkItDown preferred, pdfminer fallback), and runs a second LLM evaluation against the complete paper.
6. **Runs on a schedule**: automatically crawl and clean up cache at configurable times every day.

---

## Key Features

- **Local web UI** built with Flask + Jinja2 templates.
- **No browser automation** — plain HTTP + HTML parsing, lightweight and reliable.
- **Category management**: enable/disable categories, configure `top_n` and sort parameters.
- **Prompt engine**: edit, copy, and bind different prompts to different LLM profiles. Prompts use `{{variable}}` substitution.
- **LLM profile manager**: support OpenAI-compatible APIs and Anthropic Messages API; API keys are encrypted locally (Windows DPAPI when available, otherwise Fernet).
- **Job queue**: background worker handles crawling, catch-up, abstract evaluation, full-text reading, and cache cleanup with live progress.
- **Export**: CSV bulk export and per-paper Markdown export including all evaluations.
- **Favorites**: track papers you have fully read and reviewed.
- **Automatic cache retention**: PDFs and Markdown files are cleaned up based on configurable retention days.

---

## Why Reading Stars Matter

On papers.cool, each paper shows how many readers clicked **PDF** and **Kimi**. This project sums them into a **reading_stars** metric as a proxy for reader interest. Combined with category rank, it gives a quick, data-driven signal of which papers are drawing attention before you spend LLM tokens on them.

---

## Installation

Requires **Python 3.12** (recommended).

```bash
# Clone the repo
git clone git@github.com:AllenX95/daily_coolpapers.git
cd daily_coolpapers

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# Install dependencies
python -m pip install -r requirements.txt
```

For better PDF-to-Markdown conversion, also install:

```bash
python -m pip install markitdown
```

If `markitdown` is not installed, the system falls back to `pdfminer`.

---

## Quick Start

### Windows

Double-click:

```text
start_daily_coolpapers.bat
```

The script starts the local service and opens the browser. If the service is already running, it only opens the web page.

### Manual

```bash
python run.py
```

Then open:

```text
http://127.0.0.1:8765
```

---

## Usage Workflow

1. **Configure LLM**: Go to the **LLM Profiles** page and add an OpenAI-compatible or Anthropic profile. The API key is encrypted before being saved.
2. **Review Prompts**: On the **Prompts** page, check the default abstract-review and fulltext-review prompts and make sure each is bound to a suitable model.
3. **Set Categories**: Enable the arXiv categories you care about and adjust `top_n` if needed.
4. **Crawl**: Click **"Crawl & Evaluate Abstracts"** on the home page. The job runner will fetch metadata and then run abstract evaluations in the background.
5. **Screen**: Sort by rank, reading stars, score, or attention level. Use date/category filters to narrow results.
6. **Deep Read**: Click **"Full Text Read"** on any paper to download the PDF and run a full-text LLM evaluation.
7. **Export**: Use CSV export for spreadsheet analysis or Markdown export for notes/sharing.

---

## Project Structure

```text
daily_coolpapers/
├── app.py              # Flask app, routes, and UI helpers
├── crawler.py          # papers.cool HTML parsing and HTTP fetching
├── services.py         # Orchestrates crawl, catch-up, and LLM evaluation jobs
├── llm.py              # OpenAI-compatible and Anthropic LLM clients
├── prompt_engine.py    # Prompt rendering and token estimation
├── default_prompts.py  # Built-in abstract/fulltext prompts
├── fulltext.py         # PDF download and PDF-to-Markdown conversion
├── db.py               # SQLite schema and data access
├── security.py         # API key encryption (DPAPI / Fernet)
├── jobs.py             # Background job queue and scheduler
├── cache_manager.py    # PDF/Markdown cache and cleanup
├── config.py           # Paths and default settings/categories
├── static/             # CSS
└── templates/          # Jinja2 HTML templates

tests/                  # Unit tests
```

---

## Data & Security

All data stays on your local machine:

| Path | Purpose | Committed? |
|---|---|---|
| `data/daily_coolpapers.sqlite3` | Local SQLite database | No |
| `instance/` | Flask secret and encryption key | No |
| `cache/pdf/` | Downloaded PDFs | No |
| `cache/markdown/` | Converted Markdown files | No |
| `logs/current.log` | Runtime logs | No |
| `tmp/` | Temporary files | No |
| `.agents/` | Local agent data | No |

API keys are encrypted at rest. They are masked in the UI and never written to logs or git.

See `.gitignore` for the full exclusion list.

---

## Configuration Highlights

Settings are stored in SQLite and editable via the web UI:

- `crawler.default_top_n` — default number of papers per category.
- `crawler.concurrency` — parallel fetch threads for crawling.
- `llm.abstract_concurrency` — parallel LLM calls for abstract evaluation.
- `cache.pdf_retention_days` / `cache.markdown_retention_days` — auto cleanup.
- `scheduler.enabled` / `scheduler.daily_times` — automatic daily crawl times.

---

## Running Tests

```bash
python -B -m unittest discover -s tests
```

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## Acknowledgments

- Paper metadata is sourced from [papers.cool](https://papers.cool), a reader-friendly mirror of arXiv daily updates.
- PDF content is retrieved from arXiv.
