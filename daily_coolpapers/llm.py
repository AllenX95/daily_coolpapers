import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .db import get_bool_setting, loads_json
from .network import httpx_proxy_kwargs
from .security import secret_store

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    def __init__(self, message: str, *, code: str = "llm_error", retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class LLMResultError(LLMError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}", code=code, retryable=False)


class LLMHTTPError(LLMError):
    def __init__(
        self,
        provider: str,
        status_code: int,
        body_excerpt: str,
        *,
        retryable: bool,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.body_excerpt = body_excerpt
        super().__init__(
            f"{provider} HTTP {status_code}: {body_excerpt or 'request failed'}",
            code="http_error",
            retryable=retryable,
        )


@dataclass
class LLMResponse:
    raw_text: str
    result_json: dict[str, Any] | None
    usage: dict[str, Any] | None = None


class _ProviderText(str):
    def __new__(cls, value: str, usage: Any = None):
        instance = super().__new__(cls, value)
        def counts_only(item):
            if not isinstance(item, dict):
                return None
            return {key: counts_only(value) if isinstance(value, dict) else value
                    for key, value in item.items()
                    if isinstance(value, (dict, int, float)) and not isinstance(value, bool)}
        instance.usage = counts_only(usage)
        return instance


def parse_json_response(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.S | re.I)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def validate_evaluation_result(
    result: dict[str, Any],
    evaluation_type: str,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise LLMResultError("invalid_object", "LLM 输出必须是 JSON object")

    score = result.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise LLMResultError("invalid_schema", "score 必须是 0 到 100 的数字")

    attention = result.get("attention")
    allowed_attention = {"must_read", "read", "skim", "ignore"}
    if not isinstance(attention, str) or attention not in allowed_attention:
        raise LLMResultError(
            "invalid_schema",
            f"{evaluation_type} 的 attention 必须是 must_read/read/skim/ignore",
        )

    for key in {"novelty", "practical_value", "technical_depth", "reproduction_value"}:
        if key not in result:
            continue
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
            raise LLMResultError("invalid_schema", f"{key} 必须是 0 到 10 的数字")

    for key in {
        "tags",
        "why_interesting",
        "risk_or_limitations",
        "main_findings",
        "strengths",
        "weaknesses",
        "follow_up_questions",
    }:
        if key in result and not isinstance(result[key], list):
            raise LLMResultError("invalid_schema", f"{key} 必须是数组")

    vc = result.get("vc_perspective")
    if vc is not None:
        if not isinstance(vc, dict):
            raise LLMResultError("invalid_schema", "vc_perspective 必须是 JSON object")
        market_relevance = vc.get("market_relevance")
        if market_relevance is not None and (
            isinstance(market_relevance, bool)
            or not isinstance(market_relevance, (int, float))
            or not 0 <= market_relevance <= 10
        ):
            raise LLMResultError("invalid_schema", "market_relevance 必须是 0 到 10 的数字")
        for key in {"startup_opportunities", "investment_risks"}:
            if key in vc and not isinstance(vc[key], list):
                raise LLMResultError("invalid_schema", f"vc_perspective.{key} 必须是数组")

    return result


def call_llm(profile: dict[str, Any], prompt: str, client: httpx.Client | None = None) -> LLMResponse:
    provider = profile["provider"]
    if provider == "openai_compatible":
        raw = _call_openai_compatible(profile, prompt, client=client)
    elif provider == "anthropic":
        raw = _call_anthropic(profile, prompt, client=client)
    else:
        raise LLMError(f"不支持的 LLM provider: {provider}")
    return LLMResponse(raw_text=str(raw), result_json=parse_json_response(raw), usage=getattr(raw, 'usage', None))


def test_profile(profile: dict[str, Any]) -> str:
    response = call_llm(
        profile,
        '请只返回 JSON：{"ok": true, "message": "connection works"}',
    )
    if response.result_json is None:
        raise LLMResultError("invalid_json", "连接测试响应必须是 JSON object")
    return response.raw_text


def _api_key(profile: dict[str, Any]) -> str:
    key = secret_store.decrypt(profile.get("encrypted_api_key_ref"))
    if not key:
        raise LLMError("LLM Profile 未设置 API key")
    return key


def _headers(profile: dict[str, Any]) -> dict[str, str]:
    headers = loads_json(profile.get("custom_headers"), {})
    if not isinstance(headers, dict):
        return {}
    return {str(key): str(value) for key, value in headers.items() if key and value}


def make_llm_client(profile: dict[str, Any]) -> httpx.Client:
    return httpx.Client(**_llm_client_kwargs(profile))


def _llm_client_kwargs(profile: dict[str, Any]) -> dict[str, Any]:
    client_kwargs = {"timeout": float(profile.get("timeout_seconds") or 120)}
    client_kwargs.update(httpx_proxy_kwargs(use_system_proxy=get_bool_setting("llm.trust_env_proxy", False)))
    return client_kwargs


def _call_openai_compatible(
    profile: dict[str, Any],
    prompt: str,
    client: httpx.Client | None = None,
) -> str:
    base_url = str(profile["base_url"]).rstrip("/")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {_api_key(profile)}",
        "Content-Type": "application/json",
    }
    headers.update(_headers(profile))
    payload = {
        "model": profile["model"],
        "messages": [
            {
                "role": "system",
                "content": profile.get('system_prompt') or "You are a careful research paper analyst. Return valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": float(profile['temperature'] if profile.get('temperature') is not None else 0.2),
        "max_tokens": int(profile.get("max_output_tokens") or 2000),
        "response_format": {"type": "json_object"},
    }
    owns_client = client is None
    if client is None:
        client = make_llm_client(profile)
    try:
        response = client.post(url, headers=headers, json=payload)
        if profile.get('allow_response_format_fallback', True) and _response_format_unsupported(response) and "response_format" in payload:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = client.post(url, headers=headers, json=fallback_payload)
        _raise_for_http_status("openai_compatible", response)
        data = _response_json("openai_compatible", response)
    except LLMError:
        raise
    except httpx.HTTPError as exc:
        logger.warning("OpenAI-compatible LLM call failed error_type=%s", type(exc).__name__)
        retryable = isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))
        raise LLMError(str(exc), code="transport_error", retryable=retryable) from exc
    finally:
        if owns_client:
            client.close()

    try:
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("message.content is not text")
        return _ProviderText(content, data.get('usage'))
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            f"无法解析 OpenAI-compatible 响应: {data}",
            code="provider_response",
        ) from exc


def _call_anthropic(
    profile: dict[str, Any],
    prompt: str,
    client: httpx.Client | None = None,
) -> str:
    base_url = str(profile["base_url"]).rstrip("/")
    url = f"{base_url}/messages"
    headers = {
        "x-api-key": _api_key(profile),
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    headers.update(_headers(profile))
    payload = {
        "model": profile["model"],
        "max_tokens": int(profile.get("max_output_tokens") or 2000),
        "temperature": float(profile['temperature'] if profile.get('temperature') is not None else 0.2),
        "system": profile.get('system_prompt') or "You are a careful research paper analyst. Return valid JSON only.",
        "messages": [{"role": "user", "content": prompt}],
    }
    owns_client = client is None
    if client is None:
        client = make_llm_client(profile)
    try:
        response = client.post(url, headers=headers, json=payload)
        _raise_for_http_status("anthropic", response)
        data = _response_json("anthropic", response)
    except LLMError:
        raise
    except httpx.HTTPError as exc:
        logger.warning("Anthropic LLM call failed error_type=%s", type(exc).__name__)
        retryable = isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))
        raise LLMError(str(exc), code="transport_error", retryable=retryable) from exc
    finally:
        if owns_client:
            client.close()

    try:
        parts = data["content"]
        if not isinstance(parts, list):
            raise TypeError("content is not a list")
        return _ProviderText("".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ), data.get('usage'))
    except (KeyError, TypeError) as exc:
        raise LLMError(f"无法解析 Anthropic 响应: {data}", code="provider_response") from exc


