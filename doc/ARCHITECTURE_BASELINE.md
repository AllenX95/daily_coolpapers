# Architecture S0 baseline

Synthetic, offline measurements only. No production database, profile, cache, or log was read. This records the current read-path behavior before S3; it is not a performance acceptance PASS.

## 收尾摘要（2026-09-16）

S0 数据采集已完成：9 月 5 日的三规模 24 案例主测量均已落盘；9 月 16 日补齐 100／10,000 篇首页首、中、末页及 100,000 篇实际中、末页，共 8 个修正案例。每例 5 次预热＋30 次计时，查询计划和 Python 分配峰值另测。旧错误页码案例保留并明确降为历史任意页／越界探测，不混入修正结果。

| 规模与场景 | 日期 | p95 | SELECT 数 | 解释 |
|---|---|---:|---:|---|
| 10,000 篇：首页首屏 HTML | 2026-09-16 | 1,267.63 ms | 11 | 单日候选集压力，超过拟定 500ms 目标 |
| 100,000 篇：首页真实中间页 HTML | 2026-09-16 | 12,818.00 ms | 11 | 查询数不多，但扫描／窗口计算仍需后续优化 |
| 100,000 篇：首页真实末页 HTML | 2026-09-16 | 11,338.93 ms | 11 | 有内容的末页，已替换原越界页的验收用途 |
| 100,000 篇：备忘录全部候选服务＋JSON | 2026-09-05 | 7,629.80 ms | 1,442 | Python 峰值 1,558.12 MiB；非完整页面时间 |
| 100,000 篇：备忘录 Rare 筛选服务＋JSON | 2026-09-05 | 11,550.38 ms | 1,442 | Python 峰值 1,550.78 MiB；筛选没有避免全量快照 |

这些是 S3 的优化输入，不是 S0／S1 已经修复的性能问题。日期、机器负载和页码选择不同，不能从新旧数字直接得出回归百分比。测试数据全部集中在一个日期；真实跨日归档与生产负载未测量。完整环境、命令、源码哈希和查询计划见下方各次运行记录。

## Method and scope

Run `tmp/a4-test-env/Scripts/python.exe -B -m tests.benchmark_architecture` from the repository root. Defaults: seed 20260905, 5 warmups, 30 timed samples per case; nearest-rank p95. Each size uses a disposable SQLite database initialized by the application schema. Profile and all directory paths are patched into the same temporary root. Runtime startup and socket connections are forbidden. Seed includes two categories and two themes per paper, direction decisions, equal/invalid scores, historical successful fulltext followed by a newer failure, 20% favorites, three evaluations per paper, roughly 3 KiB result JSON and 1.7 KiB raw output per evaluation. Seed/SQLite caches are warm; this is not a cold-disk benchmark.

Home measures actual Flask HTTP test-client HTML responses. Other lists measure their actual route page-model service boundary plus JSON serialization (`favorite_papers_page_model`, `reviewed_papers_page_model`, `investment_theme_papers_model`), excluding Jinja/template and HTTP overhead. Memo measures `memo_db.candidate_data` plus JSON serialization at the database/service boundary, with theme-source preselection and high/low selection queries. These payload sizes are model JSON sizes, not HTML response sizes. Service-plus-JSON latency is a separate workload baseline, not a proven lower bound for HTML rendering; it cannot establish complete-page acceptance against the PRD latency targets.

SQL counts and plans come from a separate traced invocation; Python peak allocation uses a separate tracemalloc invocation and excludes seed generation and native SQLite/OS memory. Peak RSS is not measured. Timed samples have neither trace callback nor allocation tracing. SELECT counts include all executed SELECT/WITH queries, not only primary paper queries. Expanded IN lists are normalized in displayed query shapes; plans execute the full original SQL.

Current unpaginated lists cannot provide meaningful middle/last pages or page sizes 30/100. Those are measured on home; the remaining coverage belongs to S3 after pagination exists. No physical LLM call/ledger overhead is measured (S4). No pre/post regression claim is possible from this single baseline.

All papers share one crawl/publication date: this stresses a large single-day candidate set and is not a forecast for a real archive spread over many dates. Favorites, reviewed and theme service cases currently have no high/low-selectivity filter comparison; only memo has that comparison. Skipped papers, papers with no successful fulltext and independent team entities are covered by existing regression suites rather than this performance fixture.

## Historical run corrections and resumption

The 2026-09-05 run completed all eight cases at all three sizes. Its `home/middle` and `home/last` labels were calculated from the unfiltered paper count, although the home route defaults to the focused direction filter. Consequently those rows are retained as historical arbitrary-page probes, not valid middle/last-page acceptance; the 10,000 and 100,000 `home/last/100` responses are out-of-range empty pages. Corrected measurements appended on resumption use the actual filtered total, and a regression test checks nonempty first/middle/last responses.

