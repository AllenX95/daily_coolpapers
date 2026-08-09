import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db, services
from daily_coolpapers.llm import LLMError


class EvaluationOutcomeTests(unittest.TestCase):
    def _inputs(self, evaluation_type):
        paper = {"id": 1, "title": "Paper", "subjects_list": [], "abstract": "A"}
        prompt = {
            "id": 2,
            "version": 1,
            "type": evaluation_type,
            "template": "{{ markdown }}",
            "llm_profile_id": 3,
            "enabled": 1,
        }
        profile = {
            "id": 3,
            "model": "model-x",
            "enabled": 1,
            "context_window_tokens": 1000,
            "max_output_tokens": 100,
        }
        return paper, prompt, profile

    def test_fulltext_preparation_failure_is_recorded_once(self):
        paper, prompt, profile = self._inputs("fulltext_review")
        with (
            patch.object(services.db, "get_paper", return_value=paper),
            patch.object(services.db, "get_default_prompt", return_value=prompt),
            patch.object(services.db, "get_llm_profile", return_value=profile),
            patch.object(services, "paper_variables", return_value={"markdown": ""}),
            patch.object(services, "ensure_markdown", side_effect=ValueError("论文没有 PDF URL")),
            patch.object(services, "call_llm") as call_llm,
            patch.object(services.db, "create_evaluation", return_value=1) as create_evaluation,
        ):
            with self.assertRaisesRegex(ValueError, "PDF URL"):
                services.evaluate_paper(1, "fulltext_review")

        self.assertEqual(create_evaluation.call_count, 1)
        saved = create_evaluation.call_args.args
        self.assertEqual(saved[6], "failed")
        self.assertEqual(saved[10], "preparation_failed")
        self.assertFalse(saved[11])
        call_llm.assert_not_called()

    def test_provider_failure_keeps_retryable_classification(self):
        paper, prompt, profile = self._inputs("abstract_review")
        prompt["template"] = "{{ title }}"
        with (
            patch.object(services.db, "get_paper", return_value=paper),
            patch.object(services.db, "get_default_prompt", return_value=prompt),
            patch.object(services.db, "get_llm_profile", return_value=profile),
            patch.object(services, "paper_variables", return_value={"title": "Paper"}),
            patch.object(
                services,
                "call_llm",
                side_effect=LLMError("timeout", code="timeout", retryable=True),
            ),
            patch.object(services.db, "create_evaluation", return_value=1) as create_evaluation,
        ):
            with self.assertRaises(LLMError):
                services.evaluate_paper(1, "abstract_review")

        saved = create_evaluation.call_args.args
        self.assertEqual(saved[10], "provider_failed")
        self.assertTrue(saved[11])

    def test_terminal_fulltext_preparation_failure_is_not_auto_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "outcomes.sqlite3"):
                db.init_db()
                paper_id = db.upsert_paper(
                    {
                        "arxiv_id": "2608.00999",
                        "title": "Too Large",
                        "authors": [],
                        "subjects": [],
                        "rank": 1,
                    },
                    "cs.AI",
                    "2026-08-01",
                )
                db.create_evaluation(
                    paper_id,
                    "fulltext_review",
                    None,
                    None,
                    None,
                    "model",
                    "failed",
                    None,
                    None,
                    "context exceeded",
                    "preparation_failed",
                    False,
                )

                automatic = db.list_papers_missing_evaluation("fulltext_review")
                forced = db.list_papers_missing_evaluation(
                    "fulltext_review", include_terminal_failures=True
                )
                latest = db.list_latest_evaluations([paper_id], "fulltext_review")[paper_id]

        self.assertNotIn(paper_id, automatic)
        self.assertIn(paper_id, forced)
        self.assertEqual(latest["outcome"]["error_code"], "preparation_failed")
        self.assertFalse(latest["outcome"]["retryable"])


if __name__ == "__main__":
    unittest.main()
