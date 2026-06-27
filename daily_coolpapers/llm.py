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
    pass


@dataclass
class LLMResponse:
    raw_text: str
    result_json: dict[str, Any] | None


def parse_json_response(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.S | re.I)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def call_llm(profile: dict[str, Any], prompt: str, client: httpx.Client | None = None) -> LLMResponse:
    provider = profile["provider"]
    if provider == "openai_compatible":
        raw = _call_openai_compatible(profile, prompt, client=client)
    elif provider == "anthropic":
        raw = _call_anthropic(profile, prompt, client=client)
    else:
        raise LLMError(f"不支持的 LLM provider: {provider}")
    return LLMResponse(raw_text=raw, result_json=parse_json_response(raw))


def test_profile(profile: dict[str, Any]) -> str:
    response = call_llm(
        profile,
        '请只返回 JSON：{"ok": true, "message": "connection works"}',
    )
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
                "content": "You are a careful research paper analyst. Return valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": float(profile.get("temperature") or 0.2),
        "max_tokens": int(profile.get("max_output_tokens") or 2000),
        "response_format": {"type": "json_object"},
    }
    owns_client = client is None
    if client is None:
        client = make_llm_client(profile)
    try:
        response = client.post(url, headers=headers, json=payload)
        if response.status_code >= 400 and "response_format" in payload:
            payload.pop("response_format", None)
            response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        logger.exception("OpenAI-compatible LLM call failed")
        raise LLMError(str(exc)) from exc
    finally:
        if owns_client:
            client.close()

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"无法解析 OpenAI-compatible 响应: {data}") from exc


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
        "temperature": float(profile.get("temperature") or 0.2),
        "system": "You are a careful research paper analyst. Return valid JSON only.",
        "messages": [{"role": "user", "content": prompt}],
    }
    owns_client = client is None
    if client is None:
        client = make_llm_client(profile)
    try:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        logger.exception("Anthropic LLM call failed")
        raise LLMError(str(exc)) from exc
    finally:
        if owns_client:
            client.close()

    try:
        parts = data["content"]
        return "".join(part.get("text", "") for part in parts if part.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise LLMError(f"无法解析 Anthropic 响应: {data}") from exc