The original run did not record the harness/app/template hash or full argv; these missing historical identifiers cannot be reconstructed reliably. Appended runs record these identifiers and command parameters. Early 2026-09-05 samples overlapped with unittest activity, adding uncontrolled host-load noise; the resumed runs are kept separate, not pooled into a single p95. The database/services/memo_db source hashes allow checking that the business read paths are unchanged.

## Existing behavior contracts and regression mapping

| Contract | Existing coverage |
|---|---|
| Home pagination, date filters, stable query object and full export scope | tests/test_pagination.py, test_app.py, test_core.py |
| Favorite/skipped/clear and historical fulltext success eligibility | tests/test_personal_library.py, test_investment_themes.py |
| Theme/archive and author/organization relations | tests/test_investment_themes.py, test_team_tracking.py, test_research_entities.py |
| Direction manual veto, possible, classification/provider selection | tests/test_direction_decisions.py, test_attention_directions.py, test_direction_pipeline.py |
| Memo preview read-only, confirmation revalidation and immutable inputs | tests/test_memo_foundation.py, test_memo_versions.py |
| Memo provider calls, errors, no hidden retry, personal judgment and export | tests/test_memo_generation.py, test_memo_versions.py |
| Job transitions/recovery, pipeline idempotency and polling | tests/test_jobs.py, test_job_center.py, test_pipeline_foundation.py |
| Provider fallback, timeout/error and outcomes | tests/test_llm.py, test_evaluation_outcomes.py |
| CSRF, same-origin POST, redirect and route compatibility | tests/test_app.py, test_team_routes.py |

Mapping identifies existing regression suites; it does not assert every PRD edge case is covered. Unicode casefold, literal `%`/`_`, null/bool/invalid scores, cross-page selection and all-source export equivalence require focused S3 compatibility checks. Run full contracts with `tmp/a4-test-env/Scripts/python.exe -B -m unittest discover -s tests`. Measurement rows below are actual results, while contract test execution is reported by the integrating task.

## Measurement run

```json
{
  "time": "2026-09-05 15:13:43 +0800",
  "python": "3.13.12",
  "executable": "D:\\claude-projects\\daily-coolpapers\\tmp\\a4-test-env\\Scripts\\python.exe",
  "sqlite": "3.50.4",
  "platform": "Windows-11-10.0.26200-SP0",
  "processor": "AMD64 Family 26 Model 36 Stepping 0, AuthenticAMD",
  "logical_cpus": 20,
  "seed": 20260905,
  "source_sha256": {
    "daily_coolpapers\\db.py": "5c52b7396efa0a81f6e956665f091b2be8153675edfb59476c444ba150d12cfd",
    "daily_coolpapers\\services.py": "3770cc9eda77b1178fd5454755fef00417379193bd57086078a1d0198cd476cf",
    "daily_coolpapers\\memo_db.py": "e88a266b5600140a27ff35ea069e50de503e33a08936d703b23384128cb08113"
  }
}
```

### 100 papers / 300 evaluations / 20 favorites

Seed generation: 0.02s.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| home/first/30 HTML | 27.39 | 30.48 | 100092 | 1.3 | 11 |
| home/middle/30 HTML | 27.63 | 30.19 | 100259 | 1.3 | 11 |
| home/last/100 HTML | 34.84 | 38.34 | 188714 | 2.58 | 11 |
| favorites model JSON | 7.97 | 9.54 | 31143 | 0.21 | 3 |
| reviewed model JSON | 11.09 | 13.26 | 153128 | 1.08 | 3 |
| theme model JSON | 14.28 | 15.72 | 155276 | 1.09 | 4 |
| memo/all model JSON | 10.22 | 11.53 | 20653 | 1.51 | 14 |
| memo/rare model JSON | 9.36 | 14.71 | 1209 | 1.51 | 14 |

<details><summary>Exact sample settings and EXPLAIN QUERY PLAN</summary>

