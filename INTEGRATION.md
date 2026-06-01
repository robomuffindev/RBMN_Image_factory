# Embedding Robomuffin Image Factory into another local app

This guide is for developers building a separate local application (for
example, a self-hosted WordPress/WooCommerce admin tool) who want to use
Robomuffin Image Factory as their AI-image-generation backend. The
embedding app runs on the same machine (or another machine on the same
trusted network), drives Robomuffin through its HTTP API, downloads the
rendered images, and pushes them into its own asset pipeline.

> **Threat model.** Robomuffin is designed for self-hosted use on a trusted
> network (default bind `127.0.0.1`). Don't expose it directly to the public
> internet — there is no per-user authentication, only an optional shared
> API key. Treat it like a database: protect it at the network layer.

---

## Architecture at a glance

```
┌──────────────────────┐           HTTP                ┌────────────────────────────┐
│ Your local admin app │ ◀────────── ─────────────▶   │ Robomuffin Image Factory   │
│ (e.g. WP/WooCommerce │  GET /api/projects            │ http://127.0.0.1:8765      │
│  Python admin tool)  │  POST /api/projects/{id}/...  │                            │
│                      │  GET /api/jobs/stream (SSE)   │   ┌──────────────────┐     │
│ uses robomuffin_     │                               │   │ FastAPI + SQLite │     │
│ client.py (SDK)      │                               │   └────────┬─────────┘     │
└──────────┬───────────┘                               │            │               │
           │ reads/writes                              │   ┌────────▼─────────┐     │
           ▼                                           │   │ runner / queue   │     │
  shared filesystem ◀────────────────────────────────▶ │   └────────┬─────────┘     │
  C:\share\wp-uploads\                                 │            │               │
  (where reference images and final renders live)      └────────────┼───────────────┘
                                                                    │
                                                                    ▼
                                              one or more ComfyUI servers on the LAN
```

The embedding app and Robomuffin share a folder on disk for references
(input) and rendered outputs (output). Robomuffin's `custom_output_dir`
field lets you point each batch's outputs straight into the embedding
app's media folder.

---

## Setup checklist

### 1. Install + run Robomuffin once

```cmd
git clone https://github.com/robomuffinlabs/RBMN_Image_factory.git
cd RBMN_Image_factory
installer.bat
```

Edit `.env` to set the values your embedding app needs:

```env
# Bind to 127.0.0.1 (default). Change to 0.0.0.0 only if the embedding
# app runs on a different machine on the LAN.
FACTORY_HOST=127.0.0.1
FACTORY_PORT=8765

# Required when the embedding app calls /api/* — leave empty during
# initial local dev, then set a strong random value before you ship.
FACTORY_API_KEY=

# Origins the embedding app calls from. Add whatever ports your admin
# app uses (the defaults cover localhost:8080 and Vite's :5173).
FACTORY_CORS_ORIGINS=http://localhost:8080,http://127.0.0.1:8080

# Pick a shared output folder that BOTH apps can read/write.
# Robomuffin writes here, your admin app reads from here.
# (You can also set this per-batch via custom_output_dir.)
# FACTORY_DATA_DIR=C:\share\rbmn-data
```

Start the server with `run.bat`. The admin app then talks to
`http://127.0.0.1:8765`.

### 2. Vendor the Python SDK into your embedding app

The SDK lives in `sdk/robomuffin_client.py` (single file, depends only on
`requests`). Two ways to use it:

**Option A — copy the file** (zero-config):

```cmd
mkdir my_admin_app\vendor
copy RBMN_Image_factory\sdk\robomuffin_client.py my_admin_app\vendor\
```

Then in your admin app:
```python
from vendor.robomuffin_client import RobomuffinClient
```

**Option B — pip install -e** (lets `git pull` updates flow through):

```cmd
pip install -e ./RBMN_Image_factory/sdk
```

Then:
```python
from robomuffin_client import RobomuffinClient
```

### 3. Wire up the `updater.bat` workflow

Robomuffin ships with `updater.bat` in the repo root. The script is safe
to call from your admin app's "update Robomuffin" button — it refuses to
update while the server is still listening on its port, then does
`git pull --ff-only`, refreshes pip packages, and runs the additive DB
migration. From your admin app:

```python
# wp_admin/services/robomuffin_updater.py
import subprocess, pathlib
RB_DIR = pathlib.Path(r"C:\Users\me\Cowork\RBMN_Image_factory")

def update_robomuffin():
    """Returns (exit_code, combined_stdout_stderr). Caller is responsible
    for stopping run.bat first (the updater detects + refuses if it's running)."""
    proc = subprocess.run(
        [str(RB_DIR / "updater.bat")],
        cwd=str(RB_DIR),
        capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr
```

