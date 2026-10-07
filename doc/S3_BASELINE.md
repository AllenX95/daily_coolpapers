# S3 性能对照

本报告只测量临时 SQLite 数据库。旧 `model JSON (full)` 是全量物化参考，
与新 HTML 页面响应时间不作一一等价比较；10 万规模只运行新 HTML 代表用例。

## Environment

```json
{
  "time": "2026-09-29 09:22:59 +0800",
  "python": "3.13.12",
  "executable": "D:\\claude-projects\\daily-coolpapers\\tmp\\a4-test-env\\Scripts\\python.exe",
  "sqlite": "3.50.4",
  "platform": "Windows-11-10.0.26200-SP0",
  "processor": "AMD64 Family 26 Model 36 Stepping 0, AuthenticAMD",
  "logical_cpus": 20,
  "seed": 20260905,
  "argv": [
    "D:\\claude-projects\\daily-coolpapers\\tests\\benchmark_s3.py",
    "--sizes",
    "10000",
    "100000",
    "--warmup",
    "5",
    "--samples",
    "30",
    "--report",
    "S3_BASELINE.md"
  ],
  "parameters": {
    "sizes": [
      10000,
      100000
    ],
    "warmup": 5,
    "samples": 30,
    "case": null,
    "report": "S3_BASELINE.md"
  },
  "source_sha256": {
    "daily_coolpapers\\__init__.py": "41362a5ea4931c546433d4c48290c5707bf02dbbea1c3aafa9f43d1225e47003",
    "daily_coolpapers\\abstract_audit.py": "1ad859cef71f615322d7e92018adc8c7358e4fa0f18bad700c9a2c64458c97f7",
    "daily_coolpapers\\app.py": "aec733ed346d1382182e1d5b02a7d72bdd0d5f69ee3b127b133c6a1c6289d4c0",
    "daily_coolpapers\\cache_manager.py": "0c257f3c78f2afb1e6e0e815389a17a39b35e9e6d80fb4cd3d5b190a62e993df",
    "daily_coolpapers\\config.py": "7c5b107c4a40db09be341b6d3313e059cd25675c78cb6b09e6af37422151a12c",
    "daily_coolpapers\\crawler.py": "16f866a79d0b2157527321bd176ffa95a6bc1e1529c67cd53fa46eeafb9b9a49",
    "daily_coolpapers\\db.py": "9a4190fc15be5630a6d8a2b94adcb0264ae19bb48b692055f4a91ba2b2fe380c",
    "daily_coolpapers\\default_prompts.py": "a2512f5679627d7df70183246f2686ba9e435b05c5d24ffc59e2fe0d89549be8",
    "daily_coolpapers\\form_commands.py": "8a8584d3cec5ffdd192d1cc21eb0fb67a5f6563dc26402970a793d823267af92",
    "daily_coolpapers\\fulltext.py": "6a9a44c3dd041b0f843c6f517fbd961b9d2c21de3bb0fa65cd4c579a3adf6914",
    "daily_coolpapers\\job_views.py": "338769ecc574ea896e8bc0ab58a27b1ff4fc2a10ce2cf84b8ff7d0043b9dc4b3",
    "daily_coolpapers\\jobs.py": "40b193e21f993c7e68b2475f546adec20e1273dfced1b6633193fab44db84dbd",
    "daily_coolpapers\\llm.py": "87de5e976d8607f0b04d90d720512614c38c32b64adc896e00607c210214664d",
    "daily_coolpapers\\logging_setup.py": "a5ad1b36e8577755e9c5d8c37b778e09b0dd331254ceaeb05f34672e2317e6ed",
    "daily_coolpapers\\memo_candidates.py": "28d10aa51e99b23cee47683248dcea4442179fda5d1ea0094d498ff1cf2338a4",
    "daily_coolpapers\\memo_contract.py": "035142a749a7e0d628a1f3fa603279ac98aba8427979d61bd2d71a3c1305558e",
    "daily_coolpapers\\memo_db.py": "dde924b0eeaaa9aa88906642bd49ff36cf4c20f9061941ed79a30c0dcd8e985b",
    "daily_coolpapers\\memo_drafts.py": "c71a4c361136de4291cfc28574df22c5413ffdd0f22f73be357c6f5692097e96",
    "daily_coolpapers\\memos.py": "03c7e1d924fd893736b1845eab0c5dd80eccc1394011bad58d2122f5f5f6dc7f",
    "daily_coolpapers\\network.py": "164442901a0553d9e34be1df90ed62e6326861df1142a70d003316ec8b8b4bce",
    "daily_coolpapers\\prompt_engine.py": "ccc079085dc10c47cf7711f9f72526d988505857acee1b7b3d6935c8e704488e",
    "daily_coolpapers\\runtime_lock.py": "f5c1fc8505f5479b05e51ee7956876bef283dedea907b0a25aa25679a3c53539",
    "daily_coolpapers\\schedule_db.py": "f48699d7f1760588ebf1aa0b93315f4cdf9edf05f5f24dbe59db569cba5e5f7b",
    "daily_coolpapers\\security.py": "81b1c4f51e5188c4851aa1a0f7e0c0e71ba37b06d3d0d49d17c9dd5f8b01c2f2",
    "daily_coolpapers\\services.py": "d4fa30c71cfbb697bb3c92176af925363edeafd3d86b229423831190876549ff",
    "daily_coolpapers\\templates\\favorites.html": "ddf7ca3d749731870165da0ed94ceace969848903b44d953e2c54d2e34761f27",
    "daily_coolpapers\\templates\\memo_new.html": "96541e5b1e68cd0ae16b63dd8993f9c5375b9533665a6a09ac58d54ae1a6b71c",
    "D:\\claude-projects\\daily-coolpapers\\tests\\benchmark_s3.py": "26cb9f81b49418802150032327ef4cbcc6f42943bdca3ea66203d4ddb5a57ca5"
  }
}
```