```json
[
  {
    "case": "home/first/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 27.39,
    "p95_ms": 30.48,
    "payload_bytes": 100092,
    "python_peak_mib": 1.3,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/middle/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 27.63,
    "p95_ms": 30.19,
    "payload_bytes": 100259,
    "python_peak_mib": 1.3,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/last/100 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 34.84,
    "p95_ms": 38.34,
    "payload_bytes": 188714,
    "python_peak_mib": 2.58,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "favorites model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 7.97,
    "p95_ms": 9.54,
    "payload_bytes": 31143,
    "python_peak_mib": 0.21,
    "select_count": 3,
    "plans": [
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            NULL AS theme_added_at\n        FROM papers p\n        JOIN latest_fulltext e ON e.paper_id = p.id\n        \n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('favorite' = 'all' OR ('favorite' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'favorite')\n        ORDER BY e.created_at DESC, e.id DESC\n    ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SCAN (subquery-1)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "reviewed model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 11.09,
    "p95_ms": 13.26,
    "payload_bytes": 153128,
    "python_peak_mib": 1.08,
    "select_count": 3,
    "plans": [
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            NULL AS theme_added_at\n        FROM papers p\n        JOIN latest_fulltext e ON e.paper_id = p.id\n        \n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('all' = 'all' OR ('all' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'all')\n        ORDER BY e.created_at DESC, e.id DESC\n    ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SCAN (subquery-1)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "theme model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 14.28,
    "p95_ms": 15.72,
    "payload_bytes": 155276,
    "python_peak_mib": 1.09,
    "select_count": 4,
    "plans": [
      {
        "query": "SELECT * FROM investment_themes WHERE id=?",
        "plan": [
          "SEARCH investment_themes USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            tm.created_at AS theme_added_at\n        FROM papers p\n        LEFT JOIN latest_fulltext e ON e.paper_id = p.id\n        JOIN paper_investment_themes tm ON tm.paper_id=p.id AND tm.theme_id=?\n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('all' = 'all' OR ('all' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'all')\n        ORDER BY e.created",
        "plan": [
          "MATERIALIZE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-1) (rn=? AND paper_id=?)",
          "SEARCH (subquery-1) USING AUTOMATIC PARTIAL COVERING INDEX (rn=? AND paper_id=?) LEFT-JOIN",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "memo/all model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 10.22,
    "p95_ms": 11.53,
    "payload_bytes": 20653,
    "python_peak_mib": 1.51,
    "select_count": 14,
    "plans": [
      {
        "query": "SELECT p.id FROM papers p JOIN paper_dispositions d ON d.paper_id=p.id WHERE d.decision='favorite' ORDER BY p.id",
        "plan": [
          "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT p.*,d.decision,d.created_at AS favorited_at FROM papers p\n            LEFT JOIN paper_dispositions d ON d.paper_id=p.id WHERE p.id IN (?...)",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
        ]
      },
      {
        "query": "SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER (\n            PARTITION BY paper_id,evaluation_type ORDER BY created_at DESC,id DESC) AS rn FROM evaluations e\n            WHERE paper_id IN (?...) AND status='success' AND evaluation_type IN ('abstract_review','fulltext_review')) WHERE rn=?",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT * FROM paper_categories WHERE paper_id IN (?...) ORDER BY crawl_date,category",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.*,d.name,d.scope_text,d.status AS direction_status FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.paper_id,t.* FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id WHERE r.paper_id IN (?...) ORDER BY t.id",
        "plan": [
          "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n            a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n            o.notes AS organization_notes,o.status AS organization_status FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)",
        "plan": [
          "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
          "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "SELECT paper_id FROM paper_investment_themes WHERE theme_id=?",
        "plan": [
          "SEARCH paper_investment_themes USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)"
        ]
      }
    ]
  },
  {
    "case": "memo/rare model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 9.36,
    "p95_ms": 14.71,
    "payload_bytes": 1209,
    "python_peak_mib": 1.51,
    "select_count": 14,
    "plans": [
      {
        "query": "SELECT p.id FROM papers p JOIN paper_dispositions d ON d.paper_id=p.id WHERE d.decision='favorite' ORDER BY p.id",
        "plan": [
          "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT p.*,d.decision,d.created_at AS favorited_at FROM papers p\n            LEFT JOIN paper_dispositions d ON d.paper_id=p.id WHERE p.id IN (?...)",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
        ]
      },
      {
        "query": "SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER (\n            PARTITION BY paper_id,evaluation_type ORDER BY created_at DESC,id DESC) AS rn FROM evaluations e\n            WHERE paper_id IN (?...) AND status='success' AND evaluation_type IN ('abstract_review','fulltext_review')) WHERE rn=?",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT * FROM paper_categories WHERE paper_id IN (?...) ORDER BY crawl_date,category",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.*,d.name,d.scope_text,d.status AS direction_status FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.paper_id,t.* FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id WHERE r.paper_id IN (?...) ORDER BY t.id",
        "plan": [
          "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n            a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n            o.notes AS organization_notes,o.status AS organization_status FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)",
        "plan": [
          "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
          "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "SELECT paper_id FROM paper_investment_themes WHERE theme_id=?",
        "plan": [
          "SEARCH paper_investment_themes USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)"
        ]
      }
    ]
  }
]
```
</details>

### 10,000 papers / 30,000 evaluations / 2,000 favorites

Seed generation: 1.79s.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| home/first/30 HTML | 656.89 | 778.34 | 100185 | 1.3 | 11 |
| home/middle/30 HTML | 648.92 | 725.84 | 100762 | 1.31 | 11 |
| home/last/100 HTML | 631.28 | 673.67 | 14066 | 0.07 | 6 |
| favorites model JSON | 219.45 | 240.96 | 3061281 | 22.45 | 9 |
| reviewed model JSON | 605.86 | 652.06 | 15306996 | 112.39 | 41 |
| theme model JSON | 925.72 | 1006.58 | 15536844 | 112.91 | 42 |
| memo/all model JSON | 723.71 | 761.44 | 2063987 | 155.55 | 146 |
| memo/rare model JSON | 706.69 | 727.45 | 104503 | 154.81 | 146 |

<details><summary>Exact sample settings and EXPLAIN QUERY PLAN</summary>

