"""Built-in system prompts for image generation.

Three-tier resolution (per devnotes):
    1. User override (from AppSettings.image_system_prompt_overrides)
    2. Model-specific built-in
    3. Generic default

Per-model prompt guidance is appended to the system prompt with a header.
"""

from __future__ import annotations

_KLEIN_IMAGE = """You are a prompt engineer optimizing prompts for FLUX.2 Klein 9B.

Rules:
- Output ONE single paragraph. No bullets, headers, or line breaks.
- Front-load the structure: subject → action → environment → lighting → mood → camera.
- When reference images are provided, use COMPOSITIONAL language ("the subject from the first image", "matching the style of the second reference"). NEVER write literal tags like "Image 1" or "Image 2".
- 40 to 150 words.
- Do not include any text, captions, words, letters, watermarks, or signage in your description.
- Translate metaphors and abstract concepts into concrete, photographable visuals.
- If no user prompt is provided, invent one from the context (lyrics, concept, characters).

Output only the prompt itself. No prefixes like "Enhanced prompt:" — just the prompt body.
"""

_GENERIC_IMAGE = """You are an image-prompt engineer. Rewrite the user's prompt so it's vivid, concrete, and physically plausible. Keep it to a single paragraph (40-150 words). Front-load subject and action. Don't add captions, text, or watermarks.

Output only the prompt itself."""


BUILTIN_SYSTEM_PROMPTS: dict[str, dict[str, str]] = {
    "flux2_klein_9b": {"image": _KLEIN_IMAGE},
}


def get_builtin(model: str, type_: str = "image") -> str | None:
    return (BUILTIN_SYSTEM_PROMPTS.get(model) or {}).get(type_)


def resolve_system_prompt(
    model: str,
    type_: str,
    overrides: dict[str, dict] | None,
    guidance: dict[str, str] | None,
) -> str:
    text: str | None = None
    if overrides:
        entry = overrides.get(model) or {}
        if entry.get("enabled") and entry.get("text"):
            text = entry["text"]
    if not text:
        text = get_builtin(model, type_)
    if not text:
        text = _GENERIC_IMAGE
    if guidance and guidance.get(model):
        text = text + "\n\nADDITIONAL PROMPT RULES AND GUIDANCE FROM USER:\n" + guidance[model]
    return text
