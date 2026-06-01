"""Workflow JSON preparation utilities.

The general approach: identify nodes by their ``_meta.title`` (resilient
across ComfyUI re-saves) and patch their inputs at runtime.

For Klein image generation the relevant titles are:
    "CLIP Text Encode (Positive Prompt)"   — main prompt
    "Width" / "Height"                     — dimensions
    "RandomNoise"                          — seed
    "Load Image"                           — ref 1
    "Reference 2 Image" / "Reference 3" / "Reference 4"
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

_log = get_logger("factory.comfy.workflow")

ANTI_TEXT_SUFFIX = ", no text, no subtitles, no captions, no words, no letters, no watermarks"

REF_TITLES = ("Load Image", "Reference 2 Image", "Reference 3", "Reference 4")


def load_workflow(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def find_node_by_title(workflow: dict[str, Any], title: str) -> tuple[str | None, dict[str, Any] | None]:
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta = node.get("_meta") or {}
        if meta.get("title") == title:
            return node_id, node
    return None, None


def set_node_input(workflow: dict[str, Any], title: str, input_name: str, value: Any) -> bool:
    _, node = find_node_by_title(workflow, title)
    if not node:
        return False
    node.setdefault("inputs", {})[input_name] = value
    return True


def find_nodes_by_class_type(workflow: dict[str, Any], class_type: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") == class_type:
            out.append((node_id, node))
    return out


# ---------------------------------------------------------------------------
# Klein workflow preparation
# ---------------------------------------------------------------------------


def prepare_klein_workflow(
    workflow_path: str | Path,
    prompt: str,
    width: int,
    height: int,
    seed: int,
    ref_images: list[str] | None = None,
    negative_prompt: str = "",
) -> dict[str, Any]:
    """Load a Klein workflow JSON and patch it for this generation."""
    workflow = load_workflow(workflow_path)
    full_prompt = (prompt or "").strip()
    if ANTI_TEXT_SUFFIX.strip(", ") not in full_prompt:
        full_prompt = full_prompt + ANTI_TEXT_SUFFIX

    set_node_input(workflow, "CLIP Text Encode (Positive Prompt)", "text", full_prompt)
    set_node_input(workflow, "CLIP Text Encode (Negative Prompt)", "text", negative_prompt)
    set_node_input(workflow, "RandomNoise", "noise_seed", int(seed))

    # ---- Output size: patch the EmptyLatentImage that feeds the sampler ----
    # The Klein workflows ship with an EmptyFlux2LatentImage node titled
    # "Empty Latent" whose `width` and `height` direct-input the sampler.
    # We previously tried setting nodes titled "Width" / "Height" which
    # don't exist in these workflows — so the configured resolution was
    # silently ignored and outputs came out at the workflow's default size
    # (or at the size derived from the reference image's aspect ratio).
    # Patching the latent node directly fixes this.
    w_int, h_int = int(width), int(height)
    patched = False
    # Prefer setting by title (exact match the official workflow uses).
    if set_node_input(workflow, "Empty Latent", "width", w_int):
        set_node_input(workflow, "Empty Latent", "height", h_int)
        patched = True
    # Fall back to class_type lookup so renamed nodes still work.
    if not patched:
        for class_type in ("EmptyFlux2LatentImage", "EmptySD3LatentImage", "EmptyLatentImage"):
            for _nid, _node in find_nodes_by_class_type(workflow, class_type):
                _node.setdefault("inputs", {})["width"] = w_int
                _node.setdefault("inputs", {})["height"] = h_int
                patched = True
    if not patched:
        _log.warning("comfy.workflow.size_not_patched",
                     width=w_int, height=h_int,
                     hint="no EmptyLatent / EmptyFlux2LatentImage node found in workflow")
    else:
        _log.info("comfy.workflow.size_patched", width=w_int, height=h_int)

    # The "Scale Ref 1..4" nodes resize each reference image to roughly 1MP
    # while preserving aspect. They DO NOT control the output size — the
    # EmptyLatent above does. Leave megapixels at the workflow default
    # (typically 1.0) so refs stay in a sane VRAM footprint.

    ref_images = ref_images or []
    for i, ref in enumerate(ref_images[:4]):
        if not ref:
            continue
        set_node_input(workflow, REF_TITLES[i], "image", ref)

    return workflow


# ---------------------------------------------------------------------------
# Graph fixups (apply to all workflows)
# ---------------------------------------------------------------------------


_GROUP_ID_RE = re.compile(r"^(\d+):(\d+)$")


def flatten_group_nodes(workflow: dict[str, Any]) -> dict[str, Any]:
    """Convert ``"1217:1089"`` composite IDs to ``"1217_1089"``."""
    mapping: dict[str, str] = {}
    for node_id in list(workflow.keys()):
        m = _GROUP_ID_RE.match(node_id)
        if m:
            new_id = f"{m.group(1)}_{m.group(2)}"
            mapping[node_id] = new_id
    if not mapping:
        return workflow
    flat: dict[str, Any] = {}
    for node_id, node in workflow.items():
        new_id = mapping.get(node_id, node_id)
        if isinstance(node, dict):
            inputs = node.get("inputs", {})
            for k, v in list(inputs.items()):
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in mapping:
                    inputs[k] = [mapping[v[0]], v[1]]
        flat[new_id] = node
    _log.info("comfy.workflow.flattened", count=len(mapping))
    return flat


def remove_missing_nodes(workflow: dict[str, Any], available_types: set[str]) -> list[str]:
    """Remove nodes whose ``class_type`` isn't on the server. Returns the list removed."""
    removed: list[str] = []
    for node_id in list(workflow.keys()):
        node = workflow[node_id]
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if ct and ct not in available_types:
            removed.append(f"{node_id}({ct})")
            # Rewire: anyone pointing at this node — null their input.
            for other in workflow.values():
                if not isinstance(other, dict):
                    continue
                inp = other.get("inputs", {})
                for k, v in list(inp.items()):
                    if isinstance(v, list) and len(v) == 2 and v[0] == node_id:
                        inp.pop(k, None)
            workflow.pop(node_id, None)
    if removed:
        _log.warning("comfy.workflow.removed_missing", removed=removed)
    return removed