```json
[
  {
    "case": "home/first/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 656.89,
    "p95_ms": 778.34,
    "payload_bytes": 100185,
    "python_peak_mib": 1.3,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/middle/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 648.92,
    "p95_ms": 725.84,
    "payload_bytes": 100762,
    "python_peak_mib": 1.31,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/last/100 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 631.28,
    "p95_ms": 673.67,
    "payload_bytes": 14066,
    "python_peak_mib": 0.07,
    "select_count": 6,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "favorites model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 219.45,
    "p95_ms": 240.96,
    "payload_bytes": 3061281,
    "python_peak_mib": 22.45,
    "select_count": 9,
    "plans": [
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            NULL AS theme_added_at\n        FROM papers p\n        JOIN latest_fulltext e ON e.paper_id = p.id\n        \n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('favorite' = 'all' OR ('favorite' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'favorite')\n        ORDER BY e.created_at DESC, e.id DESC\n    ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SCAN (subquery-1)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "reviewed model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 605.86,
    "p95_ms": 652.06,
    "payload_bytes": 15306996,
    "python_peak_mib": 112.39,
    "select_count": 41,
    "plans": [
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            NULL AS theme_added_at\n        FROM papers p\n        JOIN latest_fulltext e ON e.paper_id = p.id\n        \n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('all' = 'all' OR ('all' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'all')\n        ORDER BY e.created_at DESC, e.id DESC\n    ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SCAN (subquery-1)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "theme model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 925.72,
    "p95_ms": 1006.58,
    "payload_bytes": 15536844,
    "python_peak_mib": 112.91,
    "select_count": 42,
    "plans": [
      {
        "query": "SELECT * FROM investment_themes WHERE id=?",
        "plan": [
          "SEARCH investment_themes USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            tm.created_at AS theme_added_at\n        FROM papers p\n        LEFT JOIN latest_fulltext e ON e.paper_id = p.id\n        JOIN paper_investment_themes tm ON tm.paper_id=p.id AND tm.theme_id=?\n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('all' = 'all' OR ('all' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'all')\n        ORDER BY e.created",
        "plan": [
          "MATERIALIZE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-1) (rn=? AND paper_id=?)",
          "SEARCH (subquery-1) USING AUTOMATIC PARTIAL COVERING INDEX (rn=? AND paper_id=?) LEFT-JOIN",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "memo/all model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 723.71,
    "p95_ms": 761.44,
    "payload_bytes": 2063987,
    "python_peak_mib": 155.55,
    "select_count": 146,
    "plans": [
      {
        "query": "SELECT p.id FROM papers p JOIN paper_dispositions d ON d.paper_id=p.id WHERE d.decision='favorite' ORDER BY p.id",
        "plan": [
          "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT p.*,d.decision,d.created_at AS favorited_at FROM papers p\n            LEFT JOIN paper_dispositions d ON d.paper_id=p.id WHERE p.id IN (?...)",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
        ]
      },
      {
        "query": "SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER (\n            PARTITION BY paper_id,evaluation_type ORDER BY created_at DESC,id DESC) AS rn FROM evaluations e\n            WHERE paper_id IN (?...) AND status='success' AND evaluation_type IN ('abstract_review','fulltext_review')) WHERE rn=?",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT * FROM paper_categories WHERE paper_id IN (?...) ORDER BY crawl_date,category",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.*,d.name,d.scope_text,d.status AS direction_status FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.paper_id,t.* FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id WHERE r.paper_id IN (?...) ORDER BY t.id",
        "plan": [
          "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n            a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n            o.notes AS organization_notes,o.status AS organization_status FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)",
        "plan": [
          "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
          "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "SELECT paper_id FROM paper_investment_themes WHERE theme_id=?",
        "plan": [
          "SEARCH paper_investment_themes USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)"
        ]
      }
    ]
  },
  {
    "case": "memo/rare model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 706.69,
    "p95_ms": 727.45,
    "payload_bytes": 104503,
    "python_peak_mib": 154.81,
    "select_count": 146,
    "plans": [
      {
        "query": "SELECT p.id FROM papers p JOIN paper_dispositions d ON d.paper_id=p.id WHERE d.decision='favorite' ORDER BY p.id",
        "plan": [
          "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT p.*,d.decision,d.created_at AS favorited_at FROM papers p\n            LEFT JOIN paper_dispositions d ON d.paper_id=p.id WHERE p.id IN (?...)",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
        ]
      },
      {
        "query": "SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER (\n            PARTITION BY paper_id,evaluation_type ORDER BY created_at DESC,id DESC) AS rn FROM evaluations e\n            WHERE paper_id IN (?...) AND status='success' AND evaluation_type IN ('abstract_review','fulltext_review')) WHERE rn=?",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT * FROM paper_categories WHERE paper_id IN (?...) ORDER BY crawl_date,category",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.*,d.name,d.scope_text,d.status AS direction_status FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.paper_id,t.* FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id WHERE r.paper_id IN (?...) ORDER BY t.id",
        "plan": [
          "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n            a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n            o.notes AS organization_notes,o.status AS organization_status FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)",
        "plan": [
          "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
          "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "SELECT paper_id FROM paper_investment_themes WHERE theme_id=?",
        "plan": [
          "SEARCH paper_investment_themes USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)"
        ]
      }
    ]
  }
]
```
</details>

