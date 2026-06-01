"""Tools API — utilities that operate outside the queue + projects pipeline.

Current tools:
  - Compare Folders: scan two local directories, pair image files by basename
    (case-insensitive), expose the matched pairs via /api/tools/compare/scan,
    and serve individual files via /api/tools/compare/file.

Security note: the file-serving endpoint reads from arbitrary user-typed
paths on the server's filesystem. The app is designed for self-hosted use on
127.0.0.1 (FACTORY_HOST default) and we restrict served files to common image
extensions, but operators exposing the app to a network should be aware that
giving someone access to this endpoint is roughly equivalent to giving them
filesystem read access for image files. Don't expose this app to untrusted
networks without an auth layer in front.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.logging_setup import get_logger

router = APIRouter(prefix="/api/tools", tags=["tools"])
_log = get_logger("factory.api.tools")

# Image extensions we'll honor when scanning + serving. Lowercase compared.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif"}


class CompareRequest(BaseModel):
    folder_a: str
    folder_b: str


def _scan_image_index(folder: str) -> tuple[Path, dict[str, str]]:
    """Walk ``folder`` non-recursively and build a map of lowercase-basename
    → on-disk filename for every image file. Returns (resolved_path, index).
    Raises HTTPException(400) if the folder isn't a real directory.
    """
    p = Path(folder).expanduser()
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"not a folder: {folder!r}")
    idx: dict[str, str] = {}
    try:
        for entry in os.listdir(p):
            ext = os.path.splitext(entry)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            full = p / entry
            if not full.is_file():
                continue
            idx[entry.lower()] = entry
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to list {folder!r}: {exc!s}") from exc
    return p, idx


@router.post("/compare/scan")
async def compare_scan(req: CompareRequest) -> dict[str, Any]:
    """Pair up image files that exist in BOTH folders.

    Matching is case-insensitive on the FULL basename (including extension).
    "Photo.PNG" in folder A matches "photo.png" in folder B.
    """
    a_path, a_idx = _scan_image_index(req.folder_a)
    b_path, b_idx = _scan_image_index(req.folder_b)

    common_keys = sorted(set(a_idx.keys()) & set(b_idx.keys()))
    pairs: list[dict[str, str]] = []
    for k in common_keys:
        # Use the original-cased filename from each folder for display +
        # serving. Two filenames that differ only by case still match.
        a_name = a_idx[k]
        b_name = b_idx[k]
        pairs.append({
            "name": a_name,  # display name from folder A
            "a_url": f"/api/tools/compare/file?folder={quote(str(a_path))}&name={quote(a_name)}",
            "b_url": f"/api/tools/compare/file?folder={quote(str(b_path))}&name={quote(b_name)}",
        })

    out = {
        "folder_a": str(a_path),
        "folder_b": str(b_path),
        "pairs": pairs,
        "matched": len(pairs),
        "total_a": len(a_idx),
        "total_b": len(b_idx),
        "unmatched_a": len(a_idx) - len(common_keys),
        "unmatched_b": len(b_idx) - len(common_keys),
    }
    _log.info("tools.compare.scan",
              folder_a=str(a_path), folder_b=str(b_path),
              total_a=out["total_a"], total_b=out["total_b"],
              matched=out["matched"])
    return out


@router.get("/compare/file")
async def compare_file(
    folder: str = Query(..., description="absolute folder path the user scanned"),
    name: str = Query(..., description="image filename within that folder"),
) -> FileResponse:
    """Serve a single image from a previously-scanned folder.

    Defensive validation:
      - basename must have no path separators (no traversal)
      - resolved path must stay within the requested folder
      - extension must be on the image allowlist
    """
    # Reject any name with a path separator — the client should ONLY send a
    # basename, never a relative path. This blocks "../../etc/hosts" style.
    if any(sep in name for sep in ("/", "\\", "\x00")):
        _log.warning("tools.compare.file.bad_name", name=name)
        raise HTTPException(status_code=400, detail="invalid filename")
    ext = os.path.splitext(name)[1].lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"extension not allowed: {ext}")
    base = Path(folder).expanduser().resolve()
    full = (base / name).resolve()
    # Containment check — full must be inside base.
    try:
        full.relative_to(base)
    except ValueError:
        _log.warning("tools.compare.file.outside_base", base=str(base), full=str(full))
        raise HTTPException(status_code=400, detail="path escapes folder")
    if not full.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {full}")
    media_type, _ = mimetypes.guess_type(str(full))
    if not media_type:
        media_type = {".webp": "image/webp", ".avif": "image/avif"}.get(ext, "application/octet-stream")
    return FileResponse(full, media_type=media_type)
