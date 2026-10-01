import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import call_attempts, db, security


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_S4_DB = PROJECT_ROOT / "tmp" / "s5-review" / "db_before.py"
EXPECTED_S4_DB_SHA256 = "5731591ca40822caad0d7fa83fd0345328d24faa181a539958ef23f6de00cae8"
SNAPSHOT_TABLES = (
    "schema_migrations",
    "papers",
    "paper_categories",
    "investment_themes",
    "paper_investment_themes",
    "research_authors",
    "research_organizations",
    "paper_team_tracking",
    "jobs",
    "llm_call_attempts",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_backup(source_path: Path, destination_path: Path) -> None:
    """Take a consistent SQLite snapshot through SQLite's backup API."""
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def _db_snapshot() -> tuple:
    with db.connect() as conn:
        main_schema = tuple(
            tuple(row) for row in conn.execute(
                "SELECT type,name,COALESCE(sql,'') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
        main_rows = tuple(
            (table, tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")))
            for table in SNAPSHOT_TABLES
        )
        main_state = (main_schema, main_rows)
    with db.connect_llm_profiles() as conn:
        profile_schema = tuple(
            tuple(row) for row in conn.execute(
                "SELECT type,name,COALESCE(sql,'') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
        profile_rows = tuple(tuple(row) for row in conn.execute("SELECT * FROM llm_profiles ORDER BY id"))
        profile_state = (profile_schema, profile_rows)
    return main_state, profile_state


class S6ReleaseDrillTests(unittest.TestCase):
    def test_s5_backup_restore_with_s4_compatibility_and_unknown_recovery(self):
        s4_db_path = Path(__import__("os").environ.get("S6_S4_DB_PATH", DEFAULT_S4_DB))
        if not s4_db_path.is_file():
            self.skipTest(
                "Matching S4 db.py is unavailable; set S6_S4_DB_PATH to the S4 baseline-matched db_before.py"
            )
        self.assertEqual(_sha256(s4_db_path), EXPECTED_S4_DB_SHA256)

        with tempfile.TemporaryDirectory(prefix="s6-release-drill-") as temp_name:
            temp_root = Path(temp_name)
            source_main = temp_root / "source" / "main.sqlite3"
            source_profiles = temp_root / "source" / "profiles.sqlite3"
            local_key = temp_root / "source" / "fernet.key"
            (temp_root / "source").mkdir(parents=True)
            directories = [temp_root / "source", temp_root / "restore"]
            self.enterContext(patch.object(db, "DB_PATH", source_main))
            self.enterContext(patch.object(db, "LLM_PROFILES_DB_PATH", source_profiles))
            self.enterContext(patch.object(
                db,
                "ensure_directories",
                side_effect=lambda: [path.mkdir(parents=True, exist_ok=True) for path in directories],
            ))

            # Initialize a fresh synthetic store; the existing profile migration
            # suite covers migration from the historical schema.
            db.init_db()
            db.init_llm_profiles_db()

            paper_id = db.upsert_papers(
                [{
                    "arxiv_id": "2609.00001",
                    "title": "S6 synthetic paper",
                    "authors": ["S6 synthetic author"],
                    "abstract": "Synthetic local recovery fixture.",
                    "subjects": ["cs.AI"],
                    "published_at": "2026-10-01",
                    "rank": 1,
                }],
                "cs.AI",
                "2026-10-01",
            )[0]
            db.create_evaluation(
                paper_id, "fulltext_review", None, None, None, "synthetic-model", "success",
                {"score": 85}, "synthetic result", None,
            )
            theme_id = db.create_investment_theme("S6 synthetic theme")
            db.set_paper_investment_themes(paper_id, [theme_id])
            db.save_paper_team_tracking(paper_id, {
                "author_mode": "new",
                "author_name": "S6 synthetic author",
                "organization_mode": "new",
                "organization_name": "S6 synthetic lab",
                "organization_type": "university",
            })

            store = security.SecretStore(key_path=local_key)
            with patch.object(store, "_dpapi_encrypt", return_value=None):
                encrypted_secret = store.encrypt("s6 synthetic local secret")
            self.assertTrue(encrypted_secret.startswith("fernet:"))
            with db.connect_llm_profiles() as conn:
                conn.execute(
                    """INSERT INTO llm_profiles(
                        name,provider,base_url,model,encrypted_api_key_ref,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    ("S6 synthetic profile", "openai_compatible", "https://example.invalid", "synthetic-model",
                     encrypted_secret, db.now_iso(), db.now_iso()),
                )

            job_id = db.create_job("abstract_eval", {"fixture": "s6"})
            db.update_job(job_id, "running")
            with call_attempts.operation_context(
                "abstract_review", job_id=job_id, paper_id=paper_id, model="synthetic-model",
            ):
                attempt_id, _started = call_attempts.begin_provider_request(
                    "openai_compatible",
                    {"model": "synthetic-model", "messages": [{"role": "user", "content": "synthetic"}]},
                )

            before_repeat_init = _db_snapshot()
            db.init_db()
            db.init_llm_profiles_db()
            db.migrate_llm_profiles_from_main_db()
            after_repeat_init = _db_snapshot()
            self.assertEqual(before_repeat_init, after_repeat_init)

            backup_root = temp_root / "backup"
            backup_root.mkdir()
            backup_main = backup_root / "main.sqlite3"
            backup_profiles = backup_root / "profiles.sqlite3"
            backup_key = backup_root / "fernet.key"
            _sqlite_backup(source_main, backup_main)
            _sqlite_backup(source_profiles, backup_profiles)
            shutil.copy2(local_key, backup_key)
            self.assertEqual(local_key.read_bytes(), backup_key.read_bytes())

            # A clearly post-snapshot synthetic row gives the restore-loss boundary
            # an observable example without touching any real user data.
            db.upsert_papers(
                [{"arxiv_id": "2609.00002", "title": "S6 post-backup synthetic paper",
                  "authors": [], "abstract": "", "subjects": ["cs.AI"], "rank": 2}],
                "cs.AI",
                "2026-10-01",
            )
            with db.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE arxiv_id='2609.00002'"
                ).fetchone()[0], 1)

            restore_root = temp_root / "restore"
            restore_root.mkdir(exist_ok=True)
            restored_main = restore_root / "main.sqlite3"
            restored_profiles = restore_root / "profiles.sqlite3"
            restored_key = restore_root / "fernet.key"
            _sqlite_backup(backup_main, restored_main)
            _sqlite_backup(backup_profiles, restored_profiles)
            shutil.copy2(backup_key, restored_key)

            # Check the untouched snapshot with the current code before the S4
            # compatibility startup runs recovery on the restored database.
            with patch.object(db, "DB_PATH", restored_main), patch.object(
                db, "LLM_PROFILES_DB_PATH", restored_profiles,
            ):
                self.assertEqual(_db_snapshot(), before_repeat_init)
                with db.connect() as conn:
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                with db.connect_llm_profiles() as conn:
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    restored_profile = conn.execute(
                        "SELECT encrypted_api_key_ref FROM llm_profiles WHERE id=1"
                    ).fetchone()
                self.assertEqual(
                    security.SecretStore(key_path=restored_key).decrypt(restored_profile[0]),
                    "s6 synthetic local secret",
                )

            # Build an isolated copy of the complete package and overlay only the
            # S4 baseline-matched db.py. The source checkout is never modified.
            cloned_package_parent = temp_root / "s4-package"
            cloned_package = cloned_package_parent / "daily_coolpapers"
            shutil.copytree(PROJECT_ROOT / "daily_coolpapers", cloned_package)
            shutil.copy2(s4_db_path, cloned_package / "db.py")
            self.assertEqual(_sha256(cloned_package / "db.py"), EXPECTED_S4_DB_SHA256)

            child = textwrap.dedent(
                """
                import json
                import sys
                from pathlib import Path

                package_parent = Path(sys.argv[1])
                sys.path.insert(0, str(package_parent))
                from daily_coolpapers import db
                from daily_coolpapers.security import SecretStore

                db.DB_PATH = Path(sys.argv[2])
                db.LLM_PROFILES_DB_PATH = Path(sys.argv[3])
                key_path = Path(sys.argv[4])
                db.init_db()
                db.init_llm_profiles_db()
                db.migrate_llm_profiles_from_main_db()
                first_recovery = db.mark_unfinished_jobs_interrupted()
                second_recovery = db.mark_unfinished_jobs_interrupted()

                with db.connect() as conn:
                    paper = conn.execute("SELECT title FROM papers WHERE arxiv_id='2609.00001'").fetchone()
                    post_backup = conn.execute("SELECT COUNT(*) FROM papers WHERE arxiv_id='2609.00002'").fetchone()[0]
                    attempt = conn.execute("SELECT status,error_code FROM llm_call_attempts WHERE id=?", (int(sys.argv[5]),)).fetchone()
                    attempts = conn.execute("SELECT COUNT(*) FROM llm_call_attempts").fetchone()[0]
                    job = conn.execute("SELECT status FROM jobs WHERE id=?", (int(sys.argv[6]),)).fetchone()
                with db.connect_llm_profiles() as conn:
                    profile = conn.execute("SELECT model,encrypted_api_key_ref FROM llm_profiles WHERE id=1").fetchone()
                paper_id = int(sys.argv[7])
                themes = db.list_paper_investment_themes([paper_id])[paper_id]
                team = db.get_paper_team_tracking(paper_id)
                decrypted = SecretStore(key_path=key_path).decrypt(profile["encrypted_api_key_ref"])

                report = {
                    "db_module_path": str(Path(db.__file__).resolve()),
                    "paper_title": paper["title"] if paper else None,
                    "theme_names": [item["name"] for item in themes],
                    "team_author": team["author_name"] if team else None,
                    "profile_model": profile["model"] if profile else None,
                    "synthetic_secret_restored": decrypted == "s6 synthetic local secret",
                    "recovery_counts": [first_recovery, second_recovery],
                    "attempt_status": attempt["status"] if attempt else None,
                    "attempt_error": attempt["error_code"] if attempt else None,
                    "attempt_count": attempts,
                    "job_status": job["status"] if job else None,
                    "post_backup_paper_count": post_backup,
                }
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
                """
            )
            completed = subprocess.run(
                [
                    sys.executable, "-B", "-c", child,
                    str(cloned_package_parent), str(restored_main), str(restored_profiles),
                    str(restored_key), str(attempt_id), str(job_id), str(paper_id),
                ],
                cwd=restore_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            result = json.loads(completed.stdout.strip().splitlines()[-1])

            self.assertEqual(Path(result["db_module_path"]), cloned_package / "db.py")
            self.assertEqual(result["paper_title"], "S6 synthetic paper")
            self.assertEqual(result["theme_names"], ["S6 synthetic theme"])
            self.assertEqual(result["team_author"], "S6 synthetic author")
            self.assertEqual(result["profile_model"], "synthetic-model")
            self.assertTrue(result["synthetic_secret_restored"])
            self.assertEqual(result["recovery_counts"], [1, 0])
            self.assertEqual((result["attempt_status"], result["attempt_error"]),
                             ("external_outcome_unknown", "process_interrupted"))
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(result["job_status"], "interrupted")
            self.assertEqual(result["post_backup_paper_count"], 0)


if __name__ == "__main__":
    unittest.main()
