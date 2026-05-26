# Robomuffin Image Factory

A self-hosted Python web app that drives **FLUX.2 Klein 9B** image generation on one or more remote ComfyUI servers, with batch CSV/JSON import, LLM-powered prompt enhancement, an Uploaded Assets library, a real lightbox gallery, and a queue that fans jobs across every healthy worker.

Built by **[Robomuffin Labs](https://robomuffinlabs.com)**.

> No images leave your control. Every model runs on a ComfyUI host you own; every prompt enhancement runs on an LLM provider of your choice (including local Ollama).

![status](https://img.shields.io/badge/status-active%20development-orange) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-TBD-lightgrey)

---

## Highlights

- **Image projects** that persist on disk — every reference, every output, every prompt lives in `data/projects/<id>/`.
- **Gallery + Results** with click-to-zoom lightbox, per-image Re-run / Download / Delete, **green/red ref existence badges** on every card so you can spot a missing reference before the workers do, and **status-sorted ordering** (running → done → failed → queued) so live work is always at the top.
- **Batch Render** tab: build a task list manually or import CSV/JSON; per-batch options for output format (PNG / JPG / WebP), output directory, retain-reference-filename (for replacing site images), AI-optimize-prompts, and a per-batch **Subject framing** override (inherit / on / off).
- **Batches** tab: every batch run grouped with live progress bars; Cancel queued / Retry failed / Retry all / Clear failed / Delete-batch per row, plus a project-wide "Clear all failed images" sweep.
- **Uploaded Assets** tab: every reference image ever uploaded to a project, with re-use, view, and delete (blocked if any image row still references the file).
- **Add reference picker** — modal with both "Upload from computer" and "Choose existing asset" tabs.
- **Multi-server parallel queue** — runner spawns one task per available worker slot and re-dispatches the moment any worker frees up. Configure as many ComfyUI URLs as you want; load is auto-balanced by least-in-flight selection.
- **Pause / Stop / Start queue controls** at the top of every project view. Stop is a hard kill: drains pending, marks running CANCELLED, and POSTs `/interrupt` to every ComfyUI worker so in-step generations abort instead of burning compute.
- **Worker health strip** with a green/red dot per server, in-flight counts, last-error tooltip, and a manual **Re-probe** button. Dispatcher auto-probes every 30s so an offline server self-heals once it's reachable.
- **Subject framing toggle** — *"Make sure main subject of image is largely in frame"* in Settings, with Subtle / Moderate / Strong presets plus advanced scaffold overrides. Applies composition language to the prompt and an anti-zoom-out negative; per-batch override on Batch Render.
- **GPT Image 2 alternative engine** — toggle in Settings swaps the entire pipeline from ComfyUI/Klein to OpenAI's `gpt-image-2` / `gpt-image-1` API (with a big cost warning), respecting all the same output controls.
- **Live progress** via SSE with **auto-reconnect, polling fallback, and Page-Visibility refresh** so a backgrounded tab catches up the moment you click it.
- **Prompt enhancement** through OpenAI, Anthropic, Google Gemini, or local **Ollama**.
- **Forensic logging**: every HTTP request, every LLM call (full prompt + response), every ComfyUI WebSocket frame is captured. Per-job log file at `data/projects/<pid>/logs/<job_id>.log`. `GET /api/debug/jobs/<job_id>` returns a single-URL dump.
- **Installer + run scripts** for Windows (`installer.bat` / `run.bat`) and macOS / Linux (`install.sh` / `run.sh`). No Docker required.

---

## What you need before running

You'll need three things on the machine that runs the app, plus a ComfyUI server somewhere on your network (it can be the same machine).

### 1. Python 3.11 or newer (on the machine running the app)

Windows: <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** during install.
macOS: `brew install python@3.12` or use the official installer.
Linux: your distro's package manager (`apt install python3.12 python3.12-venv` on Debian/Ubuntu).

### 2. A ComfyUI server running FLUX.2 Klein 9B

The app does NOT bundle ComfyUI. You point it at any reachable ComfyUI instance.

- **ComfyUI**: <https://github.com/comfyanonymous/ComfyUI> — installation guide on that page. The official portable Windows build is the easiest path.
- **ComfyUI-GGUF** custom node (only required if you use the GGUF variant; the official Klein workflow uses the `.safetensors` build, no custom node needed): <https://github.com/city96/ComfyUI-GGUF>

### 3. Klein 9B model files (drop these into ComfyUI's folders)

The bundled Klein workflow uses the official ComfyUI Flux.2 Klein 9B (base) stack. Download these and place them where ComfyUI expects them:

| File | Folder under your ComfyUI install | Where to get it |
|---|---|---|
| `flux-2-klein-base-9b-fp8.safetensors` | `models/diffusion_models/` | <https://huggingface.co/black-forest-labs/FLUX.2-klein> *(or use the GGUF variant if you have less VRAM — adjust the workflow JSON to match)* |
| `qwen_3_8b_fp8mixed.safetensors` | `models/text_encoders/` | <https://huggingface.co/Comfy-Org/Qwen3-8B-Text-Encoder> |
| `full_encoder_small_decoder.safetensors` | `models/vae/` | <https://huggingface.co/black-forest-labs/FLUX.2-klein> |

If your filenames differ, just edit the relevant `app/services/comfyui/workflows/klein_*ref.json` — every node uses standardized `_meta.title` fields, so as long as the titles match the runtime patches still work.

> **About model licenses.** Read the FLUX.2 Klein license on the Black Forest Labs page before commercial use.

### 4. (Optional) An LLM provider for prompt enhancement

Any one of:
- **OpenAI** API key: <https://platform.openai.com/api-keys>
- **Anthropic (Claude)** API key: <https://console.anthropic.com/>
- **Google Gemini** API key: <https://ai.google.dev/>
- **Ollama** (local, free): <https://ollama.com/download> — install, run, pull a model: `ollama pull llama3.1:8b`

You can also skip this entirely and write your own prompts.

---

## Install

### Windows

```cmd
git clone https://github.com/robomuffinlabs/RBMN_Image_factory.git
cd RBMN_Image_factory
installer.bat
```

`installer.bat` does six things, prompts before each:

1. Locates Python (`py -3.11` → `py -3` → `python`)
2. Creates `.venv/`
3. Upgrades pip
4. Installs `requirements.txt`
5. Copies `.env.example` → `.env` (only if `.env` doesn't already exist)
6. Initializes the SQLite database and seeds default settings

### macOS / Linux

```bash
git clone https://github.com/robomuffinlabs/RBMN_Image_factory.git
cd RBMN_Image_factory
./install.sh
```

---

## Run

```cmd
:: Windows
run.bat
```

```bash
# macOS / Linux
./run.sh
```

Then open <http://127.0.0.1:8765/>.

To use a different port: `run.bat 8766` (Windows) or set `FACTORY_PORT=8766` in `.env`.

---

## First-time setup walkthrough

1. Open <http://127.0.0.1:8765/>, click **Settings** in the header.
2. **ComfyUI servers**: paste your ComfyUI URL (e.g. `http://192.168.1.50:8188` or `http://localhost:8188`). Click **Test** — green ✓ means the URL responded. Add more URLs for additional GPUs; jobs will fan out across all healthy servers.
3. **LLM providers**: paste an API key for at least one provider (or fill in your Ollama base URL). Click **Test**. Pick a **default**.
4. **Image defaults**: width/height, seed mode (random or fixed), max parallel jobs per worker (1 is safe).
5. (Optional) **System prompt override** and **Prompt guidance** — leave the defaults unless you know what you're tweaking.
6. Click **Save** (sticky top-right) — green toast confirms.
7. Back to **Projects**, click **+ New project**.
8. On the project page: type a prompt, click **+ Add reference image**, pick an image (Klein is an edit model — at least one reference is required), click **Generate**.

---

## The five tabs on a project

| Tab | What it does |
|---|---|
| **Gallery** | Single-image generation form + grid of generated images. Click any thumbnail to open the lightbox. Each card has Re-run / Download / Delete. |
| **Batch Preview** | Quick-import a CSV/JSON, preview parsed rows, commit to a new project or append to this one. |
| **Batch Render** | Build a list of tasks manually (+ Add row) or import. Set output format (PNG/JPG/WebP), output directory, retain-original-filename, and AI-optimize-prompts. **Reference base directory** lets you use bare filenames in your CSV. |
| **Results** | Filtered grid for the current batch with overall progress bar + ETA. |
| **Uploaded Assets** | Every reference image uploaded to this project. View, re-use (via the picker modal on the Gallery), or delete (blocked if any image is using it). |

---

## Batch CSV/JSON format

Samples live in `Import_samples/`:

- `Import_samples/batch_render.csv` — recognized columns: `prompt, ref1..ref4, width, height, seed, negative_prompt, name`
- `Import_samples/batch_render.json` — same data plus top-level options (`output_format`, `retain_ref1_name`, `custom_output_dir`, `reference_base_dir`, `enhance_prompts`, `defaults`)

Reference paths can be:

- **Absolute** (`C:\Users\hexum\refs\hero.png` or `/home/me/refs/hero.png`) — used as-is.
- **Bare filename** (`hero.png`) — resolved against the Batch Render tab's **Reference base directory** field at commit time.
- **Path with a separator** (`./local-refs/hero.png`, `refs\hero.png`) — treated as already-qualified.

The runner reads each file from your local filesystem and uploads it to your ComfyUI server's `input/` folder automatically — your ComfyUI host does not need access to your local folders.

---

## Configuration (`.env`)

`.env.example` documents every variable. Quick reference:

```env
FACTORY_HOST=127.0.0.1
FACTORY_PORT=8765
FACTORY_RELOAD=false

# Logging
FACTORY_LOG_LEVEL=INFO          # DEBUG | INFO | WARNING | ERROR
FACTORY_LOG_JSON=false          # true = machine-readable console
FACTORY_LOG_RICH=true           # pretty console via Rich
FACTORY_LOG_RETAIN_DAYS=14

# Optional: store project data outside the repo
# FACTORY_DATA_DIR=C:\Users\hexum\Documents\RobomuffinFactory

# Seed values (used only on first DB creation; the Settings UI is authoritative afterward)
COMFYUI_URLS=http://localhost:8188
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434
DEFAULT_LLM_PROVIDER=
```

After the first run, change values in the **Settings** page (UI) — they live in the SQLite DB, not the `.env` file.

---

## Directory layout

```
RBMN_Image_factory/
├── installer.bat        # one-time setup (Windows)
├── run.bat              # start server (Windows)
├── install.sh / run.sh  # macOS / Linux equivalents
├── .env.example         # config template; copied to .env by the installer
├── requirements.txt
├── pyproject.toml
│
├── BLUEPRINT.md         # architecture overview (developer-facing)
├── CHECKLIST.md         # phased build plan (mostly complete)
├── CHANGELOG.md         # release history
├── DECISIONS.md         # locked decisions + "mistakes to avoid"
│
├── Import_samples/      # sample CSV + JSON for Batch Render
├── Klein-Workflow-Examples/  # reference workflows from the official ComfyUI Klein release
├── devnotes/            # design notes from the predecessor app (reference only)
│
├── app/                 # the Python package
│   ├── main.py
│   ├── config.py
│   ├── logging_setup.py
│   ├── db/              # SQLModel models + migrations
│   ├── api/             # FastAPI routers
│   ├── services/        # comfyui client, dispatcher, workflow prep, llm, jobs, debug helpers
│   └── web/             # Jinja templates + static
│
└── data/                # runtime; gitignored. Created on first run.
    ├── factory.db
    ├── logs/
    │   ├── factory.log  # rolling app log (14-day rotation)
    │   └── llm/<date>/  # per-LLM-call JSON: prompt + response + tokens + latency
    └── projects/<id>/
        ├── references/  # uploaded refs
        ├── outputs/     # generated images
        └── logs/<job_id>.log  # per-job forensic log
```

---

## Debug + observability

Operator-facing endpoints, all under `/api/debug/`:

| URL | What it returns |
|---|---|
| `GET /api/debug/status` | Process info, uptime, ring-buffer sizes |
| `GET /api/debug/logs/recent?limit=200&level=error` | In-memory tail of the last N structured log lines |
| `GET /api/debug/errors/recent` | Last N errors with full tracebacks |
| `GET /api/debug/requests/recent` | Last N HTTP requests with timing |
| `GET /api/debug/jobs/<job_id>` | One job's full diagnostic dump: DB row + image row + per-job log tail |
| `GET /api/debug/llm/calls` | Recent LLM calls (full prompt + response) |
| `POST /api/debug/test-log` | Emit one log line at each level (smoke test) |
| `POST /api/debug/raise` | Force a 500 to verify the error ring buffer |

The page itself logs every fetch in DevTools console as `[robomuffin] fetch → ...` for live tracing. Uncaught JS errors POST to `/api/debug/client-log`, so they end up in `data/logs/factory.log` alongside server-side tracebacks.

---

## Architecture

For the full breakdown see [BLUEPRINT.md](BLUEPRINT.md). One-paragraph version:

A single FastAPI process serves both the HTML UI (Jinja + Tailwind via CDN + plain JS) and a JSON API. SQLite (WAL) stores projects / images / jobs / settings. An in-process `asyncio.PriorityQueue` runs a dispatcher loop that selects healthy ComfyUI workers (by capability + least-loaded), uploads reference images, builds a workflow JSON (with title-based node lookup so node IDs can change between exports), submits over HTTP, streams progress over WebSocket, and downloads outputs to disk. Job lifecycle events are broadcast via SSE so the UI updates without polling. LLM enhancement runs through one of four providers (OpenAI / Anthropic / Gemini / Ollama) behind a single interface; every call is logged to a per-call JSON file under `data/logs/llm/`.

---

## Updating after a `git pull`

```bat
:: Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app.db.migrations
run.bat
```

```bash
# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app.db.migrations
./run.sh
```

The migrations are additive and idempotent — they only add new columns, never delete data.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `installer.bat` can't find Python | Install Python 3.11+ and tick "Add to PATH". |
| `run.bat` exits immediately | `.venv` is missing — run `installer.bat` first. |
| `Errno 10048 / port in use` | A previous `run.bat` is still alive. `netstat -ano \| findstr :8765`, then `taskkill /PID <pid> /F`. Or `run.bat 8766` to use a different port. |
| Setup banner: "0 healthy workers" even after saving the URL | Click **Probe all workers** on the Settings page. The dispatcher reconfigures on save, but the banner is cached until the next page reload. F5 the project view. |
| Workers show as unhealthy on the strip | Click **Re-probe** next to the workers strip, or wait 30s for the auto-probe. Check `data/logs/factory.log` for `dispatcher.startup_probed` and the per-worker `last_error`. |
| Output size doesn't match the configured width/height | Check `data/logs/factory.log` for `comfy.workflow.size_patched` — that confirms the patch landed. If it logs `comfy.workflow.size_not_patched`, the workflow JSON is missing an `EmptyFlux2LatentImage` node. |
| ComfyUI rejects WebP / GIF / BMP refs | The runner already normalizes every reference to PNG via Pillow before uploading. If you still see this, restart `run.bat` to pick up the latest code. |
| Image card shows red ✗ on a reference | The server can't find that file locally. Either fix the path in the row, set a correct **Reference base directory** on the Batch Render tab, or upload the file via the Uploaded Assets tab. |

---

## Contributing

Bug reports, feature ideas, and PRs are welcome — open an issue first for any non-trivial change so we can align on direction. Style: keep one bot/feature/setting per PR; include the relevant log line(s) you used to verify; cross-link the CHANGELOG entry.

## License

Code in this repository is © Robomuffin Labs. A formal license is pending; see `LICENSE` for the current placeholder.

The upstream models retain their own licenses:
- **FLUX.2 Klein 9B** — Black Forest Labs, see the HuggingFace model card for terms.
- **ComfyUI** — GPL-3.0, see the ComfyUI repository.