def _response_format_unsupported(response: Any) -> bool:
    if int(getattr(response, "status_code", 0)) != 400:
        return False
    text = _response_body_excerpt(response).lower()
    mentions_format = "response_format" in text or "json_object" in text
    unsupported = any(
        marker in text
        for marker in {"not supported", "unsupported", "unknown parameter", "unrecognized"}
    )
    return mentions_format and unsupported


def _response_body_excerpt(response: Any, limit: int = 500) -> str:
    try:
        text = str(response.text or "")
    except Exception:
        text = ""
    if not text:
        try:
            text = json.dumps(response.json(), ensure_ascii=False)
        except Exception:
            text = ""
    cleaned = " ".join(text.split())
    return cleaned[:limit]


def _raise_for_http_status(provider: str, response: Any) -> None:
    status_code = int(getattr(response, "status_code", 0))
    if status_code < 400:
        return
    retryable = status_code in {408, 425, 429} or status_code >= 500
    raise LLMHTTPError(
        provider,
        status_code,
        _response_body_excerpt(response),
        retryable=retryable,
    )


def _response_json(provider: str, response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception as exc:
        raise LLMError(f"{provider} 响应不是合法 JSON", code="provider_response") from exc
    if not isinstance(data, dict):
        raise LLMError(f"{provider} 响应必须是 JSON object", code="provider_response")
    return data
