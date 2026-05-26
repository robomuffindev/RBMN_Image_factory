# Changelog

All notable changes to **Robomuffin Image Factory** are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

> **Convention.** Every meaningful change gets one line under the most recent
> `[Unreleased]` group (`Added` / `Changed` / `Fixed` / `Removed` / `Docs` /
> `Internal`). When we ship, rename `[Unreleased]` to `[X.Y.Z] – YYYY-MM-DD`
> and start a new empty `[Unreleased]`.

---

## [Unreleased]

### Fixed — Worker leak from try-block scope _(2026-05-26)_
- **Root cause of "3 servers drop to 2, then 1 idle".** The runner reserved
  a worker (`dispatcher.select_worker(reserve=True)` → `in_flight += 1`),
  then did three `await`s (`_set_job_status` / `_set_image_status` /
  `_broadcast`) BEFORE entering the `try:` block whose `finally` calls
  `dispatcher.release(worker.url)`. If the task was cancelled or any of
  those awaits raised (SQLite busy, transient broadcaster issue), the
  finally never ran and the worker's `in_flight` counter stayed
  permanently incremented. Over hours, each worker reached
  `in_flight = max_parallel`, `select_worker` returned None for it, and
  it dropped out of dispatch rotation. Fix: the `try:` block now wraps
  EVERYTHING after the reserve, so the finally is guaranteed regardless
  of how the task exits. `runner.py` lines ~374-610.

### Fixed — SSE subscriber drop on long batches _(2026-05-26)_
- **Root cause of "have to refresh to see status changes".** The event
  broadcaster gave each SSE subscriber an `asyncio.Queue(maxsize=200)`.
  A 176-image batch generates ~6000 events (job_started + ~30
  progress ticks + job_completed per image), which overflowed that
  cap; on QueueFull the broadcaster **dropped the entire subscriber**.
  From that moment forward the page received no live updates and the
  user had to manually refresh. Fix: queue cap raised to 5000 AND
  changed semantics — on full queue, evict the OLDEST event (typically
  an intermediate progress tick) instead of disconnecting the
  subscriber. Now the SSE channel survives any batch size; slow clients
  miss some intermediate progress ticks but always see start + completed
  events. New `events.subscriber_added` / `events.subscriber_removed`
  logs make SSE connect/disconnect visible in `factory.log`.

### Fixed — Missing SSE block in template _(2026-05-26)_
- **Why the SSE fix above was masked.** Even after the broadcaster fix,
  the browser was never opening a `GET /api/jobs/stream` request — the
  log showed only polling endpoints (`/api/jobs/summary`, `/api/workers`,
  etc.) firing. Root cause: `subscribeSSE()`, `startPolling()`, and
  `maybeRefresh()` were CALLED in the page's init block but their
  function DEFINITIONS had been clipped from `project_view.html` during
  a previous Cowork sync round-trip. Calls raised `ReferenceError`,
  caught silently by the init block's `try/catch`, and the browser
  never subscribed. Fix: restored all three function definitions inline
  in the template, immediately before the init block that calls them.
- Added a `console.log("[robomuffin] project view init complete")` at
  the end of init so you can verify in DevTools that the IIFE ran to
  completion (= no JS error mid-init silently breaking subscriptions).

### Fixed — Lightbox "Pick" button silently fell through _(2026-05-26)_
- Lightbox rerun panel's per-slot Pick buttons called `openRefPicker(cb)`
  but the actual function in the template is `openPicker(cb)`. Before
  the fix, clicking Pick in the lightbox rerun panel always landed on
  the alert fallback. Now it opens the Uploaded Assets picker modal.

