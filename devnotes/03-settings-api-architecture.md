# Settings & API Architecture — Reference

## AppSettings Database Model

Singleton SQLModel table (`app_settings`, always `id=1`). Uses `sa_column=Column(JSON)` for complex types.

### All Settings Fields

```python
class AppSettings(SQLModel, table=True):
    # === ComfyUI ===
    comfyui_urls: list[str]                    # JSON — list of server base URLs
    comfyui_server_caps: Optional[dict]        # JSON — per-server caps: {url: {image: bool, video: bool}}

    # === Whisper ===
    whisper_mode: str                          # "local", "remote", "comfyui"
    whisper_remote_url: Optional[str]          # Gradio/REST URL
    whisper_comfyui_url: Optional[str]         # ComfyUI server with Whisper node
    whisper_model: str = "large-v2"            # WhisperX model name
    whisper_language: str = "English"

    # === LLM Providers ===
    openai_api_key: Optional[str]
    openai_model: Optional[str]
    anthropic_api_key: Optional[str]
    anthropic_model: Optional[str]
    gemini_api_key: Optional[str]
    gemini_model: Optional[str]
    default_llm_provider: Optional[str]        # "openai"/"anthropic"/"gemini" or None

    # === Generation Models ===
    image_model_type: str = "flux2_klein_dev_9b"
    video_model_type: str = "ltx_2.3"
    ltx_model_gguf: str = "ltx-2.3-22b-dev-Q8_0.gguf"  # quality vs VRAM

    # === System Prompt Overrides (JSON, keyed by model name) ===
    image_system_prompt_overrides: Optional[dict]  # {model: {text: str, enabled: bool}}
    video_system_prompt_overrides: Optional[dict]

    # === Prompt Guidance (JSON, keyed by model name) ===
    image_prompt_guidance: Optional[dict]
    video_prompt_guidance: Optional[dict]

    # === Video Parameters ===
    video_fps: int = 24
    video_max_duration: int = 15               # model max seconds
    video_min_duration: int = 5                # minimum scene length
    video_tail: int = 0                        # extra seconds (auto-trimmed)
    color_correction_enabled: bool = False

    # === Export ===
    export_transition_type: str = "none"       # "crossfade", "dissolve"
    export_transition_duration: float = 0.5
    export_color_match_clips: bool = False
    export_lfff_trim_enabled: bool = True      # trim first frame when using prev scene's LF

    # === Content Safety ===
    restrict_explicit_content: bool = False     # appends SFW tags

    # === RunPod ===
    runpod_enabled: bool = False
    runpod_api_key: Optional[str]
    runpod_idle_timeout: int = 30              # minutes
    runpod_pods: Optional[list]                # JSON — [{pod_id, label, service_type, gpu_type_id, template_id, api_port, enabled}]

    # === Project ===
    project_dir: Optional[str]                 # overrides env PROJECT_DIR
```

### Settings Initialization Pattern

```python
def _get_or_create_settings(session):
    # Phase 1: First run (no DB row) → seed from .env values
    # Phase 2: Existing row with empty fields → backfill from .env (one-time migration)
```

### Pattern for Adding New Settings

1. Add column to `AppSettings` with `Field(default=...)` or `sa_column=Column(JSON)`
2. Add to `SettingsResponse` (mask if sensitive)
3. Add optional field to `SettingsUpdate`
4. Add `if req.field is not None:` guard in `update_settings()`
5. Add to `_build_response()` with null-safe access
6. Add to `SettingsExportData` and import/export handlers
7. If runtime service affected, add side-effect logic in `update_settings()`

---

## Settings API Endpoints

**Router prefix:** `/api/settings`

### Core

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/settings` | Get all (API keys masked to last 4 chars) |
| PUT | `/api/settings` | Update. Ignores masked values (`***`). Reconfigures RunPod + dispatcher |

### Connection Tests

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/settings/test-comfyui` | Hits `/system_stats` on target URL |
| POST | `/api/settings/test-whisper` | Tests local/comfyui/remote based on mode |
| POST | `/api/settings/test-llm` | Validates API key per provider |

### Export/Import

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/settings/export` | Full backup with unmasked keys (.rbmn-settings.json) |
| POST | `/api/settings/import` | Restore from backup file |

### Built-in Prompts

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/settings/builtin-prompt` | Returns built-in system prompt for model/type (UI placeholder) |

### RunPod

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/settings/runpod/test` | Test RunPod API key |
| GET | `/api/settings/runpod/status` | All pod statuses |
| POST | `/api/settings/runpod/start` | Start a pod |
| POST | `/api/settings/runpod/stop` | Stop a pod |

### Project Directory

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/settings/browse-directory` | Native tkinter folder picker |
| POST | `/api/settings/change-project-dir` | Move project data to new path |

---

## Generation API Endpoints

**Router prefix:** `/api/projects/{project_id}/generate`

### Direct Generation

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `.../generate/image` | Single image job. Supports two-pass, seed resolution chain |
| POST | `.../generate/video` | Single video job. FF/LF + I2V, audio, skip_audio_mux |
| POST | `.../generate/enhance-prompt` | LLM enhance. Returns `{enhanced_prompt, tokens_used}` |
| POST | `.../generate/batch` | Submit multiple jobs |

### Auto-Generation

| POST | `.../generate/auto` | Batch auto-gen (all_images, empty_only, enhanced_all, enhanced_missing) |
| POST | `.../generate/auto-sequential` | Background scene-by-scene. Modes below |
| GET | `.../generate/auto-sequential/status` | Poll progress |
| POST | `.../generate/auto-sequential/cancel` | Cancel running auto-gen |

### Sequential Auto-Gen Modes