Exit codes from `updater.bat`:
- `0` — updated cleanly
- `1` — generic failure (venv missing, git failed, requirements failed)
- `2` — Robomuffin is still running on the configured port; the admin app
  should kill `run.bat`, retry the updater, and restart afterwards

---

## API reference (minimum useful surface)

Every endpoint accepts `Content-Type: application/json` and (when
`FACTORY_API_KEY` is set) the header `X-Robomuffin-Key: <value>`. The
full Swagger UI is served at `/api/docs` when the server is running.

### Projects

| Method  | Path                              | Notes                                      |
|---------|-----------------------------------|--------------------------------------------|
| `GET`   | `/api/projects`                   | List all projects                          |
| `GET`   | `/api/projects/{id}`              | One project + image counts                 |
| `POST`  | `/api/projects`                   | Body: `{name, description?}`               |
| `DELETE`| `/api/projects/{id}`              | Wipes the project folder too               |
| `GET`   | `/api/projects/{id}/images`       | List images (edit candidates filtered out) |

### Images / single-render

| Method  | Path                              | Notes                                       |
|---------|-----------------------------------|---------------------------------------------|
| `POST`  | `/api/projects/{pid}/images`      | Create + queue one image. Body: see SDK     |
| `GET`   | `/api/images/{id}`                | Status + metadata for one image             |
| `GET`   | `/api/images/{id}/file`           | Stream the rendered file                    |
| `POST`  | `/api/images/{id}/rerun`          | Re-queue with edited prompt/refs/seed       |
| `POST`  | `/api/images/{id}/edits/generate` | Spawn N edit candidates                     |
| `POST`  | `/api/images/{id}/edits/{cid}/promote` | Replace original with candidate        |
| `DELETE`| `/api/images/{id}`                | Delete row + on-disk file                   |

### Batch render (preferred for >5 images)

| Method  | Path                              | Notes                                       |
|---------|-----------------------------------|---------------------------------------------|
| `POST`  | `/api/batch/render/commit?project_id=…` | Submit rows; auto-pauses dispatcher.  |
| `GET`   | `/api/batch/runs?project_id=…`    | Status of every batch run                   |
| `POST`  | `/api/batch/runs/{id}/cancel-queued` | Stop PENDING items in this batch         |
| `POST`  | `/api/batch/runs/{id}/retry-failed`  | Re-dispatch FAILED items                 |
| `DELETE`| `/api/batch/runs/{id}`            | Wipe batch + on-disk outputs                |

### Queue control

| Method  | Path                              | Notes                                       |
|---------|-----------------------------------|---------------------------------------------|
| `GET`   | `/api/jobs/queue/status`          | `{paused, pending_count}`                   |
| `POST`  | `/api/jobs/queue/start`           | Release PENDING jobs + unpause dispatcher   |
| `POST`  | `/api/jobs/queue/pause`           | Stop auto-dispatching new commits           |
| `POST`  | `/api/jobs/queue/stop`            | Hard kill — cancels everything + interrupts |

### Live progress

`GET /api/jobs/stream` is a Server-Sent Events stream. Each event is a
JSON payload with at minimum `{job_id, image_id, status}`. Event names:
`job_started`, `job_progress`, `job_completed`, `job_failed`,
`job_retrying`, `job_cancelled`.

```python
import sseclient   # pip install sseclient-py
import requests

resp = requests.get("http://127.0.0.1:8765/api/jobs/stream",
                    headers={"X-Robomuffin-Key": "changeme"}, stream=True)
for evt in sseclient.SSEClient(resp).events():
    data = json.loads(evt.data)
    print(evt.event, data)
```

---

## End-to-end example: batch render from an admin app

