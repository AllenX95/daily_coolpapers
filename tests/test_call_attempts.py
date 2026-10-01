from contextlib import ExitStack
import contextvars
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from daily_coolpapers import call_attempts, db, llm, services
from daily_coolpapers.security import SecretStore


API_KEY = "CALL_ATTEMPT_TEST_API_KEY_SENTINEL"


class CallAttemptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="call-attempt-tests-")
        self.addCleanup(self.temp.cleanup)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        root = Path(self.temp.name)
        self.db_path = root / "main.sqlite3"
        self.patches.enter_context(patch.object(db, "DB_PATH", self.db_path))
        self.patches.enter_context(patch.object(db, "ensure_directories", return_value=None))
        self.patches.enter_context(
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        )
        self.secret_store = SecretStore(root / "instance" / "fernet.key")
        self.patches.enter_context(patch.object(llm, "secret_store", self.secret_store))
        db.init_db()

    def profile(self, provider="openai_compatible"):
        return {
            "id": 71001,
            "provider": provider,
            "base_url": "https://provider.invalid/v1",
            "model": "call-attempt-fixture",
            "encrypted_api_key_ref": self.secret_store.encrypt(API_KEY),
            "temperature": 0,
            "max_output_tokens": 64,
        }

    def invoke(self, profile, prompt, handler):
        def factory(_profile):
            return httpx.Client(transport=httpx.MockTransport(handler))

        with patch.object(llm, "make_llm_client", side_effect=factory):
            return llm.call_llm(profile, prompt)

    def rows(self, operation_id=None):
        with db.connect() as conn:
            if operation_id is None:
                result = conn.execute("SELECT * FROM llm_call_attempts ORDER BY id")
            else:
                result = conn.execute(
                    "SELECT * FROM llm_call_attempts WHERE operation_id=? ORDER BY attempt_no",
                    (operation_id,),
                )
            return [dict(row) for row in result.fetchall()]

    def create_paper(self, arxiv_id="2609.99991"):
        with db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO papers(arxiv_id,title,abstract,created_at,updated_at) VALUES(?,?,?,?,?)",
                (arxiv_id, "Call attempt fixture", "A test abstract", db.now_iso(), db.now_iso()),
            )
            return int(cursor.lastrowid)

    def test_abstract_evaluation_fallback_links_all_attempts_and_keeps_usage_on_json_failure(self):
        paper_id = self.create_paper()
        prompt = "CALL_ATTEMPT_PROMPT_SENTINEL"
        requests = []

        def handler(request):
            requests.append((request, json.loads(request.content)))
            if len(requests) == 1:
                return httpx.Response(
                    400,
                    json={"error": {"message": "response_format json_object is not supported"}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"score": 80}'}}],
                    "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
                },
                request=request,
            )

        config = services.EvaluationConfig(
            "abstract_review",
            {
                "version": 3, "type": "abstract_review",
                "template": prompt, "enabled": 1,
            },
            self.profile(),
        )
        def factory(_profile):
            return httpx.Client(transport=httpx.MockTransport(handler))

        with patch.object(llm, "make_llm_client", side_effect=factory):
            outcome = services.evaluate_abstract_candidate(
                paper_id, config=config, max_retries=0, retry_wait=lambda _attempt: None,
            )

        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][0].url.path, "/v1/chat/completions")
        self.assertEqual(requests[1][0].url.path, "/v1/chat/completions")
        self.assertIn("response_format", requests[0][1])
        self.assertNotIn("response_format", requests[1][1])
        self.assertEqual(requests[0][0].headers["authorization"], f"Bearer {API_KEY}")

        with db.connect() as conn:
            evaluation = dict(conn.execute(
                "SELECT id,status,error_code,raw_output,call_operation_id FROM evaluations WHERE paper_id=?",
                (paper_id,),
            ).fetchone())
        self.assertEqual(evaluation["status"], "failed")
        self.assertEqual(evaluation["error_code"], "invalid_response")
        self.assertEqual(evaluation["raw_output"], '{"score": 80}')
        operation_id = evaluation["call_operation_id"]
        evaluation_id = evaluation["id"]
        rows = self.rows(operation_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["attempt_no"] for row in rows], [1, 2])
        self.assertEqual({row["business_attempt_no"] for row in rows}, {1})
        self.assertEqual([row["status"] for row in rows], ["failed", "succeeded"])
        self.assertEqual(rows[0]["http_status"], 400)
        self.assertEqual(rows[1]["fallback_reason"], "response_format_unsupported")
        self.assertEqual(
            [(row["input_tokens"], row["output_tokens"], row["total_tokens"]) for row in rows],
            [(None, None, None), (6, 2, 8)],
        )
        self.assertEqual({row["evaluation_id"] for row in rows}, {evaluation_id})
        self.assertTrue(all(row["operation_id"] == operation_id for row in rows))
        for row in rows:
            self.assertRegex(row["input_hash"], r"^[0-9a-f]{64}$")
        serialized_rows = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(API_KEY, serialized_rows)
        self.assertNotIn(f"Bearer {API_KEY}", serialized_rows)
        self.assertNotIn(prompt, serialized_rows)

    def test_anthropic_request_is_recorded_with_provider_usage(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": '{"ok": true}'}],
                    "usage": {
                        "input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2,
                    },
                },
                request=request,
            )

        with call_attempts.operation_context("abstract_review", model="call-attempt-fixture") as operation_id:
            with call_attempts.business_attempt():
                response = self.invoke(self.profile("anthropic"), "anthropic-prompt", handler)

        self.assertEqual(response.result_json, {"ok": True})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/v1/messages")
        self.assertEqual(requests[0].headers["x-api-key"], API_KEY)
        row = self.rows(operation_id)[0]
        self.assertEqual(row["provider"], "anthropic")
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (7, 3))
        self.assertEqual(row["usage_source"], "anthropic")

    def test_missing_malformed_and_failed_response_usage_are_retained_safely(self):
        cases = [
            (
                "usage-missing",
                {"choices": [{"message": {"content": '{"ok": true}'}}]},
                200,
                ("succeeded", None, None, "usage_not_returned"),
                False,
            ),
            (
                "usage-malformed",
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": -1, "completion_tokens": "3", "total_tokens": True},
                },
                200,
                ("succeeded", None, None, "usage_not_returned"),
                False,
            ),
            (
                "http-failure-with-usage",
                {
                    "error": {"message": "busy"},
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                },
                429,
                ("failed", 2, 3, None),
                True,
            ),
        ]

        for call_type, payload, status, expected, should_fail in cases:
            with self.subTest(call_type=call_type):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(status, json=payload, request=request)

                with call_attempts.operation_context(call_type, model="call-attempt-fixture") as operation_id:
                    with call_attempts.business_attempt():
                        if should_fail:
                            with self.assertRaises(llm.LLMHTTPError):
                                self.invoke(self.profile(), "usage-prompt", handler)
                        else:
                            result = self.invoke(self.profile(), "usage-prompt", handler)
                            self.assertEqual(result.result_json, {"ok": True})

                self.assertEqual(len(requests), 1)
                row = self.rows(operation_id)[0]
                self.assertEqual(row["status"], expected[0])
                self.assertEqual(row["input_tokens"], expected[1])
                self.assertEqual(row["output_tokens"], expected[2])
                self.assertEqual(row["usage_missing_reason"], expected[3])
                if should_fail:
                    self.assertEqual(row["http_status"], 429)
                    self.assertEqual(row["total_tokens"], 5)
                    self.assertEqual(row["error_code"], "http_429")

    def test_request_write_failure_sends_no_provider_request(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}, request=request)

        with call_attempts.operation_context("abstract_review"):
            with call_attempts.business_attempt():
                with patch.object(call_attempts.db, "connect", side_effect=sqlite3.OperationalError("injected begin failure")):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.invoke(self.profile(), "never-sent", handler)

        self.assertEqual(requests, [])
        self.assertEqual(self.rows(), [])

    def test_abstract_retry_loop_response_write_failure_does_not_resend_request(self):
        paper_id = self.create_paper()
        requests = []
        retry_waits = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"score": 80, "attention": "read"}'}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
                },
                request=request,
            )

        config = services.EvaluationConfig(
            "abstract_review",
            {"version": 3, "type": "abstract_review", "template": "retry-write-failure", "enabled": 1},
            self.profile(),
        )

        real_connect = db.connect
        write_failed = False

        def fail_response_write(*args, **kwargs):
            nonlocal write_failed
            if requests and not write_failed:
                write_failed = True
                raise sqlite3.OperationalError("injected response write failure")
            return real_connect(*args, **kwargs)

        def factory(_profile):
            return httpx.Client(transport=httpx.MockTransport(handler))

        with (
            patch.object(llm, "make_llm_client", side_effect=factory),
            patch.object(call_attempts.db, "connect", side_effect=fail_response_write),
            self.assertRaises(sqlite3.OperationalError),
        ):
            services.evaluate_abstract_candidate(
                paper_id,
                config=config,
                max_retries=2,
                retry_wait=lambda attempt: retry_waits.append(attempt),
                raise_errors=True,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(retry_waits, [])
        with db.connect() as conn:
            evaluation = dict(conn.execute(
                "SELECT id,status,error_code,call_operation_id FROM evaluations WHERE paper_id=?",
                (paper_id,),
            ).fetchone())
        self.assertEqual(evaluation["status"], "failed")
        rows = self.rows(evaluation["call_operation_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "provider_started")
        self.assertEqual(rows[0]["evaluation_id"], evaluation["id"])

    def test_operation_context_is_isolated_from_new_thread(self):
        requests = []
        handler = lambda request: (
            requests.append(request)
            or httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"ok": true}'}}]},
                request=request,
            )
        )
        child_operation_ids = []
        thread_errors = []
        profile = self.profile()
        isolated_context = contextvars.Context()

        def run_in_thread():
            try:
                child_operation_ids.append(call_attempts.current_operation_id())
                self.invoke(profile, "thread-prompt", handler)
            except BaseException as exc:
                thread_errors.append(exc)

        with patch.object(
            llm,
            "make_llm_client",
            side_effect=lambda _profile: httpx.Client(transport=httpx.MockTransport(handler)),
        ):
            with call_attempts.operation_context("abstract_review") as operation_id:
                with call_attempts.business_attempt():
                    thread = threading.Thread(target=lambda: isolated_context.run(run_in_thread))
                    thread.start()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive(), "mock call thread did not finish")
                    response = llm.call_llm(profile, "parent-prompt")

        self.assertEqual(thread_errors, [])
        self.assertEqual(child_operation_ids, [None])
        self.assertEqual(response.result_json, {"ok": True})
        self.assertEqual(len(requests), 2)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["operation_id"], operation_id)
        self.assertEqual(rows[0]["attempt_no"], 1)


if __name__ == "__main__":
    unittest.main()
