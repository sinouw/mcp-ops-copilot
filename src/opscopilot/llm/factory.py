from __future__ import annotations

from opscopilot.config import Settings
from opscopilot.llm.base import LLMClient
from opscopilot.llm.openai_compat import OpenAICompatibleClient
from opscopilot.llm.scripted import ScriptedLLM


def create_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider in ("scripted", "scripted-hijacked"):
        return ScriptedLLM(
            settings.llm_model if settings.llm_model.startswith("scripted") else "scripted-v1",
            hijacked=settings.llm_provider == "scripted-hijacked",
        )
    return OpenAICompatibleClient(
        provider=settings.llm_provider,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
        timeout_s=settings.llm_timeout_s,
    )
