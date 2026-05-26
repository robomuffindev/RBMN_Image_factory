# Build Checklist — Robomuffin Image Factory

> Tick boxes as we go. Each phase ends with a working, demonstrable slice.
> See [BLUEPRINT.md](BLUEPRINT.md) for the why; [DECISIONS.md](DECISIONS.md) for gotchas; [CHANGELOG.md](CHANGELOG.md) for what shipped.

Legend: `[ ]` to do · `[~]` in progress · `[x]` done · `[!]` blocked

> **Status as of 2026-05-25:** Phases 0–10 complete plus the following
> post-build features now shipped (see [CHANGELOG.md](CHANGELOG.md) for
> details): Batch Render tab + Batches management tab + Uploaded Assets
> tab + reference-picker modal + reference base directory with smart
> fallback ladder + reference existence badges on cards + GPT Image 2
> alternative engine + Subject framing scaffolds (Subtle/Moderate/Strong)
> + Pause/Stop/Start queue controls + parallel dispatch across all
> healthy ComfyUI workers + dispatcher auto-configured at boot + 30s
> auto re-probe + worker health strip in UI + EmptyLatent size-patch fix
> + live gallery with SSE auto-reconnect / 8s polling / Page-Visibility
> refresh + status-sorted cards.

---

## Phase 0 — Planning & repo scaffolding

- [x] Read all `devnotes/` (six files, indexed in `00-INDEX.md`).
- [x] Lock tech stack via questionnaire (FastAPI + HTMX, SQLite + FS, 4 LLM providers, batch import supports both new/append).
- [x] Write `BLUEPRINT.md`, `CHECKLIST.md`, `CHANGELOG.md`, `DECISIONS.md`.
- [x] User review of planning docs.
- [x] Create empty directory skeleton from blueprint §3.
- [x] Initialize `pyproject.toml`, `requirements.txt`, `.env.example`, `.gitignore`.

---

## Phase 1 — Foundation (boots, talks to itself) — ✅ done 2026-05-22

- [x] `app/main.py` — FastAPI app factory, mount static, mount templates, health endpoint `GET /api/health`.
- [x] `app/config.py` — Pydantic settings, paths, env loading.
- [x] `app/logging_setup.py` — structlog + RichHandler + rotating file **+ ring buffers + LLM forensic logger + HTTP middleware + debug router** (expanded scope per owner directive).
- [x] `app/db/engine.py` — async SQLite (WAL pragma w/ graceful fallback), session factory.
- [x] `app/db/models.py` — `AppSettings`, `Project`, `Image`, `Job` (no `ServerWorker`; in-memory).
- [x] `app/db/migrations.py` — create tables, seed `AppSettings(id=1)` from `.env`.
- [x] `installer.bat`, `run.bat`, mirror `install.sh` / `run.sh`.
- [x] `README.md` — first draft with install + run + folder explanation **+ debug-endpoint map**.
- [x] **Verify**: migrations create DB + 4 tables + seeded settings row; Uvicorn boots; `/api/health` returns `{ok:true,version}`; X-Request-ID propagates; `/api/debug/raise` populates `/api/debug/errors/recent` with full traceback; `data/logs/factory.log` accumulates structured lines.

---

## Phase 2 — Settings page + ComfyUI test

- [ ] `app/api/settings.py` — `GET /api/settings` (masked), `PUT /api/settings`.
- [ ] `POST /api/settings/test-comfyui` — hits `/system_stats` on a URL.
- [ ] `POST /api/settings/test-llm` — validates each provider key (incl. Ollama ping).
- [ ] `app/web/templates/settings.html` — section cards: ComfyUI servers, LLM providers, defaults, content safety, project dir.
- [ ] Wire HTMX so test buttons inline-render ✓/✗ next to each URL/provider.
- [ ] Save flow: form submits a single PUT; masked values ignored on save.
- [ ] **Verify**: add a real ComfyUI URL → test → save → reload page shows it persisted; bad URL shows ✗ with the error string.

---

## Phase 3 — ComfyUI client + dispatcher (no UI yet)

