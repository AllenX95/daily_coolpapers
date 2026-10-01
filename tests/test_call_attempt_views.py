from datetime import datetime, timedelta, timezone
import sqlite3
import unittest

from flask import render_template

from daily_coolpapers import app as app_module, call_attempts


class CallAttemptViewTests(unittest.TestCase):
    def test_paper_details_show_summary_recent_100_unknown_usage_and_scope(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        call_attempts.init_schema(conn)

        started = datetime(2026, 9, 30, tzinfo=timezone.utc)
        for index in range(1, 106):
            conn.execute(
                """INSERT INTO llm_call_attempts(
                    operation_id,business_attempt_no,attempt_no,call_type,paper_id,
                    provider,model,status,started_at,duration_ms,input_hash
                ) VALUES(?,1,1,?,7,'fixture-provider','fixture-model','succeeded',?,10,'hash')""",
                (
                    f"view-operation-{index}",
                    f"call-{index:03d}",
                    (started + timedelta(seconds=index)).isoformat(),
                ),
            )

        details = call_attempts.details_for_paper(conn, 7)
        self.assertEqual(details["summary"]["calls"], 105)
        self.assertEqual(details["summary"]["usage_missing_calls"], 105)
        self.assertTrue(details["summary"]["items_truncated"])
        self.assertEqual(len(details["items"]), 100)
        self.assertEqual(details["items"][0]["call_type"], "call-006")
        self.assertEqual(details["items"][-1]["call_type"], "call-105")

        flask_app = app_module.create_app(secret_key="call-attempt-view-test")
        with flask_app.app_context():
            html = render_template("partials/llm_call_details.html", call_details=details)
            empty_html = render_template(
                "partials/llm_call_details.html",
                call_details={"summary": {"calls": 0}, "items": []},
            )

        self.assertEqual(html.count("<li>"), 100)
        self.assertIn("105 次", html)
        self.assertIn("call-006", html)
        self.assertIn("call-105", html)
        self.assertNotIn("call-005", html)
        self.assertIn("未知 / 未知 / 未知", html)
        self.assertIn("请求成功", html)
        self.assertIn("明细仅显示最近 100 次请求", html)
        self.assertIn("仅统计研究任务中的模型请求；模型连接测试和历史调用未纳入。", html)
        self.assertIn("模型连接测试和历史调用未纳入此处", empty_html)


if __name__ == "__main__":
    unittest.main()