### Fixed — Missing serve_image_file endpoint _(2026-05-26)_
- A previous Cowork sync clipped `serve_image_file`, `serve_image_thumb`,
  and `image_card` endpoints out of `app/api/images.py`. Browser
  requests to `/api/images/{id}/file` 404'd → broken-image icons in
  the gallery for every DONE row. Fix: all three endpoints restored
  with explicit `mimetypes.guess_type` + manual webp override + three
  distinct 404 log events (`images.serve.image_not_found`,
  `images.serve.missing_path`, `images.serve.file_not_found`) so you
  can pinpoint why any particular thumb didn't load without DevTools.
- Gallery `<img>` tags now include a `?cb={completed_at}` cache-buster
  so a browser-cached 404 doesn't persist after the file finally writes,
  plus an `onerror` handler that replaces the broken-image icon with a
  red-tinted "output_path set but file not loadable" card showing the
  actual path it tried.

### Fixed — _enqueue_image was committing mid-batch-loop _(2026-05-26)_
- The helper that creates a `Job` row and pushes it onto the in-memory
  queue called `session.commit()` internally. Inside the batch commit
  loops, this corrupted the parent transaction: some images committed
  mid-loop, identity-map state went inconsistent, and on certain edge
  cases the user's whole batch appeared to vanish on the next page
  reload. Fix: `_enqueue_image` now uses `session.flush()` only (to
  populate `job.id` for the in-memory `queue.enqueue` call); each
  caller (`create_image`, `dispatch_image`, `rerun_image`, batch
  `/commit`, `/render/commit`, plus retry/retry-all/clear-failed)
  owns exactly one `session.commit()` at the end of its work, so the
  whole unit-of-work is atomic.

### Added — Batch-wide dimension override _(2026-05-26)_
- New "Override all dimensions for this batch" checkbox + width/height
  inputs on the Batch Render tab. When ON, every imported row's
  width/height is replaced by the override values BEFORE rows hit the
  server. Useful when an imported CSV inherited inconsistent dimensions
  from the reference images and you want a uniform output size. Hints
  the common Klein aspect ratios (1024×1024, 1024×1536, 1536×1024,
  832×1216, 1216×832).

### Added — Auto-pause queue on every batch commit _(2026-05-26)_
- Both `/api/batch/commit` (Import → Preview → Commit & queue) and
  `/api/batch/render/commit` (Batch Render → Commit & queue) now set
  `AppSettings.dispatch_paused = True` BEFORE adding any rows. Items
  land at PENDING in the DB but never auto-flow to workers. The user
  reviews the gallery, then clicks **Start Queue** to release them.
  Eliminates the "I clicked commit and it started running immediately"
  surprise. Logs the flip as `batch.{commit,render}.auto_paused_on_commit`.

### Added — Probe hysteresis + skip-busy-workers _(2026-05-26)_
- Dispatcher's `probe_all` no longer marks a worker unhealthy on a
  single transient failure. Each `ComfyWorker` carries a
  `consecutive_failures` counter; healthy flips to False only after
  `UNHEALTHY_THRESHOLD = 3` failures in a row (~90 seconds of pain),
  and a single success resets the counter. Also: workers currently
  serving a job (`in_flight > 0`) are SKIPPED by the probe entirely —
  probing a busy server risked a slow `/system_stats` response being
  treated as a failure and pulling the worker out of rotation.
- Worker health strip in the UI gained a yellow dot state for
  "transient probe failure but still in rotation" so you can see flaps
  in real time. Tooltip shows the consecutive failure count.

### Added — Purge All + Clear Cancelled cleanup endpoints _(2026-05-26)_
- `POST /api/batch/cancelled/clear?project_id=…` deletes every
  CANCELLED image in the project across all batches, cascades Jobs,
  and best-effort removes orphaned output / thumbnail files from disk.
- `POST /api/batch/purge-all?project_id=…` — the panic button — drains
  the in-memory queue, cancels any in-flight Jobs, deletes every
  output file from disk, deletes every Job row and every Image row,
  and resets `project.image_count` to 0. Two-confirm UI dialog. Use
  case: a bad CSV import that committed 100 wrong rows and you want a
  clean slate to try again.