### 100,000 papers / 300,000 evaluations / 20,000 favorites

Seed generation: 13.41s.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| home/first/30 HTML | 7130.44 | 7430.45 | 100187 | 1.3 | 11 |
| home/middle/30 HTML | 7268.78 | 7519.43 | 100983 | 1.31 | 11 |
| home/last/100 HTML | 7346.32 | 9880.59 | 14070 | 0.07 | 6 |
| favorites model JSON | 2282.57 | 2350.43 | 30666491 | 225.35 | 81 |
| reviewed model JSON | 6147.26 | 6554.02 | 153362671 | 1118.53 | 401 |
| theme model JSON | 9469.06 | 9571.9 | 155662519 | 1124.82 | 402 |
| memo/all model JSON | 7512.48 | 7629.8 | 20718163 | 1558.12 | 1442 |
| memo/rare model JSON | 10952.21 | 11550.38 | 1047324 | 1550.78 | 1442 |

<details><summary>Exact sample settings and EXPLAIN QUERY PLAN</summary>

```json
[
  {
    "case": "home/first/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 7130.44,
    "p95_ms": 7430.45,
    "payload_bytes": 100187,
    "python_peak_mib": 1.3,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/middle/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 7268.78,
    "p95_ms": 7519.43,
    "payload_bytes": 100983,
    "python_peak_mib": 1.31,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/last/100 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 7346.32,
    "p95_ms": 9880.59,
    "payload_bytes": 14070,
    "python_peak_mib": 0.07,
    "select_count": 6,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "favorites model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 2282.57,
    "p95_ms": 2350.43,
    "payload_bytes": 30666491,
    "python_peak_mib": 225.35,
    "select_count": 81,
    "plans": [
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            NULL AS theme_added_at\n        FROM papers p\n        JOIN latest_fulltext e ON e.paper_id = p.id\n        \n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('favorite' = 'all' OR ('favorite' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'favorite')\n        ORDER BY e.created_at DESC, e.id DESC\n    ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SCAN (subquery-1)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "reviewed model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 6147.26,
    "p95_ms": 6554.02,
    "payload_bytes": 153362671,
    "python_peak_mib": 1118.53,
    "select_count": 401,
    "plans": [
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            NULL AS theme_added_at\n        FROM papers p\n        JOIN latest_fulltext e ON e.paper_id = p.id\n        \n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('all' = 'all' OR ('all' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'all')\n        ORDER BY e.created_at DESC, e.id DESC\n    ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SCAN (subquery-1)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "theme model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 9469.06,
    "p95_ms": 9571.9,
    "payload_bytes": 155662519,
    "python_peak_mib": 1124.82,
    "select_count": 402,
    "plans": [
      {
        "query": "SELECT * FROM investment_themes WHERE id=?",
        "plan": [
          "SEARCH investment_themes USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "\n        WITH latest_fulltext AS (\n            SELECT *\n            FROM (\n                SELECT\n                    e.*,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review'\n                  AND e.status = 'success'\n            )\n            WHERE rn = ?\n        )\n        SELECT\n            p.*,\n            e.id AS fulltext_evaluation_id,\n            e.created_at AS fulltext_evaluated_at,\n            e.model AS fulltext_model,\n            e.result_json AS fulltext_result_json,\n            COALESCE(d.decision, 'undecided') AS decision,\n            d.updated_at AS decision_updated_at,\n            tm.created_at AS theme_added_at\n        FROM papers p\n        LEFT JOIN latest_fulltext e ON e.paper_id = p.id\n        JOIN paper_investment_themes tm ON tm.paper_id=p.id AND tm.theme_id=?\n        LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n        WHERE ('all' = 'all' OR ('all' = 'undecided' AND d.paper_id IS NULL) OR d.decision = 'all')\n        ORDER BY e.created",
        "plan": [
          "MATERIALIZE (subquery-1)",
          "CO-ROUTINE (subquery-4)",
          "SCAN e USING INDEX idx_evaluations_latest_success",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-4)",
          "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-1) (rn=? AND paper_id=?)",
          "SEARCH (subquery-1) USING AUTOMATIC PARTIAL COVERING INDEX (rn=? AND paper_id=?) LEFT-JOIN",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM paper_categories\n                WHERE paper_id IN (?...)\n                ORDER BY paper_id, crawl_date DESC, category\n                ",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
        ]
      },
      {
        "query": "SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at\n                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id\n                WHERE m.paper_id IN (?...) ORDER BY t.status,m.created_at DESC,t.id",
        "plan": [
          "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      }
    ]
  },
  {
    "case": "memo/all model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 7512.48,
    "p95_ms": 7629.8,
    "payload_bytes": 20718163,
    "python_peak_mib": 1558.12,
    "select_count": 1442,
    "plans": [
      {
        "query": "SELECT p.id FROM papers p JOIN paper_dispositions d ON d.paper_id=p.id WHERE d.decision='favorite' ORDER BY p.id",
        "plan": [
          "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT p.*,d.decision,d.created_at AS favorited_at FROM papers p\n            LEFT JOIN paper_dispositions d ON d.paper_id=p.id WHERE p.id IN (?...)",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
        ]
      },
      {
        "query": "SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER (\n            PARTITION BY paper_id,evaluation_type ORDER BY created_at DESC,id DESC) AS rn FROM evaluations e\n            WHERE paper_id IN (?...) AND status='success' AND evaluation_type IN ('abstract_review','fulltext_review')) WHERE rn=?",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT * FROM paper_categories WHERE paper_id IN (?...) ORDER BY crawl_date,category",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.*,d.name,d.scope_text,d.status AS direction_status FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.paper_id,t.* FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id WHERE r.paper_id IN (?...) ORDER BY t.id",
        "plan": [
          "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n            a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n            o.notes AS organization_notes,o.status AS organization_status FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)",
        "plan": [
          "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
          "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "SELECT paper_id FROM paper_investment_themes WHERE theme_id=?",
        "plan": [
          "SEARCH paper_investment_themes USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)"
        ]
      }
    ]
  },
  {
    "case": "memo/rare model JSON",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 10952.21,
    "p95_ms": 11550.38,
    "payload_bytes": 1047324,
    "python_peak_mib": 1550.78,
    "select_count": 1442,
    "plans": [
      {
        "query": "SELECT p.id FROM papers p JOIN paper_dispositions d ON d.paper_id=p.id WHERE d.decision='favorite' ORDER BY p.id",
        "plan": [
          "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT p.*,d.decision,d.created_at AS favorited_at FROM papers p\n            LEFT JOIN paper_dispositions d ON d.paper_id=p.id WHERE p.id IN (?...)",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
        ]
      },
      {
        "query": "SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER (\n            PARTITION BY paper_id,evaluation_type ORDER BY created_at DESC,id DESC) AS rn FROM evaluations e\n            WHERE paper_id IN (?...) AND status='success' AND evaluation_type IN ('abstract_review','fulltext_review')) WHERE rn=?",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT * FROM paper_categories WHERE paper_id IN (?...) ORDER BY crawl_date,category",
        "plan": [
          "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.*,d.name,d.scope_text,d.status AS direction_status FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT r.paper_id,t.* FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id WHERE r.paper_id IN (?...) ORDER BY t.id",
        "plan": [
          "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
          "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n            a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n            o.notes AS organization_notes,o.status AS organization_status FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)",
        "plan": [
          "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
          "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
        ]
      },
      {
        "query": "SELECT paper_id FROM paper_investment_themes WHERE theme_id=?",
        "plan": [
          "SEARCH paper_investment_themes USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)"
        ]
      }
    ]
  }
]
```
</details>

