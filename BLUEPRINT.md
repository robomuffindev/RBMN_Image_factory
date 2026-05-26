# Robomuffin Image Factory — Architecture Blueprint

> Living document. Update whenever an architectural decision changes.
> Sibling docs: [README.md](README.md) · [CHECKLIST.md](CHECKLIST.md) · [CHANGELOG.md](CHANGELOG.md) · [DECISIONS.md](DECISIONS.md)
>
> **Status snapshot (2026-05-25):** Phase 1–10 plus the following post-build
> work all shipped:
> - **Batch Render** tab with output format (PNG/JPG/WebP), custom output
>   dir, retain-ref1-name, optional per-prompt LLM enhancement, reference
>   base directory with smart fallback resolution (base → base/uploads →
>   parent → project uploads → global uploads), preflight diagnostics.
> - **Batches** management tab + endpoints (cancel-queued / retry-failed /
>   retry-all / clear-failed / delete-batch / project-wide clear-failed).
> - **Uploaded Assets** tab + reference-picker modal.
> - **GPT Image 2** alternative engine (`AppSettings.use_gpt_image`).
> - **Subject framing** scaffolds (`app/services/framing.py`) with global
>   default + per-batch override + Subtle/Moderate/Strong presets +
>   LLM-enhancer integration.
> - **Pause / Stop / Start queue controls** with `dispatch_paused` setting,
>   hard-stop drain + ComfyUI `/interrupt`.
> - **Parallel dispatch** across all healthy ComfyUI workers (async-task
>   fan-out, `FIRST_COMPLETED` re-dispatch loop).
> - **Dispatcher** auto-configured at app boot from DB, periodic 30s
>   re-probe, manual `/api/workers/probe` endpoint, worker health strip in UI.
> - **Output size fix** — `prepare_klein_workflow` now patches
>   `EmptyFlux2LatentImage` directly with literal width/height instead of
>   relying on a workflow title that doesn't exist.
> - **References** rendered on every image card with green/red existence
>   flags via `references_detail`.
> - **Live gallery** with SSE auto-reconnect + 8s polling fallback +
>   Page-Visibility refresh, **status-sorted cards** (running → done →
>   failed → queued).
> - **Per-job log files**, JS error capture, dispatcher hot-reconfigure on
>   settings save, WebP→PNG reference normalization, masked-API-key
>   preservation, hardened ref-path scrubbing (BOM, NBSP, smart quotes).

---

## 1. Mission

A Python web app that generates and edits images via remote ComfyUI servers running **FLUX.2 Klein 9B**, with:

- Up to **4 reference images** per generation.
- **Image projects** with persistent storage on disk.
- **Batch mode** driven by CSV / JSON import (one row = one image).
- **AI prompt enhancement** (per-image and bulk) using LLM providers.
- A **queue + dispatcher** that pushes as many parallel jobs as there are healthy ComfyUI workers.
- A **lightbox gallery**, individual + per-project downloads.
- A **status panel** with progress, ETA, and a live debug console.
- One-click **installer.bat** / **run.bat** + an in-depth README.

This is the image-only sibling of the existing RBMN Storyboard App; we are deliberately reusing the patterns documented in `/devnotes/` but slimming down (no video, no audio, no Whisper, no scene timeline).

---

## 2. Stack (locked decisions)

