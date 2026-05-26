"""Prompt-framing helper.

Augments prompts to keep the main subject prominent in the frame — avoiding the
"product is a tiny dot in a vast background" failure mode that Klein / GPT Image
both produce when refs have varying compositions.

The user toggles this globally in Settings (`frame_subject_default`), with three
strength levels and optional custom overrides. Per-image override lives on
`Image.frame_subject` ("on" | "off" | None=inherit).

Strategy:
    final_prompt   = original_prompt + ", " + positive_scaffold
    final_negative = original_negative + ", " + negative_scaffold

The same helper is also fed to the LLM enhancer's system prompt so AI-enhanced
prompts already include framing language at the front (cleaner phrasing) and
the scaffold is added as a backstop in case the LLM ignored it.
"""
from __future__ import annotations

from dataclasses import dataclass


# Built-in scaffolds, keyed by strength. Crafted to be model-agnostic — these
# read as composition language any T2I model honors.
SCAFFOLDS: dict[str, dict[str, str]] = {
    "subtle": {
        "positive": (
            "medium framing with the subject clearly visible and centered, "
            "subject occupies a meaningful portion of the frame"
        ),
        "negative": (
            "tiny subject, distant view, excessive empty background, "
            "subject lost in scene"
        ),
    },
    "moderate": {
        "positive": (
            "hero composition with the main subject prominently displayed and "
            "centered in the frame, subject fills approximately 60-70 percent "
            "of the image, medium close-up, clear focus on subject, "
            "minimal background clutter"
        ),
        "negative": (
            "wide shot, distant view, tiny subject, miniature, far away, "
            "excessive background, subject small in frame, zoomed out, "
            "subject lost in scene, low subject-to-frame ratio"
        ),
    },
    "strong": {
        "positive": (
            "dominant hero composition, the main subject fills the majority of "
            "the frame, large prominent product shot, tight close-up framing, "
            "subject takes up 75-85 percent of the image, centered, sharp focus "
            "on subject, minimal negative space, no excessive background"
        ),
        "negative": (
            "wide shot, distant view, tiny subject, miniature, far away, "
            "small in frame, zoomed out, excessive background, lots of empty "
            "space, subject lost in scene, environmental shot, landscape framing"
        ),
    },
}


@dataclass(frozen=True)
class FramedPrompt:
    prompt: str
    negative: str
    applied: bool
    strength: str | None


def is_enabled(per_image: str | None, global_default: bool) -> bool:
    """Resolve per-image override against the global default.

    per_image:
        "on"   -> True  (force on regardless of default)
        "off"  -> False (force off regardless of default)
        None / "" -> inherit
    """
    if per_image == "on":
        return True
    if per_image == "off":
        return False
    return bool(global_default)


def apply_framing(
    prompt: str,
    negative: str,
    *,
    enabled: bool,
    strength: str = "moderate",
    positive_override: str | None = None,
    negative_override: str | None = None,
) -> FramedPrompt:
    """Return a FramedPrompt with scaffolds applied if `enabled` is True.

    Idempotent-ish: if the scaffold text is already present in the prompt,
    we don't append it again (best-effort, simple substring check).
    """
    base_prompt = (prompt or "").strip()
    base_neg = (negative or "").strip()
    if not enabled:
        return FramedPrompt(prompt=base_prompt, negative=base_neg, applied=False, strength=None)

    s = (strength or "moderate").lower()
    if s not in SCAFFOLDS:
        s = "moderate"
    pos = (positive_override or SCAFFOLDS[s]["positive"]).strip()
    neg = (negative_override or SCAFFOLDS[s]["negative"]).strip()

    # Only append if not already present — avoids prompt bloat on retries.
    if pos and pos.lower() not in base_prompt.lower():
        new_prompt = base_prompt + (", " if base_prompt and not base_prompt.endswith((",", ".")) else " ") + pos
    else:
        new_prompt = base_prompt
    if neg and neg.lower() not in base_neg.lower():
        new_neg = (base_neg + ", " if base_neg else "") + neg
    else:
        new_neg = base_neg
    return FramedPrompt(prompt=new_prompt.strip(), negative=new_neg.strip(), applied=True, strength=s)


def enhancer_instruction(strength: str = "moderate") -> str:
    """Extra system-prompt directive for the LLM enhancer when framing is on.

    Keeps the LLM-enhanced prompt itself framing-aware so the final text reads
    naturally instead of being a hand-written tail tacked onto a paragraph.
    """
    s = (strength or "moderate").lower()
    if s == "subtle":
        return (
            "When rewriting, make sure the main product or subject is clearly "
            "the focal point of the described scene — not lost in surrounding detail."
        )
    if s == "strong":
        return (
            "When rewriting, frame the main product or subject as a dominant "
            "hero element that fills most of the frame. Use composition language "
            "like 'close-up', 'large in frame', 'centered', and avoid describing "
            "expansive backgrounds or distant viewpoints."
        )
    # moderate (default)
    return (
        "When rewriting, ensure the main product or subject is prominently "
        "framed and clearly the focus — centered, medium-close composition, "
        "filling a substantial portion of the frame. Avoid wide or distant shots."
    )
