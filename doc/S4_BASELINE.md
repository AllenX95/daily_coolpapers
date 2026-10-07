# S4 local LLM call-recording overhead baseline

Measurement time: 2026-09-30T07:57:57+08:00

This offline measurement uses a temporary SQLite database and the production `llm.call_llm` path with `httpx.MockTransport`. No real provider or external network is called.

## Environment and settings

```json
{
  "measured_at": "2026-09-30T07:57:57+08:00",
  "python": "3.13.12",
  "executable": "D:\\claude-projects\\daily-coolpapers\\tmp\\a4-test-env\\Scripts\\python.exe",
  "sqlite": "3.50.4",
  "platform": "Windows-11-10.0.26200-SP0",
  "processor": "AMD64 Family 26 Model 36 Stepping 0, AuthenticAMD",
  "logical_cpus": 20,
  "warmup": 5,
  "samples": 30,
  "mock_transport": "httpx.MockTransport; no external provider/network",
  "source_sha256": {
    "daily_coolpapers\\llm.py": "a040d177fc92deff67d6306483661bf94779874e17862f2c40a3d59e41666b1a",
    "daily_coolpapers\\call_attempts.py": "157b526f4b74994c80cba9b1c7c89f0ae6beb23648436fea2ea3f3eb15249942",
    "daily_coolpapers\\db.py": "5731591ca40822caad0d7fa83fd0345328d24faa181a539958ef23f6de00cae8",
    "D:\\claude-projects\\daily-coolpapers\\tests\\benchmark_llm_calls.py": "36a8b5beef70b6e85021e13b5249175b01d6879a8b980378f6873738a2d59498"
  }
}
```

## Results

| Path | Mean ms | p50 ms | p95 ms | Timed samples | Mock requests incl. warmup | Attempt rows |
|---|---:|---:|---:|---:|---:|---:|
| Without tracing | 0.513 | 0.441 | 0.966 | 30 | 35 | 0 |
| With tracing | 13.652 | 12.955 | 18.220 | 30 | 35 | 35 |

Mean additional local overhead per physical attempt: **13.140 ms** (mean with tracing minus mean without tracing; each invocation sent one mock HTTP request; target ≤50 ms): **PASS**.
Request equivalence: **PASS**; each path issued 35 mock requests including warmup, and all request fingerprints matched.
Response and normalized usage equivalence: **PASS**; both paths returned the same result and input/output/total token counts.

## Migration and persistence checks

- Repeated `db.init_db()` was idempotent: **True** across 3 initializer runs; migration rows remained 2 and attempt rows remained 0 before measurement.
- Tracked path created 35 successful attempt rows; untracked path created 0.
- API-key and Authorization sentinels were absent from the attempt rows: **True**. Check scope is only the new `llm_call_attempts` records.
- Temporary SQLite files (tracked fixture): `{"main.sqlite3": 454656}` bytes by file.

Request fingerprints are SHA-256 digests of method, path, and request-body digest; no prompt or credential is included in this report.