| Mode | Description |
|------|-------------|
| `all_images` | Generate first-frame images for all scenes (with two-pass if enabled) |
| `all_video_single` | I2V video for all scenes |
| `all_video_fflf` | FF/LF video for all scenes |
| `all_video_v2v` | V2V extending for all scenes (chains scene-to-scene) |
| `missing_videos_single` | I2V video only for scenes missing videos |
| `missing_images_independent` | Batch-parallel images for scenes missing them |

### Seed Resolution Chain

```python
def _resolve_seed(request, scene, frame_type):
    # 1. Explicit seed in request (highest priority)
    # 2. Scene parameter override (per-frame for images, video_seed for videos)
    # 3. Project global_seed (if enabled and non-zero)
    # 4. None → dispatcher randomizes
```

### Windowed Batch Dispatch (`_run_windowed_batch`)

Two-phase continuous dispatch:
1. **Prepare**: Resolve images, enhance prompts for all eligible scenes
2. **Dispatch**: Fill N worker slots initially (N = capable workers), then as each job completes, immediately submit next

No idle workers between jobs. Maximizes throughput.

---

## Job Queue System (`backend/services/jobs/queue.py`)

### Job Model

```python
class Job(SQLModel, table=True):
    id: UUID
    project_id: UUID
    scene_id: Optional[UUID]
    job_type: JobType          # "image" | "video"
    status: JobStatus          # pending/running/done/failed/retrying/cancelled
    priority: int              # higher = more urgent
    worker_url: Optional[str]
    prompt_id: Optional[str]   # ComfyUI prompt ID
    parameters: dict           # JSON — all generation params
    result: dict               # JSON — output paths, asset IDs
    error: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    retry_count: int = 0
```

### Job Lifecycle

```
PENDING → RUNNING → DONE
                  → FAILED
                  → RETRYING (stays RUNNING, retry_count++)
        → CANCELLED
```

### Queue Operations

- `dequeue()`: Atomically selects highest-priority PENDING job, marks RUNNING
- `mark_done(job_id, result)`: Sets DONE + completed_at
- `mark_failed(job_id, error)`: Sets FAILED + completed_at + error message
- `mark_retrying(job_id)`: Keeps RUNNING (prevents duplicate dispatch), increments retry_count
- `cancel(job_id)`: Only works on PENDING or RUNNING
- `recover_running_jobs()`: On startup, cancels ALL stale PENDING + RUNNING for clean restart

### Dispatch Loop

```python
async def dispatch_loop():
    recover_running_jobs()  # startup cleanup
    while True:
        await wait_for_jobs(timeout=5.0)  # asyncio.Event or timeout
        available_slots = count_available_workers()
        for _ in range(available_slots):
            job = dequeue()
            if job:
                asyncio.create_task(process_job(job))
```

### Retry Logic

- MAX_RETRIES = 3
- Exponential backoff: base 2 seconds
- On VRAM errors: calls `/free` endpoint before retry
- `mark_retrying()` keeps status as RUNNING to prevent duplicate dispatch

---

## SSE Progress Streaming

### Pub/Sub Architecture

```python
class JobEventBroadcaster:
    subscribers: Dict[str, asyncio.Queue]  # per-client queues, maxsize=200

    def subscribe() -> Queue    # create per-client queue
    def unsubscribe(queue)      # remove on disconnect
    def put_nowait(event)       # broadcast to ALL subscribers, remove stale ones
```

### SSE Endpoint

```
GET /api/projects/{project_id}/jobs/stream
Content-Type: text/event-stream
```

Each connection subscribes its own queue. Sends `stream_ready` heartbeat on connect.
Format: `data: {json}\n\n`

### Event Types

| Internal Event | SSE Event | Key Fields |
|---------------|-----------|------------|
| processing_started | job_started | job_id |
| worker_assigned | job_worker_assigned | worker_url, scene_id |
| progress | job_progress | progress (0-100) |
| executing | job_progress | node |
| completed | job_completed | project_id, scene_id |
| failed_final | job_failed | error |
| retrying | job_retrying | job_id |

### Frontend Consumption

```typescript
function subscribeToJobEvents(onUpdate: (event) => void): () => void {
    const es = new EventSource('/api/jobs/stream');
    es.addEventListener('job_started', ...);
    es.addEventListener('job_progress', ...);
    es.addEventListener('job_completed', ...);
    es.addEventListener('job_failed', ...);
    return () => es.close();
}
```

Auto-reconnect handled by browser's native EventSource behavior.

---

## Request Flow: Image Generation (End to End)

```
Frontend clicks "Generate"
  → POST /api/projects/{pid}/generate/image
    → Resolve seed (request > scene > global > random)
    → Resolve workflow (type → JSON file path, or custom config)
    → Create Job(status=PENDING, parameters={...})
    → job_queue.notify()
  
Dispatch loop wakes up
  → dequeue() → atomically mark RUNNING
  → select_worker(required_caps={"klein"}, reserve=True)
  → Upload reference images to ComfyUI server
  → prepare_klein_workflow() → mutate nodes
  → flatten_group_nodes()
  → remove_missing_nodes()
  → stamp_vhs_unique_prefix()
  → submit_job(workflow, worker_url)
  → broadcast "job_started" SSE event
  
  → stream_and_wait(worker_url, prompt_id, on_progress=broadcast_progress)
    → WebSocket monitors ComfyUI execution
    → Yields progress events → broadcast via SSE
    → Detects completion
    → Polls /history for output files
    → Downloads output images
    → Creates Asset records in DB
    → Stores output_path on Job.result
  
  → If two-pass: auto-create Pass 2 job
  → mark_done(job_id, result)
  → broadcast "job_completed" SSE event
  → release_worker()

Frontend receives SSE "job_completed"
  → Invalidates react-query cache
  → Scene editor refreshes gallery
```