- [ ] `services/comfyui/client.py`:
    - [ ] Exception hierarchy: `ComfyUIConnectionError`, `ComfyUIWorkflowError`, `ComfyUIVRAMError`.
    - [ ] Constructor with RunPod SSL bypass + optional skip_health_check.
    - [ ] `queue_prompt`, `get_history`, `upload_image`, `get_queue`, `interrupt`, `free_memory`, `get_object_info`, `download_output`, `try_download_output`.
    - [ ] `_make_request` central dispatch (logs body before raising).
    - [ ] `stream_prompt(prompt_id)` generator with **all** completion signals (executing.node=None, execution_complete, queue_remaining=0, execution_error → VRAM check).
    - [ ] 10s WS recv timeout + 15min absolute + history fallback polling.
- [ ] `services/comfyui/dispatcher.py`:
    - [ ] `ComfyWorker` dataclass.
    - [ ] `select_worker(required_caps, reserve=True)` (capability filter → least loaded).
    - [ ] `discover_capabilities()` from `/object_info` (look for Klein node types).
    - [ ] `submit_job`, `stream_and_wait` (with WS executed output capture).
    - [ ] In-flight tracking with `try/finally` decrement.
- [ ] `services/comfyui/workflow.py`:
    - [ ] `find_node_by_title`, `set_node_input`.
    - [ ] `prepare_klein_workflow(prompt, w, h, seed, ref_images)` — sets prompt + anti-text suffix + Width/Height/RandomNoise/Load Image/Reference 2 Image/Reference 3/Reference 4.
    - [ ] `flatten_group_nodes`, `remove_missing_nodes`.
    - [ ] (No VHS / LTX fixups — image-only.)
- [ ] `services/comfyui/workflows/*.json` — pull/curate from official Klein 9B workflows: `klein_t2i`, `klein_1ref`, `klein_2ref`, `klein_3ref`, `klein_4ref`.
- [ ] **Verify (pytest)**: workflow prep mutates the right nodes; missing reference slots aren't accidentally populated; anti-text suffix is appended exactly once.

---

## Phase 4 — Job queue + SSE

- [ ] `services/jobs/queue.py`:
    - [ ] `asyncio.PriorityQueue`, atomic enqueue → DB write + queue push.
    - [ ] `asyncio.Event` wake-up.
    - [ ] `recover_running_jobs()` on startup (mark stale `running`/`pending` as `failed`).
- [ ] `services/jobs/runner.py`:
    - [ ] Dispatch loop (slots computed across workers).
    - [ ] Per-job task: select worker → upload refs → prepare → submit → stream → save outputs → mark done.
    - [ ] Retry (3, exp backoff, `/free` on VRAM).
    - [ ] Cancellation (checks flag between WS yields).
- [ ] `services/jobs/events.py` — `JobEventBroadcaster` (asyncio.Queue per subscriber, maxsize 200, prune stale).
- [ ] `app/api/jobs.py` — list, cancel, retry, delete, purge, **`GET /api/jobs/stream`** SSE endpoint.
- [ ] **Verify**: dispatch a hardcoded test job from a CLI script → see SSE events `job_started`, `job_progress`, `job_completed` arrive in `curl --no-buffer`.

---

## Phase 5 — Projects + single-image UI

- [ ] `app/api/projects.py` — full CRUD; on create, mkdir `data/projects/{id}/{references,outputs,logs}`.
- [ ] `app/api/images.py` — create, list, get, delete; `POST .../dispatch` enqueues a Job.
- [ ] `services/projects/service.py` — folder mgmt, `project.json` sidecar mirror.
- [ ] `web/templates/projects_list.html`, `web/templates/project_view.html` (Gallery tab only for now).
- [ ] Image card partial (`partials/image_card.html`) — status badge, ETA, lightbox trigger.
- [ ] `components/lightbox.html` — Alpine-driven modal, keyboard nav.
- [ ] `static/js/factory.js` — SSE wiring, status badge updater, lightbox state.
- [ ] **Verify**: create project → upload 1–4 reference images → type prompt → click Generate → watch progress bar fill → image appears in gallery → click to open lightbox → download.

---

## Phase 6 — LLM prompt enhancement