## Measurement run

```json
{
  "time": "2026-09-16 18:07:25 +0800",
  "python": "3.13.12",
  "executable": "D:\\claude-projects\\daily-coolpapers\\tmp\\a4-test-env\\Scripts\\python.exe",
  "sqlite": "3.50.4",
  "platform": "Windows-11-10.0.26200-SP0",
  "processor": "AMD64 Family 26 Model 36 Stepping 0, AuthenticAMD",
  "logical_cpus": 20,
  "seed": 20260905,
  "argv": [
    "D:\\claude-projects\\daily-coolpapers\\tests\\benchmark_architecture.py",
    "--sizes",
    "100",
    "10000",
    "--case",
    "home",
    "--warmup",
    "5",
    "--samples",
    "30"
  ],
  "parameters": {
    "sizes": [
      100,
      10000
    ],
    "warmup": 5,
    "samples": 30,
    "case": [
      "home"
    ],
    "report": "ARCHITECTURE_BASELINE.md"
  },
  "source_sha256": {
    "daily_coolpapers\\db.py": "5c52b7396efa0a81f6e956665f091b2be8153675edfb59476c444ba150d12cfd",
    "daily_coolpapers\\services.py": "3770cc9eda77b1178fd5454755fef00417379193bd57086078a1d0198cd476cf",
    "daily_coolpapers\\memo_db.py": "e88a266b5600140a27ff35ea069e50de503e33a08936d703b23384128cb08113",
    "D:\\claude-projects\\daily-coolpapers\\tests\\benchmark_architecture.py": "6025feb0e6a0ba225cdc1edf0ed25cb85f3e140043ad397187a71df698edb5f9",
    "daily_coolpapers\\app.py": "feb4dc8b1f8b723712618d2cdda39606b94e2fb96fcf88e5329f3c34f6c79bcd",
    "daily_coolpapers\\templates\\index.html": "664b6f0465b7b0ae7a6c51c90b242ae4cfb821124f216b5c4cfd542b3d8af083"
  }
}
```