## 10,000 papers

Projection migration (same isolated fixture): first 111.41 ms, second 5.54 ms; first space Δ 1699840 bytes, second space Δ 0 bytes; rows consistent=True, idempotent=True.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| favorites/html/first/page30/rank | 62.31 | 66.06 | 64811 | 0.32 | 4 |
| favorites/html/deep/page100 | 110.59 | 121.87 | 211922 | 1.01 | 4 |
| reviewed/html/first/page30/score_desc | 67.04 | 73.78 | 66502 | 0.33 | 4 |
| reviewed/html/deep/page100 | 104.49 | 133.46 | 215540 | 1.03 | 4 |
| theme/html/first/page30 | 66.59 | 76.8 | 67412 | 0.33 | 5 |
| theme/html/deep/page100/title | 119.63 | 142.43 | 217596 | 1.03 | 5 |
| memo/html/first/page30 | 33.94 | 37.86 | 21697 | 0.39 | 8 |
| memo/html/rare/deep/page100 | 35.9 | 43.02 | 58603 | 0.63 | 8 |
| legacy/reviewed model JSON (full) | 869.2 | 923.15 | 14696940 | 112.17 | 41 |
| legacy/memo candidates JSON (full) | 196.47 | 230.86 | 1913981 | 26.64 | 25 |

<details><summary>Exact results and EXPLAIN QUERY PLAN</summary>