| Layer | Choice | Why |
|---|---|---|
| Runtime | **Python 3.11+ in a local `venv`** | Self-contained install per project instructions. |
| Web framework | **FastAPI + Uvicorn** | Async, first-class SSE/streaming, type hints. |
| Frontend | **Server-rendered HTML + HTMX + Alpine.js + Tailwind (CDN)** | No Node build step, fits a focused tool, faster to iterate than React. |
| Realtime | **SSE (`text/event-stream`)** | Unidirectional progress fits the queue model; matches devnotes pattern. |
| Persistence | **SQLite (WAL) + filesystem project folders** | DB for queue/jobs/projects/settings; raw images & metadata on disk for portability. |
| Async DB | **`aiosqlite` via SQLModel/SQLAlchemy 2.x** | Mirrors devnotes pattern. |
| ComfyUI I/O | `requests` (HTTP) + `websocket-client` (WS) | Same libraries devnotes used; battle-tested. |
| LLM providers | `openai`, `anthropic`, `google-genai`, plus **Ollama** (`ollama` pkg, local) | Covers cloud + local per user choice. |
| Concurrency | `asyncio.PriorityQueue` + per-worker semaphores | Matches modern best practice for GPU farm fanout. |
| Logging | `structlog` + `RichHandler` console + rotating file | Pretty terminal + persisted debug. |