### 100 papers / 300 evaluations / 20 favorites

Seed generation: 0.02s.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| home/first/30 HTML | 25.96 | 33.63 | 100092 | 1.3 | 11 |
| home/middle/30 HTML | 25.15 | 31.22 | 100259 | 1.3 | 11 |
| home/last/100 HTML | 52.97 | 64.61 | 188714 | 2.58 | 11 |

<details><summary>Exact sample settings and EXPLAIN QUERY PLAN</summary>

```json
[
  {
    "case": "home/first/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 25.96,
    "p95_ms": 33.63,
    "payload_bytes": 100092,
    "python_peak_mib": 1.3,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/middle/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 25.15,
    "p95_ms": 31.22,
    "payload_bytes": 100259,
    "python_peak_mib": 1.3,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/last/100 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 52.97,
    "p95_ms": 64.61,
    "payload_bytes": 188714,
    "python_peak_mib": 2.58,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  }
]
```
</details>

### 10,000 papers / 30,000 evaluations / 2,000 favorites

Seed generation: 2.32s.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| home/first/30 HTML | 1084.54 | 1267.63 | 100185 | 1.3 | 11 |
| home/middle/30 HTML | 1121.55 | 1382.07 | 100733 | 1.31 | 11 |
| home/last/100 HTML | 1104.82 | 1302.27 | 189801 | 2.6 | 11 |

<details><summary>Exact sample settings and EXPLAIN QUERY PLAN</summary>

```json
[
  {
    "case": "home/first/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 1084.54,
    "p95_ms": 1267.63,
    "payload_bytes": 100185,
    "python_peak_mib": 1.3,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/middle/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 1121.55,
    "p95_ms": 1382.07,
    "payload_bytes": 100733,
    "python_peak_mib": 1.31,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/last/100 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 1104.82,
    "p95_ms": 1302.27,
    "payload_bytes": 189801,
    "python_peak_mib": 2.6,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  }
]
```
</details>

## Measurement run

```json
{
  "time": "2026-09-16 18:10:16 +0800",
  "python": "3.13.12",
  "executable": "D:\\claude-projects\\daily-coolpapers\\tmp\\a4-test-env\\Scripts\\python.exe",
  "sqlite": "3.50.4",
  "platform": "Windows-11-10.0.26200-SP0",
  "processor": "AMD64 Family 26 Model 36 Stepping 0, AuthenticAMD",
  "logical_cpus": 20,
  "seed": 20260905,
  "argv": [
    "D:\\claude-projects\\daily-coolpapers\\tests\\benchmark_architecture.py",
    "--sizes",
    "100000",
    "--case",
    "home/middle",
    "--case",
    "home/last",
    "--warmup",
    "5",
    "--samples",
    "30"
  ],
  "parameters": {
    "sizes": [
      100000
    ],
    "warmup": 5,
    "samples": 30,
    "case": [
      "home/middle",
      "home/last"
    ],
    "report": "ARCHITECTURE_BASELINE.md"
  },
  "source_sha256": {
    "daily_coolpapers\\db.py": "5c52b7396efa0a81f6e956665f091b2be8153675edfb59476c444ba150d12cfd",
    "daily_coolpapers\\services.py": "3770cc9eda77b1178fd5454755fef00417379193bd57086078a1d0198cd476cf",
    "daily_coolpapers\\memo_db.py": "e88a266b5600140a27ff35ea069e50de503e33a08936d703b23384128cb08113",
    "D:\\claude-projects\\daily-coolpapers\\tests\\benchmark_architecture.py": "6025feb0e6a0ba225cdc1edf0ed25cb85f3e140043ad397187a71df698edb5f9",
    "daily_coolpapers\\app.py": "feb4dc8b1f8b723712618d2cdda39606b94e2fb96fcf88e5329f3c34f6c79bcd",
    "daily_coolpapers\\templates\\index.html": "664b6f0465b7b0ae7a6c51c90b242ae4cfb821124f216b5c4cfd542b3d8af083"
  }
}
```

### 100,000 papers / 300,000 evaluations / 20,000 favorites

Seed generation: 23.65s.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| home/middle/30 HTML | 11444.2 | 12818.0 | 100615 | 1.31 | 11 |
| home/last/100 HTML | 10641.89 | 11338.93 | 34315 | 0.35 | 11 |

<details><summary>Exact sample settings and EXPLAIN QUERY PLAN</summary>