```python
from robomuffin_client import RobomuffinClient
rb = RobomuffinClient("http://127.0.0.1:8765", api_key="changeme")

# Create a project per content campaign so cleanup is easy.
proj = rb.create_project(name="Spring 2026 product refresh")

# Build rows from your DB. Each row gets its own reference image
# (already on disk in a folder Robomuffin can read).
rows = [
    {
        "name": p["slug"],
        "prompt": f"hero shot of {p['title']} on a marble counter, soft window light",
        "reference_paths": [p["hero_image_path"]],
        "width": 1024, "height": 1024, "seed": "random",
        # Scale hints — anchor the model to the product's real-world size.
        "size":          p.get("size_text"),         # "9 inches tall"
        "dimensions":    p.get("dim_text"),          # "9x3x3 inches"
        "relative_size": p.get("comparison_text"),   # "a tall water bottle"
    }
    for p in admin_db.fetch_products_due_for_refresh()
]

result = rb.commit_batch_render(
    project_id=proj["id"], rows=rows,
    output_format="webp", retain_ref1_name=True,
    custom_output_dir=r"C:\share\wp-uploads\rbmn-out",
)
print(f"queued {result['queued']} items, batch_run_id={result['batch_run_id']}")
print(f"refs found: {result['refs_resolved']}, missing: {result['refs_missing']}")

# Auto-paused — release when ready (or let the user click Start Queue in the UI).
rb.release_queue()

# Block until the batch is done; for a long batch, poll the run instead.
stats = rb.wait_for_batch(result["batch_run_id"], project_id=proj["id"], timeout=3600)
print(f"done {stats['counts']['done']} / failed {stats['counts']['failed']}")

# Pull the final files into the admin app's media folder.
for img in rb.list_images(proj["id"]):
    if img["status"] == "done":
        out = pathlib.Path(r"C:\share\wp-uploads\final") / (img["name"] + ".webp")
        rb.download_image(img["id"], out)
        admin_db.attach_image_to_product(img["name"], out)
```

---

## Reference image strategy

Robomuffin uploads each reference image to the chosen ComfyUI host at
job time. Your admin app has three options for how to give Robomuffin the
reference paths:

**1. Absolute paths to a shared folder (easiest).** Set
`reference_paths=["C:\\share\\wp-uploads\\hero.png"]` per row. Robomuffin
reads the file from disk and uploads it. Works for any folder Robomuffin
can read.

**2. Bare filenames + `reference_base_dir`.** Pass
`reference_base_dir="C:\\share\\wp-uploads"` once at the batch level, then
each row's `reference_paths=["hero.png"]`. Robomuffin resolves bare names
against the base dir at commit time, falling back through a ladder
including `<base>/uploads/` and the project's own uploads folder if the
file isn't at the first candidate.

**3. Upload-first.** `POST /api/projects/{id}/upload-reference` with a
multipart form. Robomuffin saves it to the project's uploads folder and
the returned path becomes the `reference_paths` entry. Use this if your
admin app generates the reference on the fly and doesn't want to leave
files in a shared folder.

The preflight diagnostic in the commit response tells you exactly which
references resolved + which didn't — handy for surfacing a "fix these
N missing refs" warning in your admin UI before the queue starts.

---

## Auth model

| Mode                     | When to use                                                                                |
|--------------------------|--------------------------------------------------------------------------------------------|
| **No auth** (default)    | Initial development on `127.0.0.1` only. Don't ship like this.                              |
| **Shared API key**       | Production self-hosted. Set `FACTORY_API_KEY` in `.env`, distribute to embedders.           |
| **Reverse proxy auth**   | If you need per-user auth, put nginx/Caddy in front with basic-auth + IP allowlist.         |

When `FACTORY_API_KEY` is set, every `/api/*` request requires
`X-Robomuffin-Key: <value>` (or `?api_key=<value>` query for browser cases).
The middleware ignores OPTIONS preflights so CORS works without leaking.
Failed auth returns HTTP 401 with a JSON detail.

The UI pages themselves (`/`, `/projects/{id}`, `/settings`, `/tools`)
remain accessible without the key so an iframe-embedded view still works.

---

## CORS

`FACTORY_CORS_ORIGINS` is a comma-separated list of origins allowed by the
CORS middleware. Defaults cover localhost dev ports. Add your production
admin app's origin to this list. The middleware allows credentials and
all standard methods/headers — there's no further configuration needed.

If the embedding app fetches Robomuffin assets directly (e.g. via an
`<img>` tag pointing at `/api/images/{id}/file`), make sure to include the
API key as a query param: `/api/images/{id}/file?api_key=…` — browsers
won't send custom headers on plain image requests.

---

## Health monitoring

The embedding app should poll `GET /api/debug/status` (or `GET /api/health`)
to know when Robomuffin is up. The SDK exposes this as `rb.health()`.
`GET /api/workers` returns the per-server health + in-flight counts —
surface this in your admin UI so users can see whether their ComfyUI
fleet is healthy before queuing a 200-image batch.

---

## When to bypass the SDK

The SDK covers the common operations. For anything more exotic
(uploading reference files, hitting `/api/batch/preview` to validate a
CSV before committing, etc.) just use `rb.session.post(url, ...)`
directly — the session is preconfigured with the API key header and
sensible timeouts.

```python
# Example: validate a CSV before committing
with open("rows.csv", "rb") as fh:
    r = rb.session.post(
        f"{rb.base_url}/api/batch/preview",
        files={"file": ("rows.csv", fh, "text/csv")},
    )
print(r.json())  # parsed rows + warnings
```
