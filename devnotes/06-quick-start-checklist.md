# Quick Start Checklist — New Klein/ComfyUI Project

Step-by-step to bootstrap a new project that uses FLUX Klein for image generation and LTX for video generation, with LLM prompt enhancement and a Settings UI.

---

## 1. Backend Foundation

### Database & Settings

```
□ Create AppSettings SQLModel singleton (id=1)
  - ComfyUI server URLs (JSON list)
  - LLM API keys (3 providers: openai, anthropic, gemini)
  - LLM model names per provider
  - Default LLM provider selector
  - Image/video model type selectors
  - System prompt overrides (JSON dict keyed by model name)
  - Prompt guidance (JSON dict keyed by model name)
  - Generation parameters (FPS, max duration, etc.)

□ Settings initialization: seed from .env on first run, backfill on migration
□ API key masking in responses (show last 4 chars only)
□ Settings export/import endpoints
```

### ComfyUI Client

```
□ HTTP client with requests.Session
  - queue_prompt() — POST /prompt
  - get_history() — GET /history/{id}
  - upload_image() — POST /upload/image (multipart)
  - get_object_info() — GET /object_info
  - download_output() — GET /view?filename=...
  - free_memory() — POST /free
  - get_system_stats() — GET /system_stats

□ WebSocket streaming (stream_prompt generator)
  - Multiple completion signals (executing node=None, execution_complete, queue_remaining=0)
  - WS 'executed' message output capture (VHS fallback)
  - Idle timeout handling with history polling
  - VRAM error detection (OutOfMemory/CUDA in error strings)
  - 10-second recv timeout for periodic polling

□ RunPod SSL bypass (disable verify for runpod.net URLs)
□ Custom exceptions: ConnectionError, WorkflowError, VRAMError
```

### Dispatcher

```
□ ComfyWorker dataclass (url, healthy, in_flight, capabilities, models)
□ Worker selection algorithm (capability filter → least loaded)
□ Reserve pattern (increment in_flight before async submission)
□ Capability discovery from /object_info node types
□ submit_job() + stream_and_wait() with retry logic
□ Output retrieval: history → retry 10x → WS output merge → direct download
□ In-flight tracking (increment on select, decrement in finally)
```

### Job Queue

```
□ Job model (SQLite): id, type, status, priority, worker_url, prompt_id, parameters, result, error
□ Status lifecycle: pending → running → done/failed/retrying → cancelled
□ Atomic dequeue (select + mark running in one transaction)
□ asyncio.Event for instant wake-up on enqueue
□ Startup recovery (cancel all stale pending/running jobs)
□ Retry logic: max 3, exponential backoff, free_memory before VRAM retry
```

### Workflow Preparation

```
□ Node identification by _meta.title (NOT by ID)
□ prepare_klein_workflow(): prompt + anti-text suffix, dimensions, seed, refs 1-4
□ prepare_ltx_workflow(): prompt, dimensions, duration, fps, seed, audio, frames
□ Fixup functions: resize_longer_edge, fix_stretch, ltxv_preprocess_compression
□ flatten_group_nodes() — "1217:1089" → "1217_1089"
□ remove_missing_nodes() — graceful degradation for optional custom nodes
□ stamp_vhs_unique_prefix() — reliable output file identification
□ Image upload to ComfyUI before workflow submission
```

### SSE Progress Streaming

```
□ JobEventBroadcaster pub/sub (asyncio.Queue per subscriber)
□ SSE endpoint (StreamingResponse, text/event-stream)
□ Event types: job_started, job_progress, job_completed, job_failed
□ Heartbeat on connect (stream_ready)
```

---

## 2. LLM Integration

### Provider Abstraction

```
□ PromptEnhancer class with static enhance() method
□ Three providers: OpenAI, Anthropic, Gemini (identical interface)
□ Temperature: 0.7, max_tokens: 300
□ resolve_llm_config() — default provider → first available
□ System message + user message format
□ Post-processing: collapse to single paragraph, strip LLM prefixes
```

### System Prompt Registry

```
□ Built-in prompts per model (image, image_last_frame, video, two_pass_base, two_pass_composite)
□ Three-tier fallback: user override → model-specific → generic default
□ Per-model prompt guidance (appended to system prompt)
□ Frontend: fetch built-in prompt as placeholder text
```

### Context Assembly

