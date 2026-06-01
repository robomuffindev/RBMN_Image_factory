"""SQLModel schema.

Tables created by ``migrations.create_all()``:
    app_settings   — singleton row (id=1) with user-facing config
    project        — image projects on disk
    image          — one row per queued/generated image (incl. batch rows)
    job            — one row per dispatch attempt (multiple per image on retry)

JSON columns hold complex values (lists, dicts). All timestamps are stored
as ISO-8601 strings for portability.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ImageStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# AppSettings (singleton)
# ---------------------------------------------------------------------------


class AppSettings(SQLModel, table=True):
    __tablename__ = "app_settings"

    id: int = Field(default=1, primary_key=True)

    # ComfyUI
    comfyui_urls: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    comfyui_server_caps: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    # LLM providers
    openai_api_key: str | None = Field(default=None)
    openai_model: str | None = Field(default=None)
    anthropic_api_key: str | None = Field(default=None)
    anthropic_model: str | None = Field(default=None)
    gemini_api_key: str | None = Field(default=None)
    gemini_model: str | None = Field(default=None)
    ollama_base_url: str | None = Field(default=None)
    ollama_model: str | None = Field(default=None)
    default_llm_provider: str | None = Field(default=None)

    # Image generation. Supported model identifiers:
    #   "flux2_klein_9b"  — FLUX.2 Klein 9B via ComfyUI (default)
    #   "qwen_edit_2511"  — Qwen Image Edit 2511 (GGUF) via ComfyUI
    image_model_type: str = Field(default="flux2_klein_9b")
    # GGUF variant for Qwen Image Edit 2511 — picks which quantization
    # the UnetLoaderGGUF node references. Q5_K_S = fastest/smallest,
    # Q8_0 = highest quality. Only used when image_model_type == "qwen_edit_2511".
    qwen_gguf_variant: str = Field(default="Q5_K_S")
    image_system_prompt_overrides: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    image_prompt_guidance: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    # Generation engine toggle. When True, jobs bypass ComfyUI entirely and
    # call OpenAI's GPT Image API. WARNING: this incurs per-image cost.
    use_gpt_image: bool = Field(default=False)
    gpt_image_model: str = Field(default="gpt-image-2")    # gpt-image-1 | gpt-image-2
    gpt_image_size: str = Field(default="auto")            # auto | 1024x1024 | 1024x1536 | 1536x1024
    gpt_image_quality: str = Field(default="auto")         # auto | low | medium | high

    # When True, /commit endpoints write Image rows but skip the dispatcher
    # enqueue. User clicks "Start Queue" in the UI to release everything.
    dispatch_paused: bool = Field(default=False)

    # "Frame the main subject prominently" — augments the prompt + negative
    # prompt with composition scaffolding so the subject doesn't end up tiny
    # in the frame. Per-image override is on Image.frame_subject.
    frame_subject_default: bool = Field(default=False)
    frame_subject_strength: str = Field(default="moderate")  # subtle | moderate | strong
    # Optional advanced overrides — when set, replace the built-in scaffolds.
    frame_subject_positive_override: str | None = Field(default=None)
    frame_subject_negative_override: str | None = Field(default=None)

    # Defaults
    default_width: int = Field(default=1024)
    default_height: int = Field(default=1024)
    default_seed_mode: str = Field(default="random")
    max_parallel_per_worker: int = Field(default=1)
    restrict_explicit_content: bool = Field(default=False)

    # Project
    project_dir: str | None = Field(default=None)

    # Bookkeeping
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Domain tables
# ---------------------------------------------------------------------------


def _new_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Project(SQLModel, table=True):
    __tablename__ = "project"

    id: str = Field(default_factory=_new_id, primary_key=True)
    name: str
    description: str = Field(default="")
    folder_path: str
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    image_count: int = Field(default=0)
    status: str = Field(default="active")  # active | archived
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class Image(SQLModel, table=True):
    __tablename__ = "image"

    id: str = Field(default_factory=_new_id, primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    order_index: int = Field(default=0)

    name: str = Field(default="")
    prompt: str = Field(default="")
    enhanced_prompt: str | None = Field(default=None)
    negative_prompt: str = Field(default="")

    reference_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    width: int = Field(default=1024)
    height: int = Field(default=1024)
    seed: int | None = Field(default=None)

    status: ImageStatus = Field(default=ImageStatus.QUEUED)
    output_path: str | None = Field(default=None)
    thumbnail_path: str | None = Field(default=None)

    # Output controls (Batch Render).
    output_format: str = Field(default="png")          # "png" | "jpg" | "webp"
    retain_ref1_name: bool = Field(default=False)      # if true, output filename = stem(ref1) + ext
    custom_output_dir: str | None = Field(default=None)  # absolute path; if set, file goes there
    batch_run_id: str | None = Field(default=None)     # groups Images that came from one Batch Render commit
    # Per-image override for the framing toggle. NULL → inherit AppSettings.frame_subject_default.
    # "on" / "off" — keep as string so NULL can mean "use global default".
    frame_subject: str | None = Field(default=None)
    # When non-NULL, this image is an "edit candidate" generated for another
    # image's lightbox-edit flow. Candidates are hidden from the main gallery
    # listing — they only appear under their parent image's Edit panel.
    # On "Use this" promotion: parent's file goes to <dir>/_replaced/ with a
    # serial suffix and the candidate's content replaces it; the candidate
    # row itself is then deleted.
    edit_of_image_id: str | None = Field(default=None, index=True)

    # Scale hints — optional CSV/JSON import columns that help the model
    # render the subject at the correct real-world scale relative to the
    # surrounding environment. Common failure mode without these: a small
    # accessory (e.g. a 3" water spout) is generated huge because the model
    # has no anchor for its true size. All three are free-form text and the
    # runner weaves them into the prompt before submitting to ComfyUI.
    #   physical_size:       absolute size text, e.g. "3 inches tall", "10 cm wide"
    #   physical_dimensions: explicit WxHxD, e.g. "5x3x2 inches"
    #   relative_size:       comparison anchor, e.g. "size of a smartphone",
    #                                              "fits in palm of a hand"
    physical_size: str | None = Field(default=None)
    physical_dimensions: str | None = Field(default=None)
    relative_size: str | None = Field(default=None)

    parameters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = Field(default=None)
    created_at: str = Field(default_factory=_now_iso)
    completed_at: str | None = Field(default=None)


class Job(SQLModel, table=True):
    __tablename__ = "job"

    id: str = Field(default_factory=_new_id, primary_key=True)
    image_id: str = Field(foreign_key="image.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)

    status: JobStatus = Field(default=JobStatus.PENDING)
    priority: int = Field(default=0)
    worker_url: str | None = Field(default=None)
    prompt_id: str | None = Field(default=None)
    retry_count: int = Field(default=0)
    error: str | None = Field(default=None)

    parameters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: str = Field(default_factory=_now_iso)
    started_at: str | None = Field(default=None)
    completed_at: str | None = Field(default=None)


class LlmCallLog(SQLModel, table=True):
    __tablename__ = "llm_call_log"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    provider: str = Field(index=True)
    model: str = Field(index=True)
    purpose: str = Field(default="")
    project_id: str | None = Field(default=None, index=True)
    image_id: str | None = Field(default=None, index=True)
    request_summary: str = Field(default="")
    response_summary: str = Field(default="")
    input_tokens: int | None = Field(default=None)
    output_tokens: int | None = Field(default=None)
    cost_estimate_usd: float | None = Field(default=None)
    error: str | None = Field(default=None)
    duration_ms: int | None = Field(default=None)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