- [ ] `services/llm/enhancer.py` — `PromptEnhancer.enhance()` for OpenAI / Anthropic / Gemini / Ollama (identical interface).
- [ ] `services/llm/prompts.py` — built-in Klein image system prompt + per-model override registry.
- [ ] `services/llm/ollama.py` — `POST http://localhost:11434/api/chat`.
- [ ] `app/api/llm.py` — `POST /api/llm/enhance` (single), `POST /api/llm/enhance-bulk` (image IDs list).
- [ ] UI: Enhance button on image card + on prompt editor; "Enhance all" button in Batch Preview tab.
- [ ] Post-processing: collapse to single paragraph, strip LLM prefixes.
- [ ] **Verify**: with each provider configured → enhance one prompt → check no `"Enhanced prompt:"` prefix leaks → check anti-text suffix is NOT present (it's added at workflow prep, not here).

---

## Phase 7 — Batch import (CSV / JSON)

- [ ] `services/projects/batch_import.py`:
    - [ ] CSV parser (header-driven, see BLUEPRINT §6).
    - [ ] JSON parser (`project_name`, `defaults`, `items`).
    - [ ] Validator (ref count ≤ 4, prompt non-empty, divisibility, file existence warning).
- [ ] `app/api/batch.py` — `POST /api/batch/preview` (returns parsed rows + per-row warnings), `POST /api/batch/commit?mode=new|append`.
- [ ] `web/templates/project_view.html` — add **Batch Preview** + **Results** tabs.
- [ ] Batch Preview UI: drag-drop area, editable table (HTMX inline edits), bulk actions (Enhance all, Set size, Randomize seeds), Commit button with mode toggle.
- [ ] Results tab: same card grid, filtered to current batch, overall progress bar + ETA.
- [ ] **Verify**: import a 10-row CSV with 2 refs each → preview shows all 10 with green status → "Enhance all" rewrites prompts → "Commit → new project" creates project and queues 10 jobs → Results tab shows them filling in live.

---

## Phase 8 — Downloads + zip export

- [ ] `services/downloads/zip.py` — streaming zip (no full in-memory copy).
- [ ] `GET /api/projects/{pid}/zip` — returns `application/zip` with all outputs; query param `include_prompts=true` adds a `prompts.csv`.
- [ ] `GET /api/projects/{pid}/images/{iid}/download` — single image.
- [ ] UI buttons: "Download all" on Gallery + Results tabs, "Download" on lightbox.
- [ ] **Verify**: download all from a 10-image project → unzip → 10 PNGs + prompts.csv → manifest matches.

---

## Phase 9 — Status panel + debug console

- [ ] Top-bar worker chips (HTMX poll every 5s on `/api/workers`).
- [ ] Queue depth + jobs/min counter (computed from `Job` table window).
- [ ] Debug drawer: `GET /api/logs/stream` SSE with structlog tail; client-side filter by job_id / level / text.
- [ ] Per-job log file at `data/projects/{pid}/logs/{job_id}.log` linked from card actions.
- [ ] **Verify**: kick off a batch → drawer fills with line-by-line events → filter by `job_id=…` and only those lines show → click a failed job → log file opens with full traceback.

---

## Phase 10 — Polish, tests, README

- [ ] Pytest suite green on: workflow prep, dispatcher selection, queue ordering, batch parser, zip export.
- [ ] Manual QA against a real ComfyUI server (one + multi-worker).
- [ ] `README.md` final pass: install, run, settings, batch format with examples, troubleshooting, FAQ.
- [ ] In-app "Help" page summarizing batch format with downloadable example CSV.
- [ ] Confirm `installer.bat` works on a clean Windows VM with only Python installed.
- [ ] Tag `v0.1.0` in `CHANGELOG.md`.

---

## Cross-phase recurring tasks

After every phase:

- [ ] Add an entry to `CHANGELOG.md` under `[Unreleased]`.
- [ ] Re-read `DECISIONS.md §Mistakes to avoid` before starting the next phase.
- [ ] If you hit something surprising, add it to `DECISIONS.md` (don't trust memory).
- [ ] Bump the relevant `TaskUpdate` so the user's task list is honest.
