# Decisions & Lessons — Robomuffin Image Factory

> A running ledger of **why** we chose what we chose, **what bit us** in the
> previous app, and **what NOT to repeat**. Append-only — when something
> changes, add a new entry that supersedes the old (don't silently edit
> history).

Sibling docs: [BLUEPRINT.md](BLUEPRINT.md) · [CHECKLIST.md](CHECKLIST.md) · [CHANGELOG.md](CHANGELOG.md)

---

## Tech choices

### TC-1 · FastAPI + HTMX (no React) — _2026-05-21_

**Decision.** Server-rendered Jinja templates with HTMX for partial swaps and
Alpine.js for tiny client state (lightbox, drawers). Tailwind via CDN.

**Why.**
- Image Factory is a focused single-purpose tool — a React build pipeline is
  overhead we don't need.
- HTMX gives us partial swaps for free; the queue UX is "card appears, card
  updates, card swaps" which is HTMX's wheelhouse.
- One Python process to install and ship; no Node toolchain in `installer.bat`.

**Tradeoff accepted.** A future "rich editor" or "complex multi-pane workspace"
will be harder than in React. If we cross that line, we revisit.

---

### TC-2 · SQLite + WAL + `aiosqlite` — _2026-05-21_

**Decision.** Single `data/factory.db` file. WAL mode for concurrent reads
during generation. SQLModel/SQLAlchemy 2.x async API.

**Why.** Mirrors devnotes pattern. Zero external services. Portable.

---

### TC-3 · In-process `asyncio.PriorityQueue` (no Celery/Redis) — _2026-05-21_

**Decision.** Single Uvicorn process owns the queue and dispatch loop. No
external broker.

**Why.**
- Single-machine desktop tool — no horizontal scaling story.
- Devnotes pattern proved this works for at least the multi-worker case.
- Avoids one more service to install and document.

**Reconsider when.** Multi-process workers or multi-machine orchestration
appears as a requirement.

---

### TC-4 · LLM providers: OpenAI + Anthropic + Gemini + Ollama — _2026-05-21_

**Decision.** All four behind one `PromptEnhancer.enhance()` interface.

**Why.** User asked for all four. Local Ollama removes the "I don't have an
API key" objection and protects sensitive prompts.

