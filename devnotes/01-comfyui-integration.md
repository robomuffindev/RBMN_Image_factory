# ComfyUI Integration — Architecture Reference

## Overview

The app communicates with one or more remote ComfyUI servers via HTTP REST + WebSocket.
It never runs ComfyUI locally — all generation happens on remote GPU machines.

## ComfyUI Client (`backend/services/comfyui/client.py`)

### Exception Hierarchy

```python
ComfyUIConnectionError  # network/connectivity failures (HTTP or WebSocket)
ComfyUIWorkflowError    # invalid workflow or execution-level errors
ComfyUIVRAMError        # CUDA OOM (detected by "OutOfMemory" or "CUDA" in error strings)
```

### Constructor

```python
ComfyUIClient(base_url, timeout=30, skip_health_check=False)
```

- Strips trailing slash from URL
- Creates a `requests.Session`
- **RunPod SSL bypass**: If URL contains "runpod.net" or starts with "https://", disables SSL verification and suppresses InsecureRequestWarning (RunPod proxy uses its own certs)
- **Health check**: Hits `/system_stats` unless `skip_health_check=True`. The skip flag is for RunPod workers added dynamically (pre-validated elsewhere)

### Key HTTP Methods

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `queue_prompt(workflow)` | POST `/prompt` | Submit workflow. Body: `{"prompt": workflow}`. Returns dict with `prompt_id` |
| `get_history(prompt_id)` | GET `/history/{id}` | Get execution results for a specific prompt |
| `upload_image(filepath, filename)` | POST `/upload/image` | Multipart upload. Returns `{name: stored_filename}` |
| `get_queue()` | GET `/queue` | Returns `{queue_pending: [], queue_running: []}` |
| `interrupt()` | POST `/interrupt` | Stop current execution (non-throwing) |
| `free_memory()` | POST `/free` | Body: `{"unload_models": True, "free_memory": True}`. Frees GPU VRAM (non-throwing) |
| `get_object_info()` | GET `/object_info` | Returns all available node type definitions (used for capability discovery) |
| `download_output(filename)` | GET `/view?filename=...&type=output` | Download generated file bytes |
| `try_download_output(filename)` | Same, but non-throwing | Returns bytes if status=200 AND content > 1000 bytes, else None |

### Central HTTP Dispatch Pattern

```python
def _make_request(method, endpoint, **kwargs):
    # Builds full URL, sends request with timeout
    # On 4xx/5xx: logs full response body BEFORE raise_for_status()
    # Returns parsed JSON or empty dict
    # Wraps all requests.RequestException into ComfyUIConnectionError
```

**Key lesson**: Always log the ComfyUI error response body before raising — ComfyUI returns detailed validation errors in JSON that are critical for debugging workflow issues.

### WebSocket Streaming: `stream_prompt(prompt_id)`

This is a generator that yields WS messages until the prompt completes. It's the most complex method.

**Connection setup:**
- Auto-generates UUID `client_id`
- Converts `http://` to `ws://` (or `https://` to `wss://`)
- For WSS: creates SSL context with `cert_reqs=ssl.CERT_NONE` (RunPod)
- Sets `ws.settimeout(10)` — enables periodic polling without blocking forever

**State tracking:**
```python
saw_progress_complete = False   # True when progress.value >= progress.max
idle_cycles = 0                 # consecutive recv timeouts with no relevant messages
_progress_complete_at = None    # monotonic timestamp when progress hit 100%
```

**Message processing:**
- Skips binary frames (preview images) and empty messages
- **Noise filtering**: `crystools.monitor` messages silently dropped
- **Prompt filtering**: Messages with wrong `prompt_id` are skipped
- Relevant messages reset `idle_cycles` to 0

**Completion detection** (multiple strategies — ComfyUI versions differ):

1. **Primary**: `msg_type == "executing"` with `data.node == None` — graph finished
2. **Alternative types**: `execution_complete`, `executed`, `complete`, `execution_success`
3. **Queue-based**: After `saw_progress_complete`, `status` with `queue_remaining == 0`
4. **Error handling**: `execution_error` → checks for OOM → raises `ComfyUIVRAMError` or `ComfyUIWorkflowError`

**Idle timeout handling** (on WebSocket recv timeout every 10s):
- If progress was 100%: polls `/history/{prompt_id}` immediately
- Every 30s: combined history + queue check
- If prompt not in queue AND not in history for 60s: raises error
- Absolute timeout: 15 minutes

