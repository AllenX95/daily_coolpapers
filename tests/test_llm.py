import unittest
from unittest.mock import Mock, patch

from daily_coolpapers import services
from daily_coolpapers.llm import (
    LLMHTTPError,
    LLMResponse,
    LLMResultError,
    call_llm,
    parse_json_response,
    validate_evaluation_result,
)


class FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class LLMContractTests(unittest.TestCase):
    def test_parse_json_response_rejects_non_object_json(self):
        self.assertIsNone(parse_json_response('[{"score": 80}]'))
        self.assertIsNone(parse_json_response("true"))
        self.assertIsNone(parse_json_response("null"))

    def test_historical_non_object_results_are_normalized(self):
        from daily_coolpapers import db

        self.assertEqual(db.loads_json_object('[{"score": 80}]'), {})
        self.assertEqual(db.loads_json_object("true"), {})

    def test_validate_evaluation_result_enforces_stable_fields(self):
        valid = {"score": 80, "attention": "read"}
        self.assertIs(validate_evaluation_result(valid, "abstract_review"), valid)

        invalid_results = [
            {},
            {"score": True, "attention": "read"},
            {"score": 101, "attention": "read"},
            {"score": 80, "attention": "maybe"},
            {"score": 80, "attention": "read", "vc_perspective": []},
            {"score": 80, "attention": "read", "tags": "ai"},
        ]
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaises(LLMResultError):
                    validate_evaluation_result(result, "abstract_review")

    def test_openai_falls_back_only_for_unsupported_response_format(self):
        client = FakeClient(
            [
                FakeResponse(
                    400,
                    {"error": {"message": "response_format json_object is not supported"}},
                    'response_format json_object is not supported',
                ),
                FakeResponse(
                    200,
                    {"choices": [{"message": {"content": '{"score":80,"attention":"read"}'}}]},
                ),
            ]
        )
        profile = {
            "provider": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "model-x",
        }

        with patch("daily_coolpapers.llm._api_key", return_value="<REDACTED>"):
            response = call_llm(profile, "prompt", client=client)

        self.assertEqual(response.result_json["score"], 80)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("response_format", client.calls[0][1]["json"])
        self.assertNotIn("response_format", client.calls[1][1]["json"])
        self.assertFalse(client.closed)

    def test_strict_call_budget_disables_response_format_fallback_and_preserves_zero_temperature(self):
        client = FakeClient([FakeResponse(400,{'error':{'message':'response_format is unsupported'}},'response_format is unsupported')])
        profile = {'provider':'openai_compatible','base_url':'https://example.test/v1','model':'fixture',
                   'temperature':0,'allow_response_format_fallback':False}
        with patch('daily_coolpapers.llm._api_key',return_value='fixture'),self.assertRaises(LLMHTTPError):
            call_llm(profile,'prompt',client=client)
        self.assertEqual(len(client.calls),1)
        self.assertEqual(client.calls[0][1]['json']['temperature'],0)

    def test_openai_does_not_retry_authentication_failure(self):
        client = FakeClient(
            [FakeResponse(401, {"error": {"message": "invalid key"}}, "invalid key")]
        )
        profile = {
            "provider": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "model-x",
        }

        with patch("daily_coolpapers.llm._api_key", return_value="<REDACTED>"):
            with self.assertRaises(LLMHTTPError) as caught:
                call_llm(profile, "prompt", client=client)

        self.assertEqual(caught.exception.status_code, 401)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(client.calls), 1)

    def test_anthropic_response_uses_same_result_contract(self):
        client = FakeClient(
            [
                FakeResponse(
                    200,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": '{"score":75,"attention":"skim"}',
                            }
                        ]
                    },
                )
            ]
        )
        profile = {
            "provider": "anthropic",
            "base_url": "https://example.test/v1",
            "model": "model-y",
        }

        with patch("daily_coolpapers.llm._api_key", return_value="<REDACTED>"):
            response = call_llm(profile, "prompt", client=client)

        result = validate_evaluation_result(response.result_json, "fulltext_review")
        self.assertEqual(result, {"score": 75, "attention": "skim"})
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(client.closed)

    def test_evaluate_paper_records_schema_failure(self):
        paper = {
            "id": 1,
            "arxiv_id": "2609.00001",
            "title": "Paper",
            "published_at": "2026-01-01",
            "subjects_list": [],
            "abstract": "Abstract",
        }
        prompt = {
            "id": 2,
            "version": 1,
            "type": "abstract_review",
            "template": "{{title}}",
            "llm_profile_id": 3,
            "enabled": 1,
        }
        profile = {"id": 3, "model": "model-x", "enabled": 1}

        with (
            patch.object(services.db, 'claim_abstract_evaluation', return_value=('test', None)),
            patch.object(services.db, 'release_evaluation_claim'),
            patch.object(services.db, 'mark_evaluation_provider_started'),
            patch.object(services.db, "get_paper", return_value=paper),
            patch.object(services.db, "get_default_prompt", return_value=prompt),
            patch.object(services.db, "get_llm_profile", return_value=profile),
            patch.object(services, "paper_variables", return_value={"title": "Paper"}),
            patch.object(
                services,
                "call_llm",
                return_value=LLMResponse("raw response", {"score": 80, "attention": "maybe"}),
            ),
            patch.object(services.db, "create_evaluation", return_value=9) as create_evaluation,
        ):
            with self.assertRaises(LLMResultError):
                services.evaluate_paper(1, "abstract_review")

        saved = create_evaluation.call_args.args
        self.assertEqual(saved[6], "failed")
        self.assertIsNone(saved[7])
        self.assertEqual(saved[8], "raw response")
        self.assertIn("invalid_schema", saved[9])


if __name__ == "__main__":
    unittest.main()