**Implementation note.** All four use the same temperature (0.7) and max
tokens (300), and the same post-processing (collapse to single paragraph,
strip "Enhanced prompt:" prefixes). See [BLUEPRINT §11](BLUEPRINT.md#11-llm-prompt-enhancement).

---

### TC-5 · Batch import supports both `new` and `append` modes — _2026-05-21_

**Decision.** `POST /api/batch/commit?mode=new|append`. Preview tab is mode-agnostic.

**Why.** User asked for both. Append is useful for incremental work; new is the
clean-room case.

---

## Patterns we are deliberately copying from devnotes

> If you're tempted to "improve" any of these, read the linked devnotes
> section first — most of them are scar tissue from real bugs.

| Pattern | Source | Why it exists |
|---|---|---|
| Identify ComfyUI nodes by `_meta.title`, never by ID | [05-workflow-json-reference.md](devnotes/05-workflow-json-reference.md) | Node IDs change between ComfyUI exports. |
| Multiple WS completion signals + history polling fallback | [01-comfyui-integration.md §stream_prompt](devnotes/01-comfyui-integration.md) | Different ComfyUI versions emit different signals. Single-signal detection silently hangs. |
| Anti-text suffix appended in `prepare_klein_workflow`, not in LLM enhance | [02-llm-prompt-enhancement.md §Klein-Specific Prompt Rules](devnotes/02-llm-prompt-enhancement.md) | The LLM doesn't know Klein's quirks; the suffix needs to be exact and present every time. |
| `flatten_group_nodes` before submission | [05-workflow-json-reference.md](devnotes/05-workflow-json-reference.md) | Cross-boundary `"1217:1089"` IDs silently break execution. |
| `remove_missing_nodes` with connection rewire | [05-workflow-json-reference.md](devnotes/05-workflow-json-reference.md) | A missing optional custom node otherwise kills the whole graph. |
| Worker `reserve=True` increments `in_flight` before async submission | [01-comfyui-integration.md §dispatcher](devnotes/01-comfyui-integration.md) | Without it, the dispatcher races itself and dogpiles one worker. |
| Settings singleton at `id=1`, get-or-create on every access | [03-settings-api-architecture.md](devnotes/03-settings-api-architecture.md) | Avoids "no row yet" branches everywhere. |
| API keys masked to last 4 chars on `GET /api/settings` | [03-settings-api-architecture.md](devnotes/03-settings-api-architecture.md) | Stops accidental key disclosure in HAR exports / screenshots. |
| `PUT /api/settings` ignores masked values (`****abcd`) | [03-settings-api-architecture.md](devnotes/03-settings-api-architecture.md) | Otherwise saving the form blanks the real key. |
| Per-subscriber `asyncio.Queue(maxsize=200)`, prune on full | [03-settings-api-architecture.md §SSE](devnotes/03-settings-api-architecture.md) | Slow client otherwise wedges the broadcaster. |
| Send a `stream_ready` heartbeat on SSE connect | [04-frontend-patterns.md](devnotes/04-frontend-patterns.md) | Frontend can show "live" indicator deterministically. |
| `recover_running_jobs()` on startup marks stale jobs failed | [03-settings-api-architecture.md §Job Queue](devnotes/03-settings-api-architecture.md) | Crash-restart otherwise spins up phantom dispatches. |

---

## Mistakes to AVOID (hard-won, from the previous app)

> Before starting a new phase in [CHECKLIST.md](CHECKLIST.md), skim this list.

### M-1 · "Image 1" / "Image 2" literal references in prompts
Klein WILL render literal text. The LLM is told to use **compositional**
language ("the subject from the first image"). The IP-Adapter slots already
know which is which.

### M-2 · Forgetting the Klein anti-text suffix
Klein will overlay watermark-style text in unguarded prompts. Suffix is
appended in `prepare_klein_workflow`, every single time, no exceptions.

### M-3 · Stripping GPU-cleanup nodes when "cleaning up" workflows
`easy cleanGpuUsed` / `easy clearCacheAll` look noisy but **prevent OOM** on
16GB cards. Strip only known-debug nodes (e.g. `Image Comparer`).

### M-4 · Skipping the `/free` call between VRAM retries
`POST /free {unload_models:true, free_memory:true}` is the difference between
recovering and dying. Always called before a retry that follows a
`ComfyUIVRAMError`.

### M-5 · Trusting `/history` immediately after completion
Output files often appear several seconds after the WS says "done." Use the
retry-10x-with-WS-executed-fallback + `try_download_output` chain. For Klein
images this is less critical than for VHS video, but use the same defensive
chain — it's free.

### M-6 · Forgetting to disable SSL verify for RunPod URLs
RunPod proxies use self-signed certs. If a URL contains `runpod.net` or starts
with `https://`, disable verify and suppress `InsecureRequestWarning`.

### M-7 · Uploading reference images **after** submitting the workflow
ComfyUI doesn't auto-fetch. Upload first, then use the returned filename in
node inputs.

### M-8 · Single completion-signal detection in the WS loop
ComfyUI versions emit different "I'm done" messages. Use **all** of:
`executing.node==None`, `execution_complete`, `executed`, `complete`,
`execution_success`, and the `status` + `queue_remaining==0` fallback. Plus
the absolute timeout.

### M-9 · Letting the SSE broadcaster grow unbounded
Per-subscriber `asyncio.Queue(maxsize=200)`. On `QueueFull`, evict the
subscriber. Otherwise one stuck browser tab freezes the whole pipeline.

### M-10 · Persisting masked API keys
On `PUT /api/settings`, if the value matches `****xxxx` shape, treat it as "no
change" — don't overwrite the real one with the mask.

### M-11 · Putting line breaks in prompts
For images this is mostly cosmetic, but the LLM enhancer collapses to a single
paragraph by default for consistency with the video sibling app and to avoid
weird tokenization edge cases. Keep the post-processing step.

### M-12 · Trusting batch-import reference paths to exist locally
Paths in a batch CSV point at the **ComfyUI host's** filesystem, not ours.
Treat missing-local-path as a **warning**, not an error. We only need a hard
error if `width`/`height` are wrong or the prompt is empty.

### M-13 · Forgetting the "running on restart" recovery
On Uvicorn boot, any `running` job in the DB is a ghost. Mark it `failed` with
a reason of "interrupted" so users can retry it explicitly.

### M-14 · Letting one slow worker block the whole queue
Dispatcher selects by `(in_flight, -last_check)`. Don't add "fastest worker
wins" without also tracking p50 — otherwise a flapping worker grabs everything
and stalls.

### M-16 · Letting `queue_prompt` and `stream_prompt` mint different `client_id`s

ComfyUI's WebSocket only sends progress/completion to the client_id that
submitted the prompt. Different IDs = the WS sees nothing forever; jobs
look stuck at "running 0%" even though generation finished. One persistent
`client_id` per `ComfyUIClient` instance, used by BOTH methods.

### M-17 · Using FLUX 1 text encoders (T5XXL + CLIP-L) with Klein 9B

Klein 9B's text encoder is **Qwen 3 8B** (12288-dim output). T5XXL is
4096-dim. Mixing them throws `mat1 and mat2 shapes cannot be multiplied
(512x4096 and 12288x4096)`. Use `CLIPLoader` (single) with
`qwen_3_8b_fp8mixed.safetensors` and `type="flux2"`.

### M-18 · Assuming Klein can do pure text-to-image

Klein is fundamentally an **edit** model — its training and the official
workflow always include at least one reference image. `klein_t2i.json`
is just `klein_1ref.json` under another name. If the user doesn't upload
a reference, the LoadImage node will fail in ComfyUI with a missing
file error. Surface this in the UI; don't pretend t2i works.

### M-15 · Confusing "image queued" with "job in flight"
The `Image` row exists from the moment the user clicks Generate. The `Job`
row is created when it's actually dispatched. Re-runs create new `Job` rows
but update the same `Image`. UI surfaces both, but actions like "retry" act
on the latest `Job`.

---

## Late-stage decisions (post Phase 1–10)

### TC-6 · One shared `client_id` per ComfyUIClient — _2026-05-24_

**Decision.** `ComfyUIClient.__init__` mints one UUID `client_id` and reuses
it for both `queue_prompt` and `stream_prompt`.

**Why.** ComfyUI's WS only forwards events to the client_id that submitted
the prompt. The original code minted fresh random IDs in both methods, so
WS connections received nothing — generation completed on the server but
the UI hung at "running 0%". Logged as `M-16` in the mistake catalogue
below; do not split the IDs again.

### TC-7 · Klein 9B workflow uses official Black Forest stack — _2026-05-24_

**Decision.** Workflow JSONs use `UNETLoader` + `CLIPLoader (qwen_3_8b…)`
+ `VAELoader (full_encoder_small_decoder)` + `ReferenceLatent`-chain
conditioning + `Flux2Scheduler` + `CFGGuider` (cfg=5). Output dimensions
come from the (rescaled-to-1MP) reference image via `GetImageSize` →
`EmptyFlux2LatentImage`. **No** `UnetLoaderGGUF`, **no** `DualCLIPLoader`,
**no** `Flux2ReferenceLatent`-style separate-latent path.

**Why.** Flux 2 Klein's text encoder is Qwen 3 8B (12288-dim output), not
T5XXL (4096-dim). Using T5 throws `mat1 and mat2 shapes cannot be
multiplied`. The reference-conditioning model in Klein wraps positive AND
negative conditioning via `ReferenceLatent`; output dim follows the
ref's shape because that's how the model was trained.

**Reconsider when.** A new Klein release ships with a different text
encoder or different ref-conditioning topology.

### TC-8 · Output format conversion happens runner-side via PIL — _2026-05-24_

**Decision.** ComfyUI always saves PNG via `SaveImage`. After the runner
downloads the bytes, it converts via Pillow if `output_format` is
`jpg` (quality 92, optimize, progressive) or `webp` (quality 90, method
6). Filenames honor `retain_ref1_name` (uses `Path(ref1).stem` + new ext)
with a collision guard.

**Why.** ComfyUI's built-in `SaveImage` only emits PNG; adding a custom
"SaveImageWebP" node introduces a dependency. Pillow is already a
requirement (thumbnails). Keeping the conversion server-side guarantees
the user gets the format they asked for regardless of which ComfyUI
release / nodes are installed.

### TC-9 · Reference base directory resolved at commit, not at runtime — _2026-05-24_

**Decision.** When a Batch Render commit lands with `reference_base_dir`
set, bare filenames in each row are resolved against the base AT COMMIT
TIME and the absolute path is stored on `Image.reference_paths`. The
runner sees only fully-qualified paths.

**Why.** Keeps the runner's input shape simple (it just reads bytes and
uploads). Makes the resolved path visible in the UI / API immediately
after commit, which is what the user expects to see. Absolute paths and
any path containing a separator (`/` or `\`) skip the resolver — they're
treated as already-qualified.

### TC-10 · `data/` is gitignored entirely — _2026-05-24_

**Decision.** Every runtime artefact (DB, logs, projects, references,
outputs) lives under `data/` and is gitignored. A single `data/.gitkeep`
file preserves the folder for a fresh clone.

**Why.** Nothing in `data/` is reproducible from the repo; everything
gets created by `installer.bat` + `run.bat`. Committing any of it
either leaks API keys (via DB row) or bloats the repo (output PNGs).

---

## Resolved open questions

(none yet — open ones live in [BLUEPRINT §15](BLUEPRINT.md#15-open-questions-to-revisit-before-coding).
Move them here once decided, prefixed `RQ-N`.)

---

## Operational lessons — 2026-05-25 patch series

### OPS-1 · Patch the EmptyLatent node by class_type, not by a title that may not exist

When customizing a vendor-supplied workflow JSON(the official ComfyUI Klein workflows), don't trust that a node with a particular title exists. The Klein workflows have no node titled "Width" or "Height" — width/height are wired from a `GetImageSize` node reading the reference image's dimensions, so the empty-latent output adopts the reference's aspect ratio. Our previous patcher's `set_node_input(workflow, "Width", "value", …)` was a silent no-op and outputs always matched the ref's shape.

**Lesson.** When you must override a value, patch by `class_type` (with title as a preferred-but-not-required hint) and overwrite the wire-reference list with a literal scalar. ComfyUI's executor uses literals as-is, ignoring the wire. Always log what got patched (`comfy.workflow.size_patched`) so a future regression is visible in `factory.log`.

### OPS-2 · Configure stateful singletons at app startup, not lazily on first save

The dispatcher used to be configured only when the user saved Settings, which meant boots after a restart left it with zero workers known. We now configure + probe in `lifespan` before `start_runner()`, plus a 30s periodic re-probe so dead servers self-heal.

**Lesson.** Any in-process registry whose source-of-truth is the DB (dispatchers, schedulers, caches) should be hydrated in the FastAPI lifespan, not deferred to the first user interaction.

### OPS-3 · Sequential await blocks parallelism even when the worker pool supports it

The runner originally did `await _process_one(job_id)` serially. With 3 healthy workers and 30 pending jobs, two workers sat idle. Pattern: `asyncio.create_task` per slot, then `asyncio.wait(..., return_when=FIRST_COMPLETED, timeout=1s)` so the loop reacts to both a worker freeing up AND new queue items / stop signals.

### OPS-4 · Hard-stop must drain the in-memory queue AND interrupt ComfyUI

A "pause" that only stops new dispatches but leaves three running ComfyUI workers churning isn't really a stop. `POST /api/jobs/queue/stop` does pause + heap drain + DB cancel + worker `/interrupt` in one atomic call.

### OPS-5 · Don't silently lose list fields in apply_update

`comfyui_urls` list-handling block got dropped during a framing migration. The endpoint returned 200, URLs vanished, only way to notice was to refresh. We now log every successful URL save (`settings.comfyui_urls.saved`).

### OPS-6 · Resolve user-typed paths with a candidate ladder, not a single guess

CSVs carry surprises: base of `C:\stuff` with files under `C:\stuff\uploads`; absolute paths from another machine; BOM + smart quotes from Excel; mixed-case extensions. The new resolver tries 6 candidates per ref, scrubs invisible characters, falls back to case-variant extensions, and lists similar files in the directory on miss.

### OPS-7 · Multiple independent freshness mechanisms beat one perfect channel

SSE alone is fragile: proxies kill long streams, browsers throttle hidden tabs, EventSource doesn't reconnect with backoff. The gallery now has SSE auto-reconnect + 8s polling fallback + Page-Visibility refresh, with a 1.5s de-dupe to handle overlaps.

---

## Template for a new entry

```
### CODE-N · Short headline — YYYY-MM-DD

**Decision.** ...

**Why.** ...

**Tradeoff accepted.** ... (optional)

**Reconsider when.** ... (optional)
```