**Key lesson**: You MUST handle multiple completion signal types. Different ComfyUI builds and custom nodes trigger different completion events. The queue_remaining check is essential as a fallback.

---

## Dispatcher (`backend/services/comfyui/dispatcher.py`)

### ComfyWorker Dataclass

```python
@dataclass
class ComfyWorker:
    url: str
    healthy: bool = True
    in_flight: int = 0              # currently running jobs
    capabilities: Set[str] = {}      # {"klein", "ltx", "upscale", "inpaint"}
    models: Set[str] = {}            # {"KLEIN", "LTX", "FLUX", "SD15"}
    last_check: datetime
    is_runpod: bool = False
```

### Worker Selection Algorithm

```python
def select_worker(required_caps=None, required_models=None, exclude_runpod=False, reserve=False):
```

Selection order:
1. Filter to healthy workers only (exclude RunPod if requested)
2. Filter by `required_caps` (set intersection). If no match → fall back to ALL healthy workers
3. Filter by `required_models` (set intersection). If no match → keep previous set
4. Pick worker with minimum `(in_flight, -last_check.timestamp())` — least loaded, then most recently checked

**Reserve pattern**: If `reserve=True`, immediately increments `in_flight`. Prevents race conditions in async dispatch. Caller must `release_worker()` if job fails before submission.

### Capability Discovery

```python
def discover_capabilities(worker):
    # Queries /object_info, inspects all node type names
    # "Klein"/"klein" → "klein" capability
    # "LTX"/"ltx" → "ltx" capability
    # Also parses CheckpointLoaderSimple inputs for model detection
```

### Job Submission + Monitoring

```python
def submit_job(workflow, worker_url=None, already_reserved=False):
    # Selects worker if not specified
    # Calls client.queue_prompt(workflow)
    # Increments in_flight (unless already_reserved)
    # Returns prompt_id

def stream_and_wait(worker_url, prompt_id, on_progress=None):
    # Opens WebSocket, yields progress
    # Captures WS 'executed' node outputs as fallback
    # Post-completion: polls /history with 10 retries (3s apart)
    # If history empty: merges WS-captured outputs
    # Always decrements in_flight in finally block
    # Returns history dict
```

**Key lesson**: VHS_VideoCombine output often doesn't appear in `/history` immediately. The retry loop + WS output capture + `try_download_output` fallback chain is essential for reliable video generation.

### Output Retrieval Fallback Chain

1. Check `/history/{prompt_id}` for `images`/`gifs`/`videos` with `filename` fields
2. Retry up to 10 times with 3-second sleep (VHS still writing to disk)
3. Merge WebSocket `executed` message outputs into history
4. Last resort: `try_download_output()` using unique VHS filename prefix

---

## Workflow Preparation (`backend/services/comfyui/workflow.py`)

### Core Utilities

```python
find_node_by_title(workflow, title)    # Match on _meta.title, returns (node_id, node_dict)
set_node_input(workflow, title, input_name, value)  # Find + set input
```

### Klein Image Workflow: `prepare_klein_workflow()`

```python
prepare_klein_workflow(workflow_path, prompt, width, height, seed, ref_images=None)
```

Node mutations:
- `"CLIP Text Encode (Positive Prompt)"` → prompt + anti-text suffix
- `"Width"` / `"Height"` → dimensions
- `"RandomNoise"` → `noise_seed`
- `"Load Image"` → ref_images[0]
- `"Reference 2 Image"` → ref_images[1]
- `"Reference 3"` → ref_images[2]
- `"Reference 4"` → ref_images[3]

**Anti-text suffix** (always appended to Klein prompts):
```
", no text, no subtitles, no captions, no words, no letters, no watermarks"
```
Klein 9B has a tendency to render text overlays. This suffix prevents it.

### LTX Video Workflow: `prepare_ltx_workflow()`

```python
prepare_ltx_workflow(workflow_path, prompt, width, height, duration, framerate, seed,
                     audio_path, first_frame=None, last_frame=None, ltx_model_gguf=None)
```

Two variants:
- **Frame-to-Frame (FF/LF)**: Both `first_frame` and `last_frame` → sets `"LOAD FIRST IMAGE FRAME"` and `"LOAD LAST IMAGE FRAME"`
- **Image-to-Video (I2V)**: Only `first_frame` → sets `"LOAD IMAGE"`

**Three automatic fixup functions** (CRITICAL — apply to all workflows):