```
□ Model info (generation model name)
□ Scene timing and position
□ Concept text + style text
□ Image direction (preset or custom)
□ Characters with images (compositional reference instructions)
□ Scene lyrics (PRIMARY creative source)
□ Story flow idea (SCENE STORYBOARD)
□ Camera action preset
□ Previous scene continuity context
```

### Key Rules

```
□ Klein: compositional language for refs, NOT "Image 1" tags
□ Klein: anti-text suffix appended in workflow prep, not in enhance
□ LTX: single paragraph CRITICAL (line breaks = separate segments)
□ Two-pass: Pass 1 scene-only (no refs), Pass 2 composite (scene as ref 1 + characters)
□ flow_idea becomes primary prompt when it exists, user prompt becomes context
```

---

## 3. Frontend

### Tech Stack

```
□ React 18 + TypeScript + Vite
□ TailwindCSS (dark theme: bg-gray-950, cards: bg-gray-900 border-gray-800)
□ Zustand for global UI state
□ @tanstack/react-query for server state
□ Axios API client at baseURL: '/api'
□ Native EventSource for SSE
```

### Settings Page Pattern

```
□ Local useState for entire settings object (NOT Zustand)
□ useEffect syncs from react-query fetch
□ Single "Save" button PUTs entire object
□ Sticky save bar at top
□ Section cards: bg-gray-900 border border-gray-800 rounded-lg p-6
□ Per-model keying for prompt overrides (Record<string, {text, enabled}>)
□ Test connection buttons with result icons
□ API key masking (ignore masked values on save)
```

### Generation UI Pattern

```
□ Enhance button → POST enhance-prompt → display result in textarea
□ Generate button → POST generate/image or video → creates Job
□ SSE subscription → update job progress in real-time
□ Job completed → invalidate queries → refresh gallery
□ Gallery with lightbox overlay for generated images/videos
```

### State Management Pattern

```
□ react-query owns server state (fetch, cache, invalidate)
□ Zustand caches for cross-component reads + UI-only state
□ Mutations: API client → Zustand update / query invalidation
□ Query keys: ['project', id], ['scenes', id], ['settings'], ['jobs']
```

---

## 4. Gotchas & Hard-Won Lessons

### ComfyUI

- WebSocket completion detection needs multiple signal types (different ComfyUI versions)
- VHS_VideoCombine output often missing from /history — need retry + WS fallback + direct download
- /free endpoint is essential for VRAM recovery between jobs
- Don't strip GPU cleanup nodes (easy cleanGpuUsed) — they prevent OOM
- RunPod URLs need SSL verification disabled
- Upload images BEFORE submitting workflow
- Group node IDs must be flattened before submission

### Klein (FLUX.2 Klein 9B)

- Will render text overlays without anti-text suffix
- IP-Adapter makes all scenes look identical if character refs used in every pass
- Two-pass solves this: Pass 1 scene-only, Pass 2 composite
- Supports up to 4 reference images per generation

### LTX 2.3 Video

- Paragraph breaks in prompt create separate video segments — always collapse to single paragraph
- img_compression=35 for LTXVPreprocess (default 18 too aggressive for frame chaining)
- ImageResizeKJv2 must use "resize" not "stretch" mode
- ResizeImagesByLongerEdge must match actual project resolution
- V2V frame skip optimization: only load tail frames for overlap, not entire previous video
- Audio conditioning available via LTXVAudioVAEEncode pipeline
- Transition LoRA trigger word "zhuanchang" goes at END of prompt

### LLM Enhancement

- Always collapse output to single paragraph
- Strip common LLM prefixes ("Enhanced prompt:", etc.)
- Lyrics should be PRIMARY creative weight (not just context)
- flow_idea should become the primary prompt, not just context
- Temperature 0.7 works well across all three providers
- 300 max tokens is enough for generation prompts

### SSE/Streaming

- Use asyncio.Queue(maxsize=200) per subscriber — remove stale subscribers
- Browser's native EventSource handles auto-reconnect
- Send heartbeat on connect so frontend knows stream is ready
- Jobs can complete between SSE connections — always fetch current state on reconnect

### SQLite

- Use WAL mode for concurrent reads during generation
- aiosqlite for async operations
- Atomic dequeue: select + update in single transaction to prevent duplicate dispatch
- Settings singleton pattern: always id=1, get-or-create on every access