```json
[
  {
    "case": "home/middle/30 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 11444.2,
    "p95_ms": 12818.0,
    "payload_bytes": 100615,
    "python_peak_mib": 1.31,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  },
  {
    "case": "home/last/100 HTML",
    "samples": 30,
    "warmup": 5,
    "p50_ms": 10641.89,
    "p95_ms": 11338.93,
    "payload_bytes": 34315,
    "python_peak_mib": 0.35,
    "select_count": 11,
    "plans": [
      {
        "query": "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories",
        "plan": [
          "SEARCH paper_categories USING COVERING INDEX idx_paper_categories_crawl_rank"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN"
        ]
      },
      {
        "query": "\n        WITH scoped AS (\n            SELECT pc.*, p.title, p.updated_at\n            FROM paper_categories pc\n            JOIN papers p ON p.id = pc.paper_id\n            WHERE pc.crawl_date = '?-?-?'\n        ),\n        ranked AS (\n            SELECT\n                scoped.*,\n                ROW_NUMBER() OVER (\n                    PARTITION BY paper_id\n                    ORDER BY CASE WHEN rank IS NULL THEN ? ELSE ? END, rank ASC, category ASC, crawl_date DESC, id ASC\n                ) AS rn\n            FROM scoped\n        ),\n        latest_abstract AS (\n            SELECT paper_id, result_json\n            FROM (\n                SELECT\n                    e.paper_id,\n                    e.result_json,\n                    ROW_NUMBER() OVER (\n                        PARTITION BY e.paper_id\n                        ORDER BY e.created_at DESC, e.id DESC\n                    ) AS eval_rn\n                FROM evaluations e\n                WHERE e.evaluation_type = 'abstract_review'\n            )\n            WHERE eval_rn = ?\n        ),\n        candidates AS (\n            SELECT ranked.*\n            FROM ranked\n            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper",
        "plan": [
          "CO-ROUTINE ranked",
          "CO-ROUTINE (subquery-9)",
          "SEARCH pc USING INDEX idx_paper_categories_crawl_rank (crawl_date=?)",
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "USE TEMP B-TREE FOR ORDER BY",
          "SCAN (subquery-9)",
          "MATERIALIZE (subquery-3)",
          "CO-ROUTINE (subquery-10)",
          "SCAN e USING INDEX idx_evaluations_latest",
          "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY",
          "SCAN (subquery-10)",
          "SCAN ranked",
          "SCALAR SUBQUERY 5",
          "SCAN attention_directions USING COVERING INDEX idx_active_direction_name",
          "CORRELATED SCALAR SUBQUERY 6",
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "BLOOM FILTER ON (subquery-3) (eval_rn=? AND paper_id=?)",
          "SEARCH (subquery-3) USING AUTOMATIC PARTIAL COVERING INDEX (eval_rn=? AND paper_id=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n        SELECT\n            pc.id AS paper_category_id,\n            pc.category,\n            pc.crawl_date,\n            pc.rank,\n            pc.reading_stars,\n            pc.pdf_clicks,\n            pc.kimi_clicks,\n            p.*\n        FROM paper_categories pc\n        JOIN papers p ON p.id = pc.paper_id\n        WHERE pc.crawl_date = '?-?-?' AND p.id IN (?...)\n        ",
        "plan": [
          "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH pc USING INDEX idx_paper_categories_paper_date (paper_id=? AND crawl_date=?)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'abstract_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      \n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest (paper_id=? AND evaluation_type=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "\n                SELECT *\n                FROM (\n                    SELECT\n                        e.*,\n                        ROW_NUMBER() OVER (\n                            PARTITION BY e.paper_id\n                            ORDER BY e.created_at DESC, e.id DESC\n                        ) AS rn\n                    FROM evaluations e\n                    WHERE e.paper_id IN (?...)\n                      AND e.evaluation_type = 'fulltext_review'\n                      AND e.status = 'success'\n                )\n                WHERE rn = ?\n                ",
        "plan": [
          "CO-ROUTINE (subquery-1)",
          "CO-ROUTINE (subquery-3)",
          "SEARCH e USING INDEX idx_evaluations_latest_success (paper_id=? AND evaluation_type=? AND status=?)",
          "SCAN (subquery-3)",
          "SCAN (subquery-1)"
        ]
      },
      {
        "query": "SELECT r.*, d.name,d.scope_text,d.status AS direction_status,\n                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at\n                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id\n                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id\n                WHERE r.paper_id IN (?...) ORDER BY r.direction_id",
        "plan": [
          "SEARCH r USING INDEX idx_direction_paper (paper_id=?)",
          "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)",
          "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
          "USE TEMP B-TREE FOR ORDER BY"
        ]
      },
      {
        "query": "\n                SELECT \n    id, type, status, idempotency_key, retry_of_job_id,\n    progress_current, progress_total, progress_message,\n    progress_details_json, error_message, started_at, finished_at, created_at\n\n                FROM jobs\n                \n                ORDER BY created_at DESC, id DESC\n                LIMIT ?\n                ",
        "plan": [
          "SCAN jobs USING INDEX idx_jobs_recent"
        ]
      },
      {
        "query": "SELECT * FROM attention_directions ORDER BY id",
        "plan": [
          "SCAN attention_directions"
        ]
      },
      {
        "query": "SELECT * FROM categories ORDER BY category",
        "plan": [
          "SCAN categories USING INDEX sqlite_autoindex_categories_1"
        ]
      }
    ]
  }
]
```
</details>