1. **`_update_resize_longer_edge()`**: Finds ALL `ResizeImagesByLongerEdge` nodes, sets `longer_edge = max(width, height)`. Prevents aspect ratio distortion.

2. **`_fix_image_resize_stretch()`**: Changes ALL `ImageResizeKJv2` nodes from `"stretch"` to `"resize"`. Stretch mode causes height distortion at scene transitions.

3. **`_update_ltxv_preprocess_compression()`**: Sets ALL `LTXVPreprocess` nodes to `img_compression=35` (default 18 was too aggressive, causing content shifts in frame chaining).

### V2V Extending Workflow: `prepare_v2v_extend_workflow()`

Uses `LTXVExtendSampler` with multi-frame overlap.

**Frame skip optimization** (saves VRAM):
```python
total_frames = round(prev_video_duration * framerate)
keep_frames = frame_overlap + 8
skip = max(0, total_frames - keep_frames)
# Sets skip_first_frames and frame_load_cap on "LOAD PREVIOUS VIDEO" node
```

Only loads the tail frames needed for overlap, not the entire previous video.

### Transition Workflow: `prepare_transition_workflow()`

For AI transition clips using the LTX Transition LoRA.

**Trigger word**: Appends `", zhuanchang"` to prompt (must be at END per model card).

### Graph Manipulation Utilities

**`flatten_group_nodes(workflow)`**: ComfyUI group nodes use `"1217:1089"` composite IDs. Converts to `"1217_1089"` and updates all connection references. Must be called BEFORE `stamp_vhs_unique_prefix()`.

**`strip_non_essential_nodes(workflow)`**: Removes debug nodes (`Image Comparer`). Does NOT strip GPU cleanup nodes (`easy cleanGpuUsed`, `easy clearCacheAll`) — these prevent OOM.

**`remove_missing_nodes(workflow, available_types)`**: Removes nodes whose `class_type` isn't available on the target server. Rewires connections to bypass removed nodes. Prevents entire dependency chain failures from missing optional nodes.

**`stamp_vhs_unique_prefix(workflow, unique_tag)`**: Sets unique `filename_prefix` on `VHS_VideoCombine` node. Enables reliable fallback download when `/history` doesn't include output.

### Dynamic Workflow System: `prepare_workflow_from_config()`

For user-uploaded custom workflows. Uses `field_mappings` (list of `{node_title, input_name, field_type}`) to dynamically apply values. Field types: prompt, negative_prompt, width, height, seed, image, video, audio, duration, framerate, steps, cfg.

---

## Multi-Server Architecture Summary

```
                    ┌─────────────────┐
                    │  Job Queue (DB)  │
                    └────────┬────────┘
                             │ dequeue
                    ┌────────▼────────┐
                    │   Dispatcher    │
                    │  (select_worker) │
                    └──┬─────────┬────┘
            ┌──────────▼──┐  ┌──▼──────────┐
            │ ComfyUI #1  │  │ ComfyUI #2  │  (+ RunPod pods)
            │ Klein + LTX │  │ LTX only    │
            └─────────────┘  └─────────────┘
```

1. **Workflow prep** → loads JSON, mutates nodes, applies fixups
2. **Worker selection** → filters by capability/model, picks least-loaded
3. **Image upload** → reference images uploaded to server before workflow submission
4. **Submission** → POST `/prompt` with workflow dict
5. **Monitoring** → WebSocket stream for progress + completion
6. **Output retrieval** → `/history` with retry, WS fallback, direct download fallback
7. **In-flight tracking** → increment on select/submit, decrement in finally block

---

## Error Handling Patterns

### VRAM OOM Recovery
```python
try:
    result = dispatcher.stream_and_wait(worker_url, prompt_id)
except ComfyUIVRAMError:
    client.free_memory()  # POST /free to unload models
    # Retry with exponential backoff (base 2s, max 3 retries)
```

### Missing Node Handling
```python
available_types = set(client.get_object_info().keys())
missing = remove_missing_nodes(workflow, available_types)
if missing:
    logger.warning(f"Removed missing nodes: {missing}")
# Workflow still runs with non-essential nodes removed
```

### RunPod Health Check
Before uploading files to a RunPod worker, verify it's actually responding:
```python
if worker.is_runpod:
    try:
        client.get_system_stats()
    except ComfyUIConnectionError:
        # Pod may have gone to sleep — trigger resume
```
