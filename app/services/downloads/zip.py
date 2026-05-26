"""Streaming zip generator for project downloads.

Uses ``zipfile.ZipFile`` over an in-memory buffer that's flushed in chunks
so we don't have to hold the whole archive in RAM.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Iterable

from app.logging_setup import get_logger

_log = get_logger("factory.downloads")


def stream_project_zip(
    project_name: str,
    image_records: Iterable[dict[str, str]],
    include_prompts_csv: bool = True,
    chunk_size: int = 64 * 1024,
) -> Iterator[bytes]:
    """Yield bytes of a streaming zip of all output images + optional prompts.csv.

    ``image_records`` is an iterable of dicts: ``{output_path, prompt, enhanced_prompt, name, width, height, seed}``.
    """
    buf = io.BytesIO()
    written_pos = 0

    def _drain() -> bytes:
        nonlocal written_pos
        buf.seek(written_pos)
        data = buf.read()
        written_pos = buf.tell()
        return data

    zf = zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
    try:
        prompts_rows: list[dict[str, str]] = []
        for rec in image_records:
            path = rec.get("output_path")
            if not path:
                continue
            p = Path(path)
            if not p.exists():
                continue
            arc = f"{project_name}/{rec.get('name') or p.name}"
            if not arc.endswith(p.suffix):
                arc += p.suffix
            zf.write(p, arcname=arc)
            prompts_rows.append({
                "filename": arc,
                "prompt": rec.get("prompt", ""),
                "enhanced_prompt": rec.get("enhanced_prompt") or "",
                "width": str(rec.get("width", "")),
                "height": str(rec.get("height", "")),
                "seed": str(rec.get("seed", "")),
            })
            chunk = _drain()
            while chunk:
                yield chunk[:chunk_size]
                chunk = chunk[chunk_size:]
        if include_prompts_csv and prompts_rows:
            sb = io.StringIO()
            writer = csv.DictWriter(sb, fieldnames=list(prompts_rows[0].keys()))
            writer.writeheader()
            writer.writerows(prompts_rows)
            zf.writestr(f"{project_name}/prompts.csv", sb.getvalue())
    finally:
        zf.close()
    final = _drain()
    if final:
        yield final