```json
{
  "projection_migration": {
    "successful_fulltext_evaluations": {
      "rows": 18572,
      "distinct_papers": 10000,
      "sha256": "1c9b135ecc6fa4eb02675143ddca41bf230a10703674eea41fbd9a3242a5e641"
    },
    "first": {
      "rows": 18572,
      "distinct_papers": 10000,
      "sha256": "c34e16a8dacf34d08f664f138b1d1ecb4e5e01391aa0f169ba4e79970a2f2123",
      "schema_marker_count": 1,
      "elapsed_ms": 111.41
    },
    "second": {
      "rows": 18572,
      "distinct_papers": 10000,
      "sha256": "c34e16a8dacf34d08f664f138b1d1ecb4e5e01391aa0f169ba4e79970a2f2123",
      "schema_marker_count": 1,
      "elapsed_ms": 5.54
    },
    "storage": {
      "before": {
        "bytes": 171118592,
        "files": {
          "main": 171118592,
          "-wal": 0,
          "-shm": 0,
          "-journal": 0
        }
      },
      "after_first": {
        "bytes": 172818432,
        "files": {
          "main": 172818432,
          "-wal": 0,
          "-shm": 0,
          "-journal": 0
        }
      },
      "after_second": {
        "bytes": 172818432,
        "files": {
          "main": 172818432,
          "-wal": 0,
          "-shm": 0,
          "-journal": 0
        }
      },
      "first_delta_bytes": 1699840,
      "second_delta_bytes": 0
    },
    "quantity_consistent": true,
    "idempotent": true,
    "sql_plans": [
      {
        "query": "SELECT ? FROM schema_migrations WHERE name = 'fulltext_evaluation_projection_v1'",
        "plan": [
          "SEARCH schema_migrations USING COVERING INDEX sqlite_autoindex_schema_migrations_1 (name=?)"
        ]
      },
      {
        "query": "INSERT OR IGNORE INTO fulltext_evaluation_projection(\n                    evaluation_id, paper_id, created_at, score_type, score_scalar\n                )\n                SELECT\n                    e.id, e.paper_id, e.created_at,\n                    CASE\n                        WHEN json_valid(e.result_json)\n                            THEN COALESCE(json_type(e.result_json, '$.score'), 'missing')\n                        ELSE 'invalid'\n                    END,\n                    CASE\n                        WHEN json_valid(e.result_json)\n                            THEN json_extract(e.result_json, '$.score')\n                        ELSE NULL\n                    END\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review' AND e.status = 'success'",
        "plan": [
          "SCAN e"
        ]
      },
      {
        "query": "INSERT INTO schema_migrations(name, applied_at) VALUES ('fulltext_evaluation_projection_v1', '?-?-? ?:?:?')",
        "plan": []
      }
    ]
  },
  "results": [
    {
      "case": "favorites/html/first/page30/rank",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 62.31,
      "p95_ms": 66.06,
      "payload_bytes": 64811,
      "python_peak_mib": 0.32,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "favorites/html/deep/page100",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 110.59,
      "p95_ms": 121.87,
      "payload_bytes": 211922,
      "python_peak_mib": 1.01,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "reviewed/html/first/page30/score_desc",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 67.04,
      "p95_ms": 73.78,
      "payload_bytes": 66502,
      "python_peak_mib": 0.33,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "reviewed/html/deep/page100",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 104.49,
      "p95_ms": 133.46,
      "payload_bytes": 215540,
      "python_peak_mib": 1.03,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "theme/html/first/page30",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 66.59,
      "p95_ms": 76.8,
      "payload_bytes": 67412,
      "python_peak_mib": 0.33,
      "select_count": 5,
      "plans": [
        {
          "query": "SELECT * FROM investment_themes WHERE id=?",
          "plan": [
            "SEARCH investment_themes USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR LAST 3 TERMS OF ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "theme/html/deep/page100/title",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 119.63,
      "p95_ms": 142.43,
      "payload_bytes": 217596,
      "python_peak_mib": 1.03,
      "select_count": 5,
      "plans": [
        {
          "query": "SELECT * FROM investment_themes WHERE id=?",
          "plan": [
            "SEARCH investment_themes USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "memo/html/first/page30",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 33.94,
      "p95_ms": 37.86,
      "payload_bytes": 21697,
      "python_peak_mib": 0.39,
      "select_count": 8,
      "plans": [
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=?\n        ) SELECT COUNT(*) AS total FROM candidates",
          "plan": [
            "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
          ]
        },
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=?\n        ) SELECT id,title,arxiv_id,score,favorited_at,preselected FROM candidates ORDER BY favorited_at DESC, id DESC LIMIT ? OFFSET ?",
          "plan": [
            "SEARCH d USING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT r.*,d.name,d.scope_text,d.status AS direction_status,\n                   (r.manual_decision='confirmed' OR\n                    (r.manual_decision IS NULL AND r.model_decision='matched')) AS effective\n            FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,r.direction_id\n            ",
          "plan": [
            "SEARCH r USING INDEX sqlite_autoindex_paper_direction_results_1 (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            SELECT r.paper_id,t.*\n            FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,t.id\n            ",
          "plan": [
            "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR LAST TERM OF ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n                   a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n                   o.notes AS organization_notes,o.status AS organization_status\n            FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id\n            JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)\n            ",
          "plan": [
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "SELECT * FROM prompts WHERE ?=? AND type = 'investment_memo' AND enabled = ? ORDER BY type, is_default DESC, name",
          "plan": [
            "SCAN prompts",
            "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY"
          ]
        },
        {
          "query": "SELECT * FROM attention_directions WHERE status='active' ORDER BY id",
          "plan": [
            "SCAN attention_directions"
          ]
        },
        {
          "query": "SELECT t.*, COALESCE(m.paper_count,?) AS paper_count\n            FROM investment_themes t LEFT JOIN\n                (SELECT theme_id, COUNT(*) AS paper_count FROM paper_investment_themes GROUP BY theme_id) m\n                ON m.theme_id=t.id ORDER BY t.status, t.updated_at DESC, t.id DESC",
          "plan": [
            "MATERIALIZE m",
            "SCAN paper_investment_themes USING COVERING INDEX idx_paper_investment_themes_theme_created",
            "SCAN t USING INDEX sqlite_autoindex_investment_themes_1",
            "BLOOM FILTER ON m (theme_id=?)",
            "SEARCH m USING AUTOMATIC COVERING INDEX (theme_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "memo/html/rare/deep/page100",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 35.9,
      "p95_ms": 43.02,
      "payload_bytes": 58603,
      "python_peak_mib": 0.63,
      "select_count": 8,
      "plans": [
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=? AND instr(memo_casefold(COALESCE(p.title,'') || ' ' || COALESCE(p.arxiv_id,'')), memo_casefold('Rare')) > ?\n        ) SELECT COUNT(*) AS total FROM candidates",
          "plan": [
            "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
          ]
        },
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=? AND instr(memo_casefold(COALESCE(p.title,'') || ' ' || COALESCE(p.arxiv_id,'')), memo_casefold('Rare')) > ?\n        ) SELECT id,title,arxiv_id,score,favorited_at,preselected FROM candidates ORDER BY favorited_at DESC, id DESC LIMIT ? OFFSET ?",
          "plan": [
            "SEARCH d USING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT r.*,d.name,d.scope_text,d.status AS direction_status,\n                   (r.manual_decision='confirmed' OR\n                    (r.manual_decision IS NULL AND r.model_decision='matched')) AS effective\n            FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,r.direction_id\n            ",
          "plan": [
            "SEARCH r USING INDEX sqlite_autoindex_paper_direction_results_1 (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            SELECT r.paper_id,t.*\n            FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,t.id\n            ",
          "plan": [
            "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR LAST TERM OF ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n                   a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n                   o.notes AS organization_notes,o.status AS organization_status\n            FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id\n            JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)\n            ",
          "plan": [
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "SELECT * FROM prompts WHERE ?=? AND type = 'investment_memo' AND enabled = ? ORDER BY type, is_default DESC, name",
          "plan": [
            "SCAN prompts",
            "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY"
          ]
        },
        {
          "query": "SELECT * FROM attention_directions WHERE status='active' ORDER BY id",
          "plan": [
            "SCAN attention_directions"
          ]
        },
        {
          "query": "SELECT t.*, COALESCE(m.paper_count,?) AS paper_count\n            FROM investment_themes t LEFT JOIN\n                (SELECT theme_id, COUNT(*) AS paper_count FROM paper_investment_themes GROUP BY theme_id) m\n                ON m.theme_id=t.id ORDER BY t.status, t.updated_at DESC, t.id DESC",
          "plan": [
            "MATERIALIZE m",
            "SCAN paper_investment_themes USING COVERING INDEX idx_paper_investment_themes_theme_created",
            "SCAN t USING INDEX sqlite_autoindex_investment_themes_1",
            "BLOOM FILTER ON m (theme_id=?)",
            "SEARCH m USING AUTOMATIC COVERING INDEX (theme_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "legacy/reviewed model JSON (full)",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 869.2,
      "p95_ms": 923.15,
      "payload_bytes": 14696940,
      "python_peak_mib": 112.17,
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
      "case": "legacy/memo candidates JSON (full)",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 196.47,
      "p95_ms": 230.86,
      "payload_bytes": 1913981,
      "python_peak_mib": 26.64,
      "select_count": 25,
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
        }
      ]
    }
  ]
}
```
</details>

