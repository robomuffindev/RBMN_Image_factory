"""Multi-provider LLM prompt enhancer.

All four providers (OpenAI, Anthropic, Gemini, Ollama) implement the same
interface via ``enhance(prompt, ..., provider=...)``. Every call is wrapped
in the ``log_llm_call`` context manager so the full prompt + response lands
in ``data/logs/llm/<date>/*.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.logging_setup import get_logger
from app.services.debug.llm_logger import log_llm_call

_log = get_logger("factory.llm")

_PREFIX_RE = re.compile(
    r"^\s*(enhanced prompt|here is the enhanced prompt|here's the enhanced prompt|rewritten prompt)\s*[:\-]\s*",
    re.IGNORECASE,
)


def _post(text: str) -> str:
    text = (text or "").strip()
    # Strip "Enhanced prompt:" style prefixes.
    text = _PREFIX_RE.sub("", text)
    # Strip surrounding quotes.
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    # Collapse to single paragraph.
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass
class EnhanceResult:
    enhanced: str
    provider: str
    model: str
    tokens_input: int | None = None
    tokens_output: int | None = None
    latency_ms: float | None = None


def _resolve(provider: str | None, settings_row: Any) -> tuple[str, str | None, str | None]:
    """Return (provider, api_key, model) per resolution rule from devnotes."""
    candidates = ["openai", "anthropic", "gemini", "ollama"]
    if provider:
        candidates = [provider] + [p for p in candidates if p != provider]
    if settings_row.default_llm_provider:
        if settings_row.default_llm_provider in candidates:
            candidates.remove(settings_row.default_llm_provider)
        candidates.insert(0, settings_row.default_llm_provider)
    for p in candidates:
        key = getattr(settings_row, f"{p}_api_key", None) if p != "ollama" else "n/a"
        model = getattr(settings_row, f"{p}_model", None)
        if p == "ollama" and getattr(settings_row, "ollama_base_url", None):
            return "ollama", None, model
        if p != "ollama" and key:
            return p, key, model
    return "ollama", None, getattr(settings_row, "ollama_model", None)


async def enhance(
    prompt: str,
    *,
    settings_row: Any,
    purpose: str = "enhance_image",
    project_id: str | None = None,
    image_id: str | None = None,
    context: str = "",
    provider_override: str | None = None,
) -> EnhanceResult:
    """Enhance a single prompt. Picks provider per priority chain."""
    provider, api_key, model = _resolve(provider_override, settings_row)
    if not model:
        model = {
            "openai": "gpt-4o",
            "anthropic": "claude-sonnet-4-5",
            "gemini": "gemini-2.0-flash",
            "ollama": "llama3.1:8b",
        }.get(provider, "gpt-4o")

    from app.services.llm.prompts import resolve_system_prompt

    sys_prompt = resolve_system_prompt(
        "flux2_klein_9b",
        "image",
        settings_row.image_system_prompt_overrides,
        settings_row.image_prompt_guidance,
    )
    # If the global framing default is on, append a framing instruction to the
    # system prompt so the LLM rewrites with composition language baked in
    # (the runner still applies a scaffold as backstop). Per-image override
    # isn't available here — this function is called pre-row in batch flows —
    # so we only honor the global default. Override always wins via scaffold.
    try:
        if bool(getattr(settings_row, "frame_subject_default", False)):
            from app.services.framing import enhancer_instruction
            sys_prompt = sys_prompt + "\n\n" + enhancer_instruction(
                getattr(settings_row, "frame_subject_strength", "moderate") or "moderate"
            )
    except Exception:
        pass
    user_msg = f"Original prompt: {prompt}" if prompt else "No prompt provided. Generate one from the context below."
    if context:
        user_msg += f"\n\nContext: {context}"

    with log_llm_call(
        provider=provider,
        model=model,
        purpose=purpose,
        system_prompt=sys_prompt,
        user_message=user_msg,
        request_meta={"temperature": 0.7, "max_tokens": 300},
        project_id=project_id,
        image_id=image_id,
    ) as rec:
        if provider == "openai":
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, timeout=45.0)
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.7,
                max_tokens=300,
            )
            rec.response_text = resp.choices[0].message.content or ""
            if resp.usage:
                rec.tokens_input = resp.usage.prompt_tokens
                rec.tokens_output = resp.usage.completion_tokens
        elif provider == "anthropic":
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=45.0)
            resp = await client.messages.create(
                model=model,
                system=sys_prompt,
                max_tokens=300,
                temperature=0.7,
                messages=[{"role": "user", "content": user_msg}],
            )
            rec.response_text = "".join(block.text for block in resp.content if hasattr(block, "text"))
            if hasattr(resp, "usage") and resp.usage:
                rec.tokens_input = resp.usage.input_tokens
                rec.tokens_output = resp.usage.output_tokens
        elif provider == "gemini":
            import asyncio

            from google import genai

            client = genai.Client(api_key=api_key)

            def _call() -> str:
                resp = client.models.generate_content(
                    model=model,
                    contents=user_msg,
                    config={"temperature": 0.7, "max_output_tokens": 300, "system_instruction": sys_prompt},
                )
                return getattr(resp, "text", "") or ""

            rec.response_text = await asyncio.to_thread(_call)
        elif provider == "ollama":
            base = getattr(settings_row, "ollama_base_url", None) or "http://localhost:11434"
            async with httpx.AsyncClient(timeout=60.0) as http:
                r = await http.post(
                    f"{base.rstrip('/')}/api/chat",
                    json={
                        "model": model,
                        "stream": False,
                        "options": {"temperature": 0.7, "num_predict": 300},
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                    },
                )
                r.raise_for_status()
                data = r.json()
                rec.response_text = (data.get("message") or {}).get("content", "")
                rec.response_meta = {"eval_count": data.get("eval_count"), "prompt_eval_count": data.get("prompt_eval_count")}
                rec.tokens_input = data.get("prompt_eval_count")
                rec.tokens_output = data.get("eval_count")
        else:
            raise ValueError(f"unknown provider: {provider}")

    enhanced = _post(rec.response_text)
    return EnhanceResult(
        enhanced=enhanced,
        provider=provider,
        model=model,
        tokens_input=rec.tokens_input,
        tokens_output=rec.tokens_output,
        latency_ms=rec.latency_ms,
    )
