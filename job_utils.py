"""
Shared utilities for the daily job application automation.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def load_json_list(path: Path) -> list[dict[str, Any]]:
    """Return a JSON list from path, or an empty list if it is missing/bad."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_json_atomic(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write JSON via temp file + replace so interrupted runs do not corrupt logs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(entries, tmp, indent=2, ensure_ascii=False)
            tmp.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def job_identity(title: str = "", company: str = "", url: str = "", job_id: str = "") -> str:
    """Build a stable best-effort identity for dedupe across sources."""
    if job_id:
        return f"id:{job_id.strip().lower()}"
    if url:
        clean_url = url.split("?")[0].rstrip("/").lower()
        return f"url:{clean_url}"
    return f"title-company:{title.strip().lower()}::{company.strip().lower()}"


def already_seen(entries: list[dict[str, Any]], job: dict[str, Any]) -> bool:
    """Return True when job appears in existing log entries."""
    current = job_identity(
        title=job.get("title", ""),
        company=job.get("company", ""),
        url=job.get("url", ""),
        job_id=job.get("job_id", ""),
    )
    for entry in entries:
        existing = job_identity(
            title=entry.get("title", ""),
            company=entry.get("company", ""),
            url=entry.get("url", ""),
            job_id=entry.get("job_id", ""),
        )
        if existing == current:
            return True
    return False
