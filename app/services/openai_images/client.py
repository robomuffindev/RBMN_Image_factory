"""OpenAI GPT Image generation client.

When AppSettings.use_gpt_image is True, the runner calls this instead of
dispatching to ComfyUI. Returns PNG bytes that the runner then writes to
disk (with optional JPG/WebP conversion via PIL, same as the ComfyUI path).

⚠ Each call costs money. The Settings UI shows a warning near the toggle.

API reference: https://platform.openai.com/docs/api-reference/images
"""

from __future__ import annotations

import base64
from pathlib import Path

from app.logging_setup import get_logger
from app.services.debug.llm_logger import log_llm_call

_log = get_logger("factory.openai_images")


async def generate_with_gpt_image(
    *,
    api_key: str,
    prompt: str,
    reference_paths: list[str] | None = None,
    model: str = "gpt-image-2",
    size: str = "auto",
    quality: str = "auto",
    project_id: str | None = None,
    image_id: str | None = None,
) -> bytes:
    """Generate one image via OpenAI's Images API. Returns PNG bytes.

    - If reference_paths is empty/None → uses images.generate (text-to-image).
    - If reference_paths has 1+ entries → uses images.edit with those as inputs.

    Every call is logged through `log_llm_call` for forensics (same place
    as prompt-enhance calls) so you can audit cost and traceback failures.
    """
    if not api_key:
        raise RuntimeError("OpenAI API key not configured — set it in Settings before using GPT Image.")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, timeout=300.0)
    refs = [p for p in (reference_paths or []) if p]
    purpose = "gpt_image_edit" if refs else "gpt_image_generate"

    api_size = size if size != "auto" else "1024x1024"

    with log_llm_call(
        provider="openai_images",
        model=model,
        purpose=purpose,
        system_prompt=f"OpenAI Images API ({purpose}) model={model} size={size} quality={quality}",
        user_message=prompt,
        request_meta={"references": refs, "size": api_size, "quality": quality},
        project_id=project_id,
        image_id=image_id,
    ) as rec:
        if refs:
            # Edit mode with one or more reference images.
            opened: list = []
            try:
                for p in refs[:16]:  # API caps; we don't enforce 4 here
                    fp = Path(p)
                    if not fp.exists():
                        raise FileNotFoundError(f"reference not found locally: {p}")
                    opened.append(fp.open("rb"))
                image_arg = opened if len(opened) > 1 else opened[0]
                resp = await client.images.edit(
                    model=model,
                    image=image_arg,
                    prompt=prompt,
                    n=1,
                    size=api_size,
                )
            finally:
                for fh in opened:
                    try: fh.close()
                    except Exception: pass
        else:
            # Pure text-to-image.
            kwargs: dict = {"model": model, "prompt": prompt, "n": 1, "size": api_size}
            # `quality` is only supported on some models; we pass it best-effort.
            if quality and quality != "auto":
                kwargs["quality"] = quality
            resp = await client.images.generate(**kwargs)

        # Response shape: resp.data[0].b64_json (preferred) or .url
        if not resp.data:
            raise RuntimeError("OpenAI Images returned no data")
        item = resp.data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            png_bytes = base64.b64decode(b64)
        else:
            # Fallback: fetch from URL (older models).
            import httpx
            url = getattr(item, "url", None)
            if not url:
                raise RuntimeError("OpenAI Images returned neither b64_json nor url")
            async with httpx.AsyncClient(timeout=120.0) as http:
                r = await http.get(url)
                r.raise_for_status()
                png_bytes = r.content

        rec.response_text = f"<binary image {len(png_bytes)} bytes>"
        rec.response_meta = {"bytes": len(png_bytes), "size": api_size}
        _log.info("openai_images.ok", purpose=purpose, model=model, size=api_size, bytes=len(png_bytes))
        return png_bytes
