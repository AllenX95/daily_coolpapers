import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import call_attempts, db
from tests.test_crawl_observability import _paper


class _Response:
    def __init__(self, status_code, usage=None):
        self.status_code = status_code
        self._usage = usage or {}

    def json(self):
        return {"usage": self._usage}


class CallRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(db, "DB_PATH", Path(self.tmp.name) / "main.sqlite3"))
        self.stack.enter_context(patch.object(db, "LLM_PROFILES_DB_PATH", Path(self.tmp.name) / "profiles.sqlite3"))
        self.stack.enter_context(patch.object(db, "ensure_directories"))
        db.init_db()
        db.init_llm_profiles_db()
        self.paper_id = db.upsert_papers([_paper("2609.00001")], "cs.AI", "2026-09-30")[0]

    def _job_and_claim(self):
        job_id = db.create_job("abstract_eval", {"paper_id": self.paper_id})
        db.update_job(job_id, "running")
        token, reason = db.claim_abstract_evaluation(
            self.paper_id, job_id=job_id, pipeline_job_id=job_id,
        )
        self.assertIsNone(reason)
        return job_id, token

    def _operation(self, job_id):
        return call_attempts.operation_context(
            "abstract_review", job_id=job_id, paper_id=self.paper_id,
            evaluation_type="abstract_review", model="test-model",
        )

    def _begin(self):
        return call_attempts.begin_provider_request(
            "openai_compatible",
            {"model": "test-model", "messages": [{"role": "user", "content": "test"}]},
        )

    def test_recovery_marks_unclaimed_in_flight_request_unknown(self):
        with self._operation(None):
            attempt_id, _started = self._begin()

        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 0)
        with db.connect() as conn:
            attempt = conn.execute("SELECT status,error_code FROM llm_call_attempts WHERE id=?", (attempt_id,)).fetchone()
        self.assertEqual((attempt["status"], attempt["error_code"]),
                         ("external_outcome_unknown", "process_interrupted"))

    def test_next_interrupted_attempt_is_not_hidden_by_prior_failed_evaluation(self):
        job_id, token = self._job_and_claim()
        with self._operation(job_id) as operation_id:
            db.mark_evaluation_provider_started(token, operation_id)
            with call_attempts.business_attempt():
                prior_id, prior_started = self._begin()
                call_attempts.finish_provider_response(prior_id, prior_started, "openai_compatible", _Response(503))
                prior_evaluation_id = db.create_evaluation(
                    self.paper_id, "abstract_review", None, None, None, "test-model", "failed",
                    None, None, "provider failed", error_code="provider_failed", pipeline_job_id=job_id,
                )
            with call_attempts.business_attempt(retry_reason="provider_retry"):
                interrupted_id, _started = self._begin()

        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 1)
        with db.connect() as conn:
            evaluations = conn.execute(
                "SELECT id,status,error_code,call_operation_id FROM evaluations WHERE paper_id=? ORDER BY id",
                (self.paper_id,),
            ).fetchall()
            attempts = conn.execute(
                "SELECT id,status,evaluation_id FROM llm_call_attempts WHERE operation_id=? ORDER BY attempt_no",
                (operation_id,),
            ).fetchall()
        self.assertEqual([row["id"] for row in evaluations[:1]], [prior_evaluation_id])
        self.assertEqual([row["error_code"] for row in evaluations],
                         ["provider_failed", "external_outcome_unknown"])
        self.assertEqual(evaluations[1]["call_operation_id"], operation_id)
        self.assertEqual([(row["id"], row["status"]) for row in attempts],
                         [(prior_id, "failed"), (interrupted_id, "external_outcome_unknown")])
        self.assertEqual([row["evaluation_id"] for row in attempts],
                         [prior_evaluation_id, evaluations[1]["id"]])

    def test_known_provider_response_without_business_commit_is_not_unknown_or_retried(self):
        job_id, token = self._job_and_claim()
        with self._operation(job_id) as operation_id:
            db.mark_evaluation_provider_started(token, operation_id)
            with call_attempts.business_attempt():
                attempt_id, started = self._begin()
                call_attempts.finish_provider_response(
                    attempt_id, started, "openai_compatible",
                    _Response(200, {"prompt_tokens": 12, "completion_tokens": 4}),
                )

        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 1)
        with db.connect() as conn:
            evaluation = conn.execute(
                "SELECT id,status,error_code,error_retryable,call_operation_id FROM evaluations WHERE paper_id=?",
                (self.paper_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status,evaluation_id,input_tokens,output_tokens FROM llm_call_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual((evaluation["status"], evaluation["error_code"], evaluation["error_retryable"]),
                         ("failed", "result_persistence_interrupted", 0))
        self.assertEqual(evaluation["call_operation_id"], operation_id)
        self.assertEqual((attempt["status"], attempt["evaluation_id"], attempt["input_tokens"], attempt["output_tokens"]),
                         ("succeeded", evaluation["id"], 12, 4))
        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 0)

    def test_classification_recovery_uses_saved_provider_result(self):
        job_id = db.create_job("daily_pipeline", {})
        db.update_job(job_id, "running")
        with self._operation(job_id) as operation_id:
            with call_attempts.business_attempt():
                evaluation_id = db.start_classification_attempt(
                    self.paper_id, job_id, "daily", [], {"title": "title", "abstract": "abstract"},
                    {}, 1, operation_id=operation_id,
                )
                attempt_id, started = self._begin()
                call_attempts.finish_provider_response(
                    attempt_id, started, "openai_compatible", _Response(200, {"input_tokens": 8}),
                )

        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 1)
        with db.connect() as conn:
            evaluation = conn.execute(
                "SELECT status,error_code,call_operation_id FROM evaluations WHERE id=?", (evaluation_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status,evaluation_id,input_tokens FROM llm_call_attempts WHERE id=?", (attempt_id,),
            ).fetchone()
        self.assertEqual((evaluation["status"], evaluation["error_code"], evaluation["call_operation_id"]),
                         ("failed", "result_persistence_interrupted", operation_id))
        self.assertEqual((attempt["status"], attempt["evaluation_id"], attempt["input_tokens"]),
                         ("succeeded", evaluation_id, 8))

    def test_new_operation_without_attempt_is_pre_request_interruption(self):
        job_id, token = self._job_and_claim()
        with self._operation(job_id) as operation_id:
            db.mark_evaluation_provider_started(token, operation_id)

        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 1)
        self.assertEqual(db.list_evaluations(self.paper_id), [])
        events = db.list_job_events(job_id, event_type="abstract.paper_failed")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "pipeline_interrupted")


if __name__ == "__main__":
    unittest.main()
