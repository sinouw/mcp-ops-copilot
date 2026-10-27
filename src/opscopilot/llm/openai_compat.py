"""One adapter for every OpenAI-compatible Chat Completions endpoint.

OpenAI, Groq and Gemini (via its OpenAI compatibility layer) all accept the same request
shape for tool calling, so a single ~100 line adapter covers them; provider differences are
reduced to a base URL preset.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from opscopilot.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage

BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


def _to_wire(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in m.tool_calls
        ]
    if m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    return out


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__invalid_json__": str(raw)[:500]}
    return parsed if isinstance(parsed, dict) else {"__invalid_json__": str(raw)[:500]}


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str = "",
        temperature: float = 0.0,
        timeout_s: float = 60.0,
        max_retries: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = base_url or BASE_URLS.get(provider, "")
        if not url:
            raise LLMError(f"no base URL for provider {provider!r}; set LLM_BASE_URL")
        if not api_key:
            raise LLMError(f"LLM_API_KEY is required for provider {provider!r}")
        self.provider, self.model = provider, model
        self._temperature, self._max_retries = temperature, max_retries
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_wire(m) for m in messages],
            "temperature": self._temperature,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
        data = await self._post(body)
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"malformed response: {str(data)[:300]}") from exc
        calls = [
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=tc["function"]["name"],
                arguments=_parse_args(tc["function"].get("arguments")),
            )
            for i, tc in enumerate(choice.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        return LLMResponse(
            message=Message(
                role="assistant", content=choice.get("content") or "", tool_calls=calls
            ),
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            model=data.get("model", self.model),
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                last = exc
            else:
                if resp.status_code < 400:
                    result: dict[str, Any] = resp.json()
                    return result
                if resp.status_code not in _RETRYABLE:
                    raise LLMError(f"{self.provider} HTTP {resp.status_code}: {resp.text[:300]}")
                last = LLMError(f"{self.provider} HTTP {resp.status_code}")
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(min(float(retry_after), 30.0))
                    continue
            await asyncio.sleep(min(2**attempt + random.random(), 20.0))
        raise LLMError(f"{self.provider} request failed after retries: {last}")