### Added — Subject framing _(2026-05-25)_
- **"Make sure main subject of image is largely in frame"** toggle in
  Settings → new *Subject framing* section. Three strength presets
  (Subtle / Moderate / Strong) plus an advanced section to override the
  scaffold text. When ON, every generation gets composition language
  appended to the positive prompt ("hero composition, subject fills
  60–70% of frame, medium close-up, minimal background clutter…") and an
  anti-zoom-out negative prompt ("wide shot, tiny subject, zoomed out,
  excessive background…"). Per-batch override dropdown on the Batch
  Render tab: *inherit from Settings* / *ON for this batch* / *OFF for
  this batch* — stored on `Image.frame_subject`.
- New `app/services/framing.py` houses the scaffolds + `apply_framing(...)`
  helper + `enhancer_instruction(...)` so the LLM enhancer also gets a
  system-prompt directive when framing is on (cleaner phrasing, scaffold
  still applied as backstop). Runner stashes framed text on `image._framed_prompt`
  and `image._framed_negative` so retries re-apply automatically.
- AppSettings gained `frame_subject_default`, `frame_subject_strength`,
  `frame_subject_positive_override`, `frame_subject_negative_override`;
  Image gained nullable `frame_subject` ("on" / "off" / NULL=inherit).
  Additive `ALTER TABLE` migrations on boot.

### Added — Hard Stop Queue + Pause/Start controls _(2026-05-25)_
- **Three-button strip** at the top right of every project view:
  *Pause Queue* (yellow — stops auto-dispatching new commits, lets running
  jobs finish), *Stop Queue* (red — the panic button), *Start Queue*
  (green — releases pending jobs onto workers). A live status pill shows
  *▶ Auto-dispatch · N pending* or *⏸ PAUSED · N pending*, polling every 5s.
- **Stop Queue** (`POST /api/jobs/queue/stop`) does five things atomically:
  sets `dispatch_paused=True`, drains the in-memory queue, marks every
  PENDING/RETRYING Job CANCELLED, marks every RUNNING Job CANCELLED,
  mirrors that on Image rows, and POSTs `/interrupt` to every healthy
  ComfyUI worker so any in-step generation aborts immediately instead of
  burning compute. Confirm dialog spells out what's about to happen;
  response toast shows exactly how many of each were stopped.
- **Pause/Start** (`POST /api/jobs/queue/pause`, `…/start`, plus
  `GET /api/jobs/queue/status`): pausing leaves Jobs at PENDING in the DB
  but skips the in-memory dispatcher; Start re-queues them in priority order.
- `AppSettings.dispatch_paused` boolean (additive migration). `_enqueue_image`
  honors it so batch commits sit at PENDING until the user clicks Start.

### Added — Parallel dispatch across multiple ComfyUI servers _(2026-05-25)_
- **Runner now fans jobs out concurrently** instead of pulling one at a
  time and blocking until completion. Each tick the loop asks the
  dispatcher how many slots are free (`available_slots()` = sum of
  `max_parallel_per_worker − in_flight` across healthy workers), pops
  that many jobs from the queue, and spawns one `asyncio.Task` per job.
  The instant any worker frees up (`asyncio.wait(..., FIRST_COMPLETED)`)
  the next pending job is dispatched. With three healthy servers and
  `max_parallel_per_worker=1`, you'll see three concurrent jobs running
  with the load auto-balanced.
- `_process_one_safely` wrapper funnels any unhandled exception into
  `_retry_or_fail` so a single misbehaving job can't kill the runner.
  Graceful drain on Stop with a 10s timeout before force-cancel.
- New `dispatcher.workers` and `dispatcher.healthy_workers` properties
  (thread-safe snapshot lists) used by `/api/workers` and the Stop endpoint.

### Added — Dispatcher startup config + auto re-probe + worker status strip _(2026-05-25)_
- **Dispatcher gets configured at app boot** from the persisted
  `comfyui_urls`, then immediately probes every worker. Previously the
  dispatcher had zero workers known until the user saved Settings —
  so the runner sat idle even though servers were configured in the DB.
- **Periodic re-probe** runs every 30s in the background so a worker
  that was offline at boot self-heals automatically once it's reachable.
- **Worker health strip** at the top of every project view shows each
  ComfyUI URL with a green/red health dot, current `in_flight` count
  (e.g. *2 in flight* / *idle*), and the last error on hover. New
  **Re-probe** button calls `POST /api/workers/probe` to force an
  immediate health check on every URL — useful to verify a Settings
  save without waiting the 30s polling interval.

### Fixed — Output size mismatch _(2026-05-25)_
- **Renders were ignoring the configured width/height.** Cause: the
  official Klein workflows wire `EmptyFlux2LatentImage.width/height`
  from a `GetImageSize` node that reads the reference image's
  dimensions — so output aspect always matched the ref, regardless of
  what you set in the UI. `prepare_klein_workflow` was also looking for
  a node titled "Width"/"Height" that doesn't exist in the official
  workflow, so the override was a silent no-op. Fix: the function now
  patches the `Empty Latent` / `EmptyFlux2LatentImage` node directly,
  overwriting the wire-reference with literal integer values. Logs
  `comfy.workflow.size_patched width=… height=…` per render so you can
  verify in `data/logs/factory.log` what dimensions reached the sampler.

### Fixed — Settings save was dropping new ComfyUI URLs _(2026-05-25)_
- `apply_update()` had lost its `comfyui_urls` list-handling block during
  an earlier framing migration. The endpoint returned 200, but newly
  added URLs were silently discarded. Restored the block plus the
  masked-API-key branch and the JSON-dict handlers
  (`image_system_prompt_overrides`, `image_prompt_guidance`,
  `comfyui_server_caps`). URLs are now stripped, trailing-slash-normalized,
  and deduplicated preserving order; logged via `settings.comfyui_urls.saved`.

### Added — Smarter reference path resolution + preflight diagnostics _(2026-05-25)_
- `_resolve_ref` in `/api/batch/render/commit` now tries a ladder of
  candidates per reference (base/file → base/uploads/file →
  parent-of-base/file → parent-of-base/uploads/file → project uploads dir
  → global uploads dir) and returns the first that exists. Handles
  whitespace, BOM, NBSP, zero-width spaces, smart quotes copy-pasted from
  Excel/Notepad. Case-variant extension fallback (`.WEBP` ↔ `.webp`) and
  case-insensitive directory scan as last resort. Commit response now
  carries `refs_resolved`, `refs_missing`, and `refs_unresolved[]` so the
  UI surfaces a warning toast + `alert()` listing exactly which files
  weren't found. Unresolved refs log `batch.ref_unresolved` with a
  `similar_files_in_dir` sample to spot rename typos.
- **Per-image reference badges on every card.** `_serialize_image` now
  returns `references_detail` — a list of `{path, name, exists}`. The
  gallery and results grids render each reference under the prompt with
  a green ✓ or red ✗ + the basename, hover for full path. Lets you spot
  "queued but workers can't find the file" at a glance.

### Added — Live gallery / results auto-update _(2026-05-25)_
- Three independent update mechanisms so a stale gallery is essentially
  impossible: (1) EventSource subscription with exponential-backoff
  auto-reconnect on error (1s → 30s cap, resets on next open); (2)
  polling fallback every 8s, but only when there are running/queued
  images — finished projects don't poll; (3) Page Visibility listener
  that triggers an instant refresh the moment you focus the tab and
  re-subscribes the EventSource if the browser killed it while hidden.
  1.5s de-dupe so a burst of 30 done events fires one HTTP fetch.
- **Status-sorted cards.** Both Gallery and Results grids now order by
  `STATUS_RANK = { running:0, done:1, failed:2, cancelled:3, queued:4 }`
  with newest-first as tiebreak — so live work is at the top, history
  in the middle, pending work at the bottom.

### Fixed — App boot regressions _(2026-05-25)_
- `static_dir` / `templates_dir` properties added to `FactorySettings`
  in `app/config.py` — `app.mount("/static", …)` was hitting AttributeError.
- `register_web_routes` typo in `main.py` swapped to
  `app.include_router(web_routes.router)` matching what `app/web/routes.py`
  actually exports.
- `start_runner` / `stop_runner` / `_runner_loop` (with the new parallel
  dispatch) rebuilt after a Cowork sync had truncated the runner tail.

### Fixed _(2026-05-24, part 5)_
- **ComfyUI rejecting WebP references.** Symptom on the user side:
  `Invalid image file: <name>.webp`. Cause: ComfyUI's `LoadImage` validator
  rejects WebP/GIF/BMP even when PIL can read them. Runner's `_upload_refs`
  now **normalizes every reference to PNG via Pillow** before uploading
  (preserves alpha when present), writes a temp PNG, uploads that, and
  cleans up. PNG is the universal format ComfyUI accepts everywhere.
  Original references on disk are untouched.

### Added — Batch Runs management _(2026-05-24, part 5)_
- **New "Batches" tab** on each project page (between Results and Uploaded
  Assets). Lists every batch run grouped by `batch_run_id`, newest first.
  Each card shows: short ID, output format, output directory, start time,
  elapsed seconds/minutes/hours (live for active runs), stat row
  (total / done / running / queued / failed), and progress bar.
- **Per-batch actions** on each card:
  - **Cancel queued** — flips QUEUED images to CANCELLED (in-flight RUNNING
    jobs are left alone).
  - **Retry failed** — re-dispatches every FAILED image in the batch.
  - **Re-run entire batch** — re-dispatches every image regardless of status.
  - **Clear failed** — deletes every FAILED image row + cascades jobs.
  - **Delete batch** — wipes every image in the batch (with on-disk file
    cleanup) and cascades jobs.
- **"Hide completed" filter** on the Batches tab so the page stays clean
  when older runs accumulate.
- **"Clear all failed images" header button** that wipes every FAILED image
  in the project across all batches (and outside batches).
- **Auto-refresh** on the Batches tab — polls `/api/batch/runs` every 5s
  while the tab is open so progress numbers move live; stops when you
  switch tabs.
- API endpoints (all under `/api/batch/`):
  - `GET /runs?project_id=…` — list runs with stats.
  - `POST /runs/{id}/cancel-queued`
  - `POST /runs/{id}/retry-failed`
  - `POST /runs/{id}/retry-all`
  - `POST /runs/{id}/clear-failed`
  - `DELETE /runs/{id}`
  - `GET /failed?project_id=…` — list all failed images in project.
  - `POST /failed/clear?project_id=…` — delete all failed images in project.

### Added — GPT Image 2 engine toggle _(2026-05-24, part 4)_
- **Settings → "Image generation engine"** section with a single checkbox that
  swaps the entire pipeline from ComfyUI / Klein over to OpenAI's
  `gpt-image-2` (or `gpt-image-1`) API. Big amber warning right next to the
  toggle: "Can get expensive — OpenAI charges per image (~$0.04 standard,
  ~$0.19 high-quality). Set a usage limit on your OpenAI account."
- Companion controls: **Model** (gpt-image-2 / gpt-image-1), **Size**
  (auto / 1024×1024 / 1024×1536 / 1536×1024), **Quality**
  (auto / low / medium / high).
- New service `app/services/openai_images/client.py` —
  `generate_with_gpt_image(...)` wraps `AsyncOpenAI.images.generate` (text
  → image) and `images.edit` (reference → image, mirroring Klein's edit
  behavior). References get uploaded byte-by-byte from the local
  Robomuffin host, same as the ComfyUI path. Every call is logged via
  `log_llm_call` so it shows up in `/api/debug/llm/calls` for cost audit.
- Runner branches on `AppSettings.use_gpt_image` at the start of
  `_process_one`. When on: skip dispatcher entirely, call the new client,
  honor the same output-handling tail (custom_output_dir,
  retain_ref1_name, PNG/JPG/WebP conversion, batch_run.log).
- AppSettings gained `use_gpt_image`, `gpt_image_model`, `gpt_image_size`,
  `gpt_image_quality` — applied via additive `ALTER TABLE` migrations so
  existing DBs upgrade in place.
- Setup banner on the project page now respects the toggle: when GPT
  Image is on, it only requires an OpenAI key (it stops nagging about
  missing/unhealthy ComfyUI servers).

### Docs / Repo polish _(2026-05-24, part 3)_
- **README rewritten for GitHub.** Top-of-file blurb credits **Robomuffin
  Labs** (<https://robomuffinlabs.com>), highlights table, "What you need
  before running" section with model download URLs (Klein UNET + Qwen 3 8B
  text encoder + Klein VAE) and the exact folders to drop each file into,
  step-by-step install (Windows + macOS / Linux), first-time setup
  walkthrough, the five project tabs explained, batch CSV/JSON format,
  `.env` reference, directory map, debug endpoint cheat sheet,
  architecture one-pager, post-`git pull` upgrade steps, troubleshooting
  matrix, contributing pointers, and a license note.
- **`.gitignore` hardened** for a clean first commit. Ignores `data/`
  (everything runtime — DB, logs, projects, references, outputs) but
  keeps `data/.gitkeep` so the folder structure is preserved.
  Ignores `.env`, `.venv/`, every cache directory, IDE droppings, OS
  junk, and image / archive blobs anywhere in the tree EXCEPT inside
  `Import_samples/` and `devnotes/`.
- **`LICENSE` placeholder** added pointing future commercial users at
  Robomuffin Labs and noting the upstream model licenses (Black Forest
  Labs FLUX.2 Klein + ComfyUI team).
- **`data/.gitkeep`** so a fresh clone gets the right folder layout.

### Added _(2026-05-24, part 2)_
- **Batch Render — reference base directory.** New optional field on
  `BatchRenderCommit` / the Batch Render tab UI: type a folder path once, then
  every row can use bare filenames (`kitten.png`) instead of full paths
  (`C:\Users\hexum\refs\kitten.png`). Bare names are resolved against the base
  at commit time; absolute paths and any path containing `/` or `\` are kept
  as-is.
- **Louder runner ref-upload logging.** Every reference upload now logs
  `runner.ref_upload.start` (with size), `runner.ref_upload.ok` (with the
  remote filename ComfyUI assigned) or `runner.ref_upload.failed`. Missing
  local files log `runner.ref_missing_locally` with a hint that the bare
  filename will be tried (which only works if the file is already in the
  ComfyUI host's `input/` folder).
- **Image API surfaces the new fields.** `_serialize_image` now returns
  `output_format`, `retain_ref1_name`, `custom_output_dir`, and
  `batch_run_id` so the UI can read them back per image.
- **Import_samples updated** to show both styles side-by-side: bare
  filenames (resolved against `reference_base_dir`) and absolute paths
  (kept verbatim). Comments inside the JSON explain the resolution rules.

### Added _(2026-05-24)_
- **Batch Render tab.** Build a list of render tasks manually (+ Add row) or import
  from CSV/JSON; per-batch options: output format (PNG / JPG / WebP — WebP is
  web-optimized), output directory, retain ref1 filename for site replacements, and
  AI-optimize each prompt via the configured LLM (off by default per spec).
  Endpoint: `POST /api/batch/render/commit?project_id=...`.
- **`Import_samples/` folder** at repo root with `batch_render.csv` and
  `batch_render.json` showing every recognized column / option.
- **Per-image output controls** persisted on the `image` table:
  `output_format`, `retain_ref1_name`, `custom_output_dir`, `batch_run_id`.
  Additive `ALTER TABLE` migrations applied on boot so existing DBs upgrade in
  place.
- **Runner honors output controls**: writes to custom dir if set, uses
  `Path(ref1).stem` as filename if `retain_ref1_name`, converts PNG→JPG/WebP via
  PIL (quality 92 for JPG, quality 90/method 6 for WebP), and appends a
  `batch_run.log` line in the output dir for every image in the same batch run.
- **Uploaded Assets tab.** Gallery of every reference image you've uploaded to
  the project: thumbnail + filename + size + "in use by N images" badge.
  Click to view full-size, × to delete (blocked if any image row currently
  references it — UI shows the conflict).
- **Reference picker modal.** The Gallery's "+ Add reference image" button now
  opens a two-tab dialog: **Upload from computer** (one-shot file picker) or
  **Choose existing asset** (grid of your uploaded references — click one to
  attach without re-uploading).
- Endpoints: `GET /api/projects/{pid}/references`,
  `GET /api/projects/{pid}/references/{file}`,
  `DELETE /api/projects/{pid}/references/{file}` (returns `{deleted:false,in_use_by:[...]}` on conflict).

### Fixed _(2026-05-24)_
- **ComfyUI WS client_id mismatch — the "stuck at running 0%" bug.**
  `queue_prompt` and `stream_prompt` were each minting their own random
  `client_id`. ComfyUI only sends WS messages (progress, executed, completion)
  to the client_id that submitted the prompt. With different IDs our WS stream
  saw nothing — generation completed on the server but our UI never knew.
  Fix: one persistent `client_id` per `ComfyUIClient` instance, used by both
  `queue_prompt` AND `stream_prompt`. Logged at every step now.
- **Per-job log files** at `data/projects/{project_id}/logs/{job_id}.log`.
  The runner wraps each job in `job_log_capture(job_id, project_id)` which
  attaches a filtered file handler for the duration of that one job. Everything
  the runner / dispatcher / ComfyUI client logs during that job (now including
  every WS frame at debug level) lands in the file. Survives the job — open it
  any time later to see exactly what happened.
- **`GET /api/debug/jobs/{job_id}`** — returns the job's DB row + the linked
  image + the tail of the per-job log file. Single URL diagnostic.
- **WS streaming is much louder** — every WS frame, every history poll, every
  output capture is now logged. Idle cycles tick every 10s, history is probed
  every 15s regardless of state (catches completion even when WS is silent).
- **The "flash and disappear" UI bug.** Root cause: `factory.js` was loaded
  AFTER `alpinejs` in `base.html`. With `defer` scripts running in document
  order, Alpine fired its `alpine:init` event **before** `factory.js`'s
  listener was registered, so `Alpine.data('projectPage', ...)` never ran.
  Alpine then encountered `x-data="projectPage(...)"` on the page, found no
  registered factory, evaluated to nothing, and every `x-show="tab==='gallery'"`
  section hid itself. Fix: moved `factory.js` to load *before* Alpine, added
  `[x-cloak] { display: none !important; }` so the page stays hidden until
  Alpine fully initializes (no more flash of unstyled content).
- **Browser console now traces every step.** Added a `[robomuffin]` prefixed
  logger that logs every method entry, every fetch round-trip (with status +
  duration), every state transition. Open DevTools → Console to see what the
  page is actually doing.
- **JS errors are now persisted server-side.** Added `window.onerror` +
  `unhandledrejection` listeners in `base.html` that POST to
  `POST /api/debug/client-log` via `navigator.sendBeacon`. That endpoint logs
  the payload through structlog so JS errors end up in `data/logs/factory.log`
  AND in `GET /api/debug/errors/recent` alongside server-side tracebacks.
- **`window.fetch` is wrapped with a console-logging shim** in `base.html` so
  every HTTP request from the page is visible in DevTools.

### Fixed _(2026-05-23)_
- **Gallery now actually updates when you click Generate.** Previous version
  mixed Jinja `{% for img in images %}` (server-rendered at page load) with an
  Alpine `this.images` array (client-side). Newly-created images joined the JS
  array but never appeared in the DOM, making the whole app feel inert. Rebuilt
  the Gallery + Results grids with Alpine `<template x-for="img in images">` so
  every state change is visible immediately.
- **Project page now shows a setup banner** when no ComfyUI URL is configured,
  no worker is reachable, or no LLM provider is configured — with a direct link
  to `/settings`. Closes the "I clicked Generate and nothing happened" gap.
- **Settings page expanded** with the previously-missing sections:
  - **System prompt override** (per-model textarea + enable checkbox + "Show built-in default" button calling `/api/settings/builtin-prompt`)
  - **Prompt guidance** (free-text appended to system prompt)
  - **Image model** dropdown (currently just `flux2_klein_9b`)
  - **"Probe all workers"** button calling `/api/workers/probe`
  - Sticky save bar with Save/Reload/Cancel and a toast on success
- **Image card actions are now wired** in client JS: Re-run posts to
  `/api/images/{id}/dispatch`, Delete posts to DELETE `/api/images/{id}`,
  Download links to `/api/images/{id}/file?download=1`, click-to-zoom opens the
  lightbox. All actions show toast feedback.
- **`process_info.py` was Unix-only** — `import resource` doesn't exist on Windows
  and crashed `run.bat` immediately. Now wrapped in a `try/except ImportError`
  with a graceful fallback: pid/uptime/cwd/platform always populated, RSS and
  CPU time become `null` on Windows. Reported by Lorenzo right after first install.
- **`run.bat` simplified.** Earlier attempt to pre-check the port with `netstat |
  findstr` false-positived on _every_ port the user tried — the findstr regex was
  too loose / the cmd quoting interacted badly with delayed expansion. Reverted to
  a no-frills version: just start uvicorn. If the server exits non-zero, print
  the PID lookup + `taskkill` recovery commands as a post-mortem. Still accepts
  an optional port arg (`run.bat 8766`). Confirmed the Python app binds the port
  exactly once (`uvicorn.run` in `app/main.py:96`); no double-bind in code.

### Added — Phases 2–10 (full feature build) _(2026-05-22)_

**Phase 2 — Settings API + UI**
- `app/services/settings_service.py` — `get_or_create`, `to_response` (masked keys), `apply_update` (ignores `********`-shaped values).
- `GET / PUT /api/settings`, `POST /api/settings/test-comfyui`, `POST /api/settings/test-llm` (all 4 providers).
- `/settings` page (Tailwind + Alpine) with per-server test buttons, per-provider key+model+test, image defaults, project dir.

**Phase 3 — ComfyUI integration**
- `services/comfyui/client.py` — full HTTP + WS port from devnotes. RunPod SSL bypass. WebSocket completion via 5+ signal types + idle-history fallback + absolute timeout. `ComfyUIVRAMError` extraction.
- `services/comfyui/dispatcher.py` — `ComfyWorker` registry, `select_worker(reserve=True)`, capabilities, `probe_all()`.
- `services/comfyui/workflow.py` — `prepare_klein_workflow` w/ anti-text suffix + ref slots 1-4, `flatten_group_nodes`, `remove_missing_nodes` (with comments-as-fallback).
- Workflow JSON templates for T2I + 1/2/3/4 reference variants under `services/comfyui/workflows/`.

**Phase 4 — Queue + SSE streaming**
- `services/jobs/queue.py` (heap + wake event + drain).
- `services/jobs/runner.py` (parallel dispatch loop, per-job log capture, retry/fail funnel, hard-stop interrupt).
- `services/jobs/events.py` (job + log broadcasters).
- `api/jobs.py` adds `/stream`, `/queue/{pause,resume,start,stop,status}`, `/api/workers`, `/api/workers/probe`.

(See entries above for everything shipped in Phases 5-10 plus post-build work.)