> The full provider/install matrix lives in [DECISIONS.md §Tech choices](DECISIONS.md#tech-choices).

---

## 3. Directory layout

```
RBMN_Image_factory/
├── installer.bat                # Creates venv, installs requirements, runs first-run setup
├── run.bat                      # Activates venv and starts Uvicorn
├── README.md                    # End-user docs (install, run, settings, batch format)
├── requirements.txt
├── pyproject.toml               # tool config (ruff, mypy, pytest)
├── .env.example                 # Seeds default settings on first run
├── BLUEPRINT.md   CHECKLIST.md   CHANGELOG.md   DECISIONS.md
│
├── app/                         # Python package
│   ├── __init__.py
│   ├── main.py                  # FastAPI app factory + Uvicorn entrypoint
│   ├── config.py                # Pydantic settings, paths, env loading
│   ├── logging_setup.py         # structlog + console + rotating file
│   │
│   ├── db/
│   │   ├── engine.py            # async engine, WAL pragma, session factory
│   │   ├── models.py            # SQLModel: AppSettings, Project, Image, Job, ServerWorker
│   │   └── migrations.py        # Tiny in-process migration runner
│   │
│   ├── services/
│   │   ├── comfyui/
│   │   │   ├── client.py        # HTTP + WS client (port of devnotes pattern)
│   │   │   ├── dispatcher.py    # Worker registry, select_worker(), reserve, capabilities
│   │   │   ├── workflow.py      # prepare_klein_workflow() + fixups + group flatten
│   │   │   └── workflows/       # JSON files: klein_t2i, klein_1ref … klein_4ref
│   │   ├── jobs/
│   │   │   ├── queue.py         # asyncio.PriorityQueue + asyncio.Event wake-up
│   │   │   ├── runner.py        # dispatch loop, retry, VRAM recovery
│   │   │   └── events.py        # JobEventBroadcaster (pub/sub for SSE)
│   │   ├── llm/
│   │   │   ├── enhancer.py      # PromptEnhancer (4 providers, identical interface)
│   │   │   ├── prompts.py       # Built-in system prompts + per-model overrides
│   │   │   └── ollama.py        # Local Ollama HTTP client (localhost:11434)
│   │   ├── projects/
│   │   │   ├── service.py       # CRUD, asset path mgmt, zip export
│   │   │   └── batch_import.py  # CSV/JSON parser → list[QueuedImage]
│   │   └── downloads/
│   │       └── zip.py           # Stream a project's images as a single zip
│   │
│   ├── api/                     # FastAPI routers (one file per resource)
│   │   ├── projects.py
│   │   ├── images.py
│   │   ├── jobs.py              # incl. SSE stream
│   │   ├── settings.py
│   │   ├── batch.py             # CSV/JSON upload + preview + dispatch
│   │   └── llm.py               # enhance single / bulk
│   │
│   └── web/                     # Server-rendered UI
│       ├── routes.py            # GET /, /projects/{id}, /settings, /batch
│       ├── templates/
│       │   ├── base.html        # Header, nav, sidebar, debug console drawer
│       │   ├── projects_list.html
│       │   ├── project_view.html       # Tabs: Gallery | Batch Preview | Results
│       │   ├── settings.html
│       │   ├── partials/        # HTMX fragments: image_card, job_row, progress_bar
│       │   └── components/      # lightbox.html, toast.html, debug_drawer.html
│       └── static/
│           ├── css/tailwind.css         # CDN-linked in base; small custom additions here
│           ├── js/htmx.min.js
│           ├── js/alpine.min.js
│           └── js/factory.js            # SSE wiring, lightbox state, console buffer
│
├── data/                        # Created at runtime (gitignored)
│   ├── factory.db
│   ├── logs/factory.log
│   └── projects/{project_id}/
│       ├── project.json         # Portable sidecar mirror of DB row
│       ├── references/          # User-uploaded reference images
│       ├── outputs/             # Generated images (named {image_id}_{seed}.png)
│       └── batch.csv|.json      # Original batch import (kept for re-runs)
│
├── tests/
│   ├── test_workflow_prep.py
│   ├── test_dispatcher.py
│   ├── test_batch_import.py
│   └── test_queue.py
│
└── devnotes/                    # Existing reference docs (read-only)
```

---

## 4. Data model

### `AppSettings` (singleton, `id=1`)

Pared down from the storyboard app — image-only fields:

```
comfyui_urls: list[str]            # JSON
comfyui_server_caps: dict | None   # JSON {url: {klein: bool, healthy: bool, last_check}}

openai_api_key, openai_model
anthropic_api_key, anthropic_model
gemini_api_key, gemini_model
ollama_base_url           # default http://localhost:11434
ollama_model              # e.g. "llama3.1:8b"
default_llm_provider      # "openai" | "anthropic" | "gemini" | "ollama"

image_model_type          # "flux2_klein_9b" (only one for now)
image_system_prompt_overrides: dict   # {model: {text, enabled}}
image_prompt_guidance:     dict       # {model: str}

default_width, default_height, default_seed_mode  # "random" | "fixed"
max_parallel_per_worker = 1
restrict_explicit_content: bool = False
project_dir: str | None    # override for data/projects
```

### `Project`

`id, name, description, created_at, updated_at, settings (JSON), folder_path, image_count, status`

### `Image` (one per generated/queued image — including batch rows)

```
id, project_id, order_index
prompt, enhanced_prompt, negative_prompt
reference_paths: list[str]      # up to 4, project-relative
width, height, seed
status: queued | running | done | failed | cancelled
output_path: str | None
thumbnail_path: str | None
parameters: dict                # workflow-specific extras
error: str | None
created_at, completed_at
```

### `Job` (one per dispatch attempt)

```
id, image_id, project_id
status, priority, worker_url, prompt_id
started_at, completed_at
retry_count, error
```

`Image` ↔ `Job` is 1‑to‑many (a retry creates a new Job; the Image keeps the latest result).

### `ServerWorker` (in-memory mirror of `comfyui_urls`)

`url, healthy, in_flight, capabilities, last_check` — same shape as devnotes `ComfyWorker`.

---

## 5. End-to-end request flow

### Single image

```
UI "Generate"
  → POST /api/projects/{pid}/images        (creates Image row, status=queued)
  → POST /api/projects/{pid}/images/{iid}/dispatch
       └─ creates Job(priority=N), queue.notify()
Dispatcher loop wakes
  → select_worker(required_caps={"klein"}, reserve=True)
  → upload reference images to that ComfyUI server
  → prepare_klein_workflow(prompt+anti-text, w, h, seed, refs)
  → flatten_group_nodes → remove_missing_nodes → submit
  → broadcast SSE "job_started"
  → stream_and_wait → SSE "job_progress" repeatedly
  → on completion: download output → write to projects/{pid}/outputs/
  → write thumbnail → update Image row → SSE "job_completed"
UI (HTMX listener) swaps the image card in place.
```

### Batch (CSV / JSON)

```
UI "Import batch"
  → POST /api/batch/preview (multipart)
       └─ parse → return rows with validation status (refs found, prompt non-empty, etc.)
UI shows Preview tab with editable table.
UI "Create new project" OR "Add to current project"
  → POST /api/batch/commit?mode=new|append
       └─ create/select Project
       └─ insert N Image rows (status=queued)
       └─ optionally call /api/llm/enhance-bulk first (user toggle)
UI switches to Results tab.
  → all Image rows render as cards in their queued state.
  → SSE updates progress / completion live.
  → "Download all" button → GET /api/projects/{pid}/zip → streamed zip.
```

---

## 6. Batch import format

### CSV columns (header row required)

| Column | Required | Description |
|---|---|---|
| `prompt` | yes | Raw prompt — will be auto-suffixed with the Klein anti-text string. |
| `ref1` … `ref4` | no | Absolute or project-relative paths reachable from the ComfyUI server's machine. |
| `width` | no | Default from settings. |
| `height` | no | Default from settings. |
| `seed` | no | Blank or `random` = randomized; integer = fixed. |
| `negative_prompt` | no | Optional. |
| `enhance` | no | `true` / `false`; overrides project default. |
| `name` | no | Display label / output filename hint. |

### JSON

```jsonc
{
  "project_name": "August Promo",     // optional; required if mode=new
  "defaults": { "width": 1024, "height": 1024, "enhance": true },
  "items": [
    { "prompt": "...", "ref1": "C:/refs/hero.png", "seed": 42 },
    { "prompt": "...", "ref1": "C:/refs/hero.png", "ref2": "C:/refs/logo.png" }
  ]
}
```

Preview tab validation:

- Reference paths checked for existence (warn, don't block — paths may live on the ComfyUI host).
- `ref1`…`ref4` cap enforced.
- Prompt non-empty.
- Width/height divisible by 16.

---

## 7. Frontend layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Robomuffin Image Factory       [Projects] [Settings]    Workers ● 3/4  ⚙️  │
├──────────────────────────────────────────────────────────────────────────────┤
│ ◀ Sidebar: project list  │  Project: "August Promo"                          │
│                          │  [Gallery] [Batch Preview] [Results] [Settings]   │
│  + New Project           │  ─────────────────────────────────────────────    │
│  - August Promo          │                                                   │
│  - Holiday card v3       │  (tab content)                                    │
│                          │                                                   │
│                          │                                                   │
│                          │                                                   │
├──────────────────────────────────────────────────────────────────────────────┤
│ ▼ Debug console (collapsible drawer, last 500 lines, filter by job_id)       │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Gallery tab
- Grid of cards (HTMX-swapped on SSE events).
- Each card: thumbnail, prompt, status badge, ETA, actions (Re-run, Edit prompt, Enhance, Download, Delete, Open in lightbox).
- Lightbox: keyboard nav (←/→/Esc), shows full prompt + parameters, "Use as reference" button.

### Batch Preview tab
- Drag-and-drop CSV/JSON area on top.
- Editable table of parsed rows; inline error chips per row.
- Bulk actions: "Enhance all prompts" (LLM), "Set default size", "Randomize all seeds".
- "Commit → New project" / "Commit → Append to this project" buttons.

### Results tab
- Same grid as Gallery, but filtered to the current batch and grouped by status.
- Overall progress bar (X/Y completed, ETA from rolling avg per worker).

### Settings page
- One section per: ComfyUI servers (add/test/remove), LLM providers (per-provider key + test), Image defaults (size/seed), System prompt overrides per model, Project dir, Content safety, Logging level.

### Status & debug
- Top-right: live worker chips (green/red), aggregate queue depth, jobs/min.
- Bottom drawer: streamed log lines (structlog → SSE). Filter by `job_id`, `level`, free-text.

---

## 8. Dispatcher & queue

- **In-process** `asyncio.PriorityQueue` (not Celery/Redis — single-machine, single-process is sufficient for this tool).
- **Wake mechanism**: `asyncio.Event` set on enqueue.
- **Dispatch loop**:
  1. Wait on event or timeout 5s.
  2. Compute available slots = Σ (`max_parallel_per_worker` − `in_flight`) over healthy workers.
  3. While slots > 0 and queue non-empty: `pop`, `select_worker(reserve=True)`, `asyncio.create_task(run_job)`.
- **Retry**: 3 max with exponential backoff; on `VRAMError` call `/free` first.
- **Recovery**: on startup, mark any `running`/`pending` jobs as `failed` (clean restart).
- **Cancellation**: cancel sets queue intent flag; the running task checks between WS reads.

---

## 9. Status, progress & ETA

- Each SSE event carries `{job_id, image_id, project_id, status, progress, current_node, worker_url}`.
- ETA per image = `(100 − progress) / progress * elapsed`, smoothed with rolling avg of the worker's last 5 jobs.
- ETA per project = `pending_count * avg_seconds + sum(remaining of running)`.

---

## 10. Logging & debugging

- `structlog` JSON to `data/logs/factory.log` (rotated 10MB × 5).
- `RichHandler` to console in dev, plain in production.
- A **separate** SSE stream `/api/logs/stream` for the debug drawer (deduplicated, capped at 200 lines/sec).
- Per-job log file `data/projects/{pid}/logs/{job_id}.log` for deep dives.

---

## 11. LLM prompt enhancement

Mirrors devnotes pattern, simplified for image-only:

- `PromptEnhancer.enhance(prompt, context, provider, api_key, model, ...)`.
- One built-in system prompt for `flux2_klein_9b` (image) — adapted from devnotes §2.
- Per-user override (saved in `AppSettings.image_system_prompt_overrides["flux2_klein_9b"]`).
- Anti-text suffix is appended in `prepare_klein_workflow`, **never** in the LLM step.
- Ollama provider: hits `http://localhost:11434/api/chat`, same temperature/max-token contract.
- `POST /api/llm/enhance` (single) and `POST /api/llm/enhance-bulk` (project or batch); both return enhanced prompts and tokens used.

---

## 12. Security / sanity

- All API keys masked (`****abcd`) in `GET /api/settings`; never logged.
- Reference paths from batch are not opened locally (they exist on the ComfyUI host); we only validate they exist if the path also resolves locally.
- `restrict_explicit_content` setting appends SFW suffix per devnotes pattern.
- Allow user to set `project_dir`; default `data/projects/mments-as-fallback).
- `services/comfyui/workflows/klein_{t2i,1ref,2ref,3ref,4ref}.json` — official Klein workflow templates.

**Phase 4 — Queue + SSE**
- `services/jobs/queue.py` — `JobQueue` (in-memory heap), `recover_running_jobs()`, `repopulate_from_db()`, `drain()` for hard-stop.
- `services/jobs/runner.py` — parallel dispatch loop (`asyncio.wait FIRST_COMPLETED`), per-job log capture, retry/fail funnel.
- `api/jobs.py` — `/api/jobs/stream` (SSE), `/api/jobs/queue/{pause,resume,start,stop,status}`, `/api/workers`, `/api/workers/probe`.

**Phase 5+ — Projects, batch, framing**
- `api/projects.py`, `api/images.py`, `api/batch.py` — REST endpoints.
- `services/projects/service.py`, `services/projects/batch_import.py` — CSV/JSON parsing, sidecar files, ZIP packaging.
- `services/framing.py` — composition scaffolds (Subtle/Moderate/Strong) + LLM-enhancer instruction helper.
- `services/openai_images/client.py` — GPT Image 2/1 alternative engine.

---

## 17. Security & masking

- All API keys masked (`****abcd`) in `GET /api/settings`; never logged.
- Reference paths from batch rows are scrubbed for BOM / NBSP / smart quotes before any filesystem call.
- The hard-stop endpoint sends `/interrupt` to ComfyUI workers so in-step generations abort cleanly.
- `restrict_explicit_content` setting appends SFW suffix per devnotes pattern.
- `project_dir` is user-configurable; default is `data/projects/` under the app root.