## 100,000 papers

Projection migration (same isolated fixture): first 1572.9 ms, second 8.87 ms; first space Δ 17551360 bytes, second space Δ 0 bytes; rows consistent=True, idempotent=True.

| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |
|---|---:|---:|---:|---:|---:|
| favorites/html/first/page30/rank | 283.67 | 309.76 | 64875 | 0.32 | 4 |
| favorites/html/deep/page100 | 319.92 | 336.65 | 212390 | 1.01 | 4 |
| reviewed/html/first/page30/score_desc | 488.57 | 516.47 | 66588 | 0.33 | 4 |
| reviewed/html/deep/page100 | 498.53 | 612.61 | 216043 | 1.03 | 4 |
| theme/html/first/page30 | 527.04 | 653.95 | 67511 | 0.33 | 5 |
| theme/html/deep/page100/title | 653.41 | 831.6 | 218106 | 1.03 | 5 |
| memo/html/first/page30 | 183.98 | 195.28 | 21791 | 0.39 | 8 |
| memo/html/rare/deep/page100 | 163.09 | 173.15 | 58767 | 0.64 | 8 |

<details><summary>Exact results and EXPLAIN QUERY PLAN</summary>

```json
{
  "projection_migration": {
    "successful_fulltext_evaluations": {
      "rows": 185715,
      "distinct_papers": 100000,
      "sha256": "d3d841232308629e864fa4082bef4743e3b6ef54f8599f558311f1aec3a7f99b"
    },
    "first": {
      "rows": 185715,
      "distinct_papers": 100000,
      "sha256": "7f27bb2717fc9994492414a1e30867d879305dfbf86792c139528584b0337e7b",
      "schema_marker_count": 1,
      "elapsed_ms": 1572.9
    },
    "second": {
      "rows": 185715,
      "distinct_papers": 100000,
      "sha256": "7f27bb2717fc9994492414a1e30867d879305dfbf86792c139528584b0337e7b",
      "schema_marker_count": 1,
      "elapsed_ms": 8.87
    },
    "storage": {
      "before": {
        "bytes": 1713438720,
        "files": {
          "main": 1713438720,
          "-wal": 0,
          "-shm": 0,
          "-journal": 0
        }
      },
      "after_first": {
        "bytes": 1730990080,
        "files": {
          "main": 1730990080,
          "-wal": 0,
          "-shm": 0,
          "-journal": 0
        }
      },
      "after_second": {
        "bytes": 1730990080,
        "files": {
          "main": 1730990080,
          "-wal": 0,
          "-shm": 0,
          "-journal": 0
        }
      },
      "first_delta_bytes": 17551360,
      "second_delta_bytes": 0
    },
    "quantity_consistent": true,
    "idempotent": true,
    "sql_plans": [
      {
        "query": "SELECT ? FROM schema_migrations WHERE name = 'fulltext_evaluation_projection_v1'",
        "plan": [
          "SEARCH schema_migrations USING COVERING INDEX sqlite_autoindex_schema_migrations_1 (name=?)"
        ]
      },
      {
        "query": "INSERT OR IGNORE INTO fulltext_evaluation_projection(\n                    evaluation_id, paper_id, created_at, score_type, score_scalar\n                )\n                SELECT\n                    e.id, e.paper_id, e.created_at,\n                    CASE\n                        WHEN json_valid(e.result_json)\n                            THEN COALESCE(json_type(e.result_json, '$.score'), 'missing')\n                        ELSE 'invalid'\n                    END,\n                    CASE\n                        WHEN json_valid(e.result_json)\n                            THEN json_extract(e.result_json, '$.score')\n                        ELSE NULL\n                    END\n                FROM evaluations e\n                WHERE e.evaluation_type = 'fulltext_review' AND e.status = 'success'",
        "plan": [
          "SCAN e"
        ]
      },
      {
        "query": "INSERT INTO schema_migrations(name, applied_at) VALUES ('fulltext_evaluation_projection_v1', '?-?-? ?:?:?')",
        "plan": []
      }
    ]
  },
  "results": [
    {
      "case": "favorites/html/first/page30/rank",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 283.67,
      "p95_ms": 309.76,
      "payload_bytes": 64875,
      "python_peak_mib": 0.32,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "favorites/html/deep/page100",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 319.92,
      "p95_ms": 336.65,
      "payload_bytes": 212390,
      "python_peak_mib": 1.01,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "reviewed/html/first/page30/score_desc",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 488.57,
      "p95_ms": 516.47,
      "payload_bytes": 66588,
      "python_peak_mib": 0.33,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "reviewed/html/deep/page100",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 498.53,
      "p95_ms": 612.61,
      "payload_bytes": 216043,
      "python_peak_mib": 1.03,
      "select_count": 4,
      "plans": [
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "SCAN p USING COVERING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    NULL AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                \n                LEFT JOIN paper_dispositions d ON d.paper_id = p.id\n                LEFT JOIN p",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SCAN p USING INDEX sqlite_autoindex_papers_1",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "theme/html/first/page30",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 527.04,
      "p95_ms": 653.95,
      "payload_bytes": 67511,
      "python_peak_mib": 0.33,
      "select_count": 5,
      "plans": [
        {
          "query": "SELECT * FROM investment_themes WHERE id=?",
          "plan": [
            "SEARCH investment_themes USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR LAST 3 TERMS OF ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "theme/html/deep/page100/title",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 653.41,
      "p95_ms": 831.6,
      "payload_bytes": 218106,
      "python_peak_mib": 1.03,
      "select_count": 5,
      "plans": [
        {
          "query": "SELECT * FROM investment_themes WHERE id=?",
          "plan": [
            "SEARCH investment_themes USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n            WITH candidates AS (\n                SELECT\n                    p.id, p.title,\n                    e.evaluation_id AS fulltext_evaluation_id,\n                    e.created_at AS fulltext_evaluated_at,\n                    e.score_type AS fulltext_score_type,\n                    e.score_scalar AS fulltext_score_scalar,\n                    COALESCE(d.decision, 'undecided') AS decision,\n                    d.updated_at AS decision_updated_at,\n                    tm.created_at AS theme_added_at,\n                    lc.category AS latest_category_name,\n                    lc.rank AS latest_category_rank,\n                    lc.reading_stars AS latest_category_stars\n                FROM papers p\n                LEFT JOIN fulltext_evaluation_projection e\n                    ON e.evaluation_id = (\n                        SELECT ep.evaluation_id\n                        FROM fulltext_evaluation_projection ep\n                        WHERE ep.paper_id = p.id\n                        ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n                        LIMIT ?\n                    )\n                JOIN paper_investment_themes tm ON tm.paper_id = p.id AND tm.theme_id = ?\n        ",
          "plan": [
            "MATERIALIZE page_ids",
            "CO-ROUTINE (subquery-6)",
            "SEARCH tm USING INDEX idx_paper_investment_themes_theme_created (theme_id=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH lc USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "CORRELATED SCALAR SUBQUERY 2",
            "SEARCH pc USING COVERING INDEX idx_paper_categories_paper_date (paper_id=?)",
            "USE TEMP B-TREE FOR ORDER BY",
            "SCAN (subquery-6)",
            "SCAN page_ids",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH ev USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n        SELECT *\n        FROM paper_categories\n        WHERE paper_id IN (?...)\n        ORDER BY paper_id, crawl_date DESC, category\n        ",
          "plan": [
            "SEARCH paper_categories USING INDEX idx_paper_categories_paper_date (paper_id=?)"
          ]
        },
        {
          "query": "\n        SELECT t.id, t.name, t.status, m.paper_id, m.created_at AS added_at\n        FROM paper_investment_themes m\n        JOIN investment_themes t ON t.id = m.theme_id\n        WHERE m.paper_id IN (?...)\n        ORDER BY t.status, m.created_at DESC, t.id\n        ",
          "plan": [
            "SEARCH m USING INDEX idx_paper_investment_themes_paper (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "memo/html/first/page30",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 183.98,
      "p95_ms": 195.28,
      "payload_bytes": 21791,
      "python_peak_mib": 0.39,
      "select_count": 8,
      "plans": [
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=?\n        ) SELECT COUNT(*) AS total FROM candidates",
          "plan": [
            "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
          ]
        },
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=?\n        ) SELECT id,title,arxiv_id,score,favorited_at,preselected FROM candidates ORDER BY favorited_at DESC, id DESC LIMIT ? OFFSET ?",
          "plan": [
            "SEARCH d USING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT r.*,d.name,d.scope_text,d.status AS direction_status,\n                   (r.manual_decision='confirmed' OR\n                    (r.manual_decision IS NULL AND r.model_decision='matched')) AS effective\n            FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,r.direction_id\n            ",
          "plan": [
            "SEARCH r USING INDEX sqlite_autoindex_paper_direction_results_1 (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            SELECT r.paper_id,t.*\n            FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,t.id\n            ",
          "plan": [
            "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR LAST TERM OF ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n                   a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n                   o.notes AS organization_notes,o.status AS organization_status\n            FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id\n            JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)\n            ",
          "plan": [
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "SELECT * FROM prompts WHERE ?=? AND type = 'investment_memo' AND enabled = ? ORDER BY type, is_default DESC, name",
          "plan": [
            "SCAN prompts",
            "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY"
          ]
        },
        {
          "query": "SELECT * FROM attention_directions WHERE status='active' ORDER BY id",
          "plan": [
            "SCAN attention_directions"
          ]
        },
        {
          "query": "SELECT t.*, COALESCE(m.paper_count,?) AS paper_count\n            FROM investment_themes t LEFT JOIN\n                (SELECT theme_id, COUNT(*) AS paper_count FROM paper_investment_themes GROUP BY theme_id) m\n                ON m.theme_id=t.id ORDER BY t.status, t.updated_at DESC, t.id DESC",
          "plan": [
            "MATERIALIZE m",
            "SCAN paper_investment_themes USING COVERING INDEX idx_paper_investment_themes_theme_created",
            "SCAN t USING INDEX sqlite_autoindex_investment_themes_1",
            "BLOOM FILTER ON m (theme_id=?)",
            "SEARCH m USING AUTOMATIC COVERING INDEX (theme_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    },
    {
      "case": "memo/html/rare/deep/page100",
      "samples": 30,
      "warmup": 5,
      "p50_ms": 163.09,
      "p95_ms": 173.15,
      "payload_bytes": 58767,
      "python_peak_mib": 0.64,
      "select_count": 8,
      "plans": [
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=? AND instr(memo_casefold(COALESCE(p.title,'') || ' ' || COALESCE(p.arxiv_id,'')), memo_casefold('Rare')) > ?\n        ) SELECT COUNT(*) AS total FROM candidates",
          "plan": [
            "SEARCH d USING COVERING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"
          ]
        },
        {
          "query": "WITH candidates AS (\n        SELECT\n            p.id,\n            p.title,\n            p.arxiv_id,\n            memo_score(e.score_type,e.score_scalar) AS score,\n            d.created_at AS favorited_at,\n            CASE WHEN ? THEN ? ELSE ? END AS preselected\n        FROM papers p\n        JOIN paper_dispositions d\n          ON d.paper_id=p.id AND d.decision='favorite'\n        JOIN fulltext_evaluation_projection e\n          ON e.evaluation_id=(\n              SELECT ep.evaluation_id\n              FROM fulltext_evaluation_projection ep\n              WHERE ep.paper_id=p.id\n              ORDER BY ep.created_at DESC, ep.evaluation_id DESC\n              LIMIT ?\n          )\n        LEFT JOIN paper_team_tracking t ON t.paper_id=p.id\n        LEFT JOIN research_authors a ON a.id=t.lead_author_id\n        LEFT JOIN research_organizations o ON o.id=t.organization_id\n        WHERE ?=? AND instr(memo_casefold(COALESCE(p.title,'') || ' ' || COALESCE(p.arxiv_id,'')), memo_casefold('Rare')) > ?\n        ) SELECT id,title,arxiv_id,score,favorited_at,preselected FROM candidates ORDER BY favorited_at DESC, id DESC LIMIT ? OFFSET ?",
          "plan": [
            "SEARCH d USING INDEX idx_paper_dispositions_decision_updated (decision=?)",
            "SEARCH p USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH e USING INTEGER PRIMARY KEY (rowid=?)",
            "CORRELATED SCALAR SUBQUERY 1",
            "SEARCH ep USING COVERING INDEX idx_fulltext_projection_latest (paper_id=?)",
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT r.*,d.name,d.scope_text,d.status AS direction_status,\n                   (r.manual_decision='confirmed' OR\n                    (r.manual_decision IS NULL AND r.model_decision='matched')) AS effective\n            FROM paper_direction_results r\n            JOIN attention_directions d ON d.id=r.direction_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,r.direction_id\n            ",
          "plan": [
            "SEARCH r USING INDEX sqlite_autoindex_paper_direction_results_1 (paper_id=?)",
            "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "\n            SELECT r.paper_id,t.*\n            FROM paper_investment_themes r\n            JOIN investment_themes t ON t.id=r.theme_id\n            WHERE r.paper_id IN (?...)\n            ORDER BY r.paper_id,t.id\n            ",
          "plan": [
            "SEARCH r USING COVERING INDEX sqlite_autoindex_paper_investment_themes_1 (paper_id=?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",
            "USE TEMP B-TREE FOR LAST TERM OF ORDER BY"
          ]
        },
        {
          "query": "\n            SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,\n                   a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,\n                   o.notes AS organization_notes,o.status AS organization_status\n            FROM paper_team_tracking t\n            JOIN research_authors a ON a.id=t.lead_author_id\n            JOIN research_organizations o ON o.id=t.organization_id\n            WHERE t.paper_id IN (?...)\n            ",
          "plan": [
            "SEARCH t USING INDEX sqlite_autoindex_paper_team_tracking_1 (paper_id=?)",
            "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
            "SEARCH o USING INTEGER PRIMARY KEY (rowid=?)"
          ]
        },
        {
          "query": "SELECT * FROM prompts WHERE ?=? AND type = 'investment_memo' AND enabled = ? ORDER BY type, is_default DESC, name",
          "plan": [
            "SCAN prompts",
            "USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY"
          ]
        },
        {
          "query": "SELECT * FROM attention_directions WHERE status='active' ORDER BY id",
          "plan": [
            "SCAN attention_directions"
          ]
        },
        {
          "query": "SELECT t.*, COALESCE(m.paper_count,?) AS paper_count\n            FROM investment_themes t LEFT JOIN\n                (SELECT theme_id, COUNT(*) AS paper_count FROM paper_investment_themes GROUP BY theme_id) m\n                ON m.theme_id=t.id ORDER BY t.status, t.updated_at DESC, t.id DESC",
          "plan": [
            "MATERIALIZE m",
            "SCAN paper_investment_themes USING COVERING INDEX idx_paper_investment_themes_theme_created",
            "SCAN t USING INDEX sqlite_autoindex_investment_themes_1",
            "BLOOM FILTER ON m (theme_id=?)",
            "SEARCH m USING AUTOMATIC COVERING INDEX (theme_id=?) LEFT-JOIN",
            "USE TEMP B-TREE FOR ORDER BY"
          ]
        }
      ]
    }
  ]
}
```
</details>