def stamp_unique_filename_prefix(workflow: dict[str, Any], prefix: str) -> None:
    """Set a unique ``filename_prefix`` on all SaveImage nodes for fallback download."""
    for _id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") in ("SaveImage", "Image Save"):
            node.setdefault("inputs", {})["filename_prefix"] = prefix


# ---------------------------------------------------------------------------
# Pick the right Klein workflow file given ref count
# ---------------------------------------------------------------------------


def klein_workflow_path_for_refs(workflows_dir: str | Path, ref_count: int) -> Path:
    base = Path(workflows_dir)
    ref_count = max(0, min(ref_count, 4))
    fname = "klein_t2i.json" if ref_count == 0 else f"klein_{ref_count}ref.json"
    return base / fname


def qwen_workflow_path_for_refs(workflows_dir: str | Path, ref_count: int) -> Path:
    """Pick the right Qwen Image Edit 2511 workflow JSON for the given
    number of references. Falls back to t2i if no refs provided."""
    base = Path(workflows_dir)
    ref_count = max(0, min(ref_count, 4))
    fname = "qwen_t2i.json" if ref_count == 0 else f"qwen_{ref_count}ref.json"
    return base / fname


def workflow_path_for_model(workflows_dir: str | Path, model_type: str, ref_count: int) -> Path:
    """Resolve the workflow JSON for the configured model + ref count."""
    mt = (model_type or "flux2_klein_9b").lower()
    if mt == "qwen_edit_2511":
        return qwen_workflow_path_for_refs(workflows_dir, ref_count)
    return klein_workflow_path_for_refs(workflows_dir, ref_count)


def prepare_qwen_workflow(
    workflow_path: str | Path,
    prompt: str,
    width: int,
    height: int,
    seed: int,
    ref_images: list[str] | None = None,
    negative_prompt: str = "",
    gguf_variant: str = "Q5_K_S",
) -> dict[str, Any]:
    """Load a Qwen Image Edit 2511 workflow JSON and patch it for this generation.

    GGUF UNet variant is selected via AppSettings.qwen_gguf_variant. References
    are wired into TextEncodeQwenImageEditPlus.image1..N and (for the 1-ref+
    case) the first ref also feeds VAEEncode → KSampler.latent_image.
    Qwen Image Edit Plus uses the same conditioning for + and - so the
    negative prompt is logged and ignored at the workflow level.
    """
    workflow = load_workflow(workflow_path)
    full_prompt = (prompt or "").strip()
    if ANTI_TEXT_SUFFIX.strip(", ") not in full_prompt:
        full_prompt = full_prompt + ANTI_TEXT_SUFFIX

    # Prompt → PrimitiveStringMultiline (title "Prompt").
    if not set_node_input(workflow, "Prompt", "value", full_prompt):
        for _nid, _node in find_nodes_by_class_type(workflow, "PrimitiveStringMultiline"):
            _node.setdefault("inputs", {})["value"] = full_prompt

    # Seed → KSampler.seed.
    for _nid, _node in find_nodes_by_class_type(workflow, "KSampler"):
        _node.setdefault("inputs", {})["seed"] = int(seed)

    # GGUF variant → UnetLoaderGGUF.unet_name.
    gv = (gguf_variant or "Q5_K_S").strip()
    unet_name = gv if gv.endswith(".gguf") else f"qwen-image-edit-2511-{gv}.gguf"
    for _nid, _node in find_nodes_by_class_type(workflow, "UnetLoaderGGUF"):
        _node.setdefault("inputs", {})["unet_name"] = unet_name

    # Output size — patch EmptySD3LatentImage even on i2i variants.
    w_int, h_int = int(width), int(height)
    patched = False
    for ct in ("EmptySD3LatentImage", "EmptyLatentImage"):
        for _nid, _node in find_nodes_by_class_type(workflow, ct):
            _node.setdefault("inputs", {})["width"] = w_int
            _node.setdefault("inputs", {})["height"] = h_int
            patched = True
    if patched:
        _log.info("comfy.workflow.size_patched", width=w_int, height=h_int, engine="qwen")

    # References → LoadImage (titled "Load Ref 1..4").
    ref_images = ref_images or []
    for i, ref in enumerate(ref_images[:4]):
        if not ref:
            continue
        set_node_input(workflow, f"Load Ref {i+1}", "image", ref)

    if negative_prompt:
        _log.info("comfy.workflow.qwen_negative_ignored",
                  prompt_len=len(negative_prompt),
                  reason="TextEncodeQwenImageEditPlus has no separate negative input")

    return workflow
