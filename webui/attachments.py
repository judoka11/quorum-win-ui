"""Files attached to a Quorum Web conversation.

Uploads are saved under <workdir>/attachments/<batch>/, inside the folder the participants run in,
so agents that can read files (claude, codex) can open them. Text and PDF contents are also put
into the prompt, so every participant sees them; images go to codex as --image inputs (see
agent_cli.RUN_IMAGES) and claude opens them with its Read tool.
"""

from __future__ import annotations

import base64
import io
import re
import shutil
import uuid
from pathlib import Path

MAX_FILES_PER_MESSAGE = 10
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_INLINE_PER_FILE = 100_000  # characters of a text/PDF file put into the prompt
MAX_INLINE_TOTAL = 300_000
MAX_PDF_PAGES = 300
IMAGE_TYPES = {
    b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg", b"GIF87a": ".gif", b"GIF89a": ".gif",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
EXTRACTED_SUFFIX = ".extracted.txt"


def _safe_name(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name.strip() or "file"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:120]


def _kind(name: str, data: bytes) -> str | None:
    ext = Path(name).suffix.lower()
    if data.startswith(b"%PDF") or ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTS or any(data.startswith(sig) for sig in IMAGE_TYPES) or data[8:12] == b"WEBP":
        return "image"
    if b"\x00" not in data[:8192]:
        try:
            data.decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            try:
                data.decode("cp1250")  # Polish Windows text files
                return "text"
            except UnicodeDecodeError:
                return None
    return None


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[PDF text extraction is not installed (pypdf); open the file to read it.]"
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [p.extract_text() or "" for p in reader.pages[:MAX_PDF_PAGES]]
    except Exception as e:  # damaged or encrypted PDFs
        return f"[Could not extract text from this PDF ({e.__class__.__name__}); open the file to read it.]"
    text = "\n\n".join(f"--- page {i} ---\n{t.strip()}" for i, t in enumerate(pages, 1) if t.strip())
    if len(reader.pages) > MAX_PDF_PAGES:
        text += f"\n\n[Only the first {MAX_PDF_PAGES} of {len(reader.pages)} pages were extracted.]"
    return text or "[No text could be extracted; it may be a scanned PDF. Open the file to look at it.]"


def save(workdir: Path, files: list[dict]) -> list[dict]:
    """Decode and save uploads ({name, data: base64}); returns attachment records.

    Raises ValueError with a message for the user on a bad upload (nothing is kept then).
    """
    if not files:
        return []
    if len(files) > MAX_FILES_PER_MESSAGE:
        raise ValueError(f"Attach at most {MAX_FILES_PER_MESSAGE} files per message.")
    batch = uuid.uuid4().hex[:12]
    folder = workdir / "attachments" / batch
    records, used = [], set()
    try:
        for f in files:
            name = _safe_name(f.get("name", "file"))
            try:
                data = base64.b64decode(f.get("data") or "", validate=True)
            except ValueError:
                raise ValueError(f"{name}: the upload was damaged; try attaching it again.") from None
            if len(data) > MAX_FILE_BYTES:
                raise ValueError(f"{name} is larger than {MAX_FILE_BYTES // 1024 // 1024} MB.")
            kind = _kind(name, data)
            if kind is None:
                raise ValueError(f"{name}: this file type can't be read. Attach text files, PDFs or "
                                 "images (PNG, JPG, GIF, WebP). For Word files, save them as PDF first.")
            stem, ext = Path(name).stem, Path(name).suffix
            n = 1
            while name.lower() in used:
                n += 1
                name = f"{stem} ({n}){ext}"
            used.add(name.lower())
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
            if kind == "pdf":
                (folder / (name + EXTRACTED_SUFFIX)).write_text(_pdf_text(data), encoding="utf-8")
            records.append({"name": name, "kind": kind, "size": len(data),
                            "path": f"attachments/{batch}/{name}"})
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return records


def remove(workdir: Path, records: list[dict]) -> None:
    for batch in {Path(r["path"]).parent for r in records}:
        shutil.rmtree(workdir / batch, ignore_errors=True)


def resolve(workdir: Path, rel: str) -> Path | None:
    """The file for an attachment path, only if it lies inside the attachments folder."""
    root = (workdir / "attachments").resolve()
    path = (workdir / rel).resolve()
    return path if path.is_file() and root in path.parents else None


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1250", errors="replace")


def prompt_context(workdir: Path, records: list[dict]) -> tuple[str, list[str]]:
    """(text to put before the user's message, absolute image paths for codex)."""
    if not records:
        return "", []
    parts = ["The user attached the files below to this conversation. Text and PDF contents are "
             "included here. Every file is also saved in your working directory at the path shown, "
             "so you can open it yourself; open the images to look at them."]
    images, budget = [], MAX_INLINE_TOTAL
    for r in records:
        path = workdir / r["path"]
        header = f"### Attached file: {r['name']} (path: {r['path']})"
        if not path.exists():
            parts.append(f"{header}\n[This file is no longer available.]")
            continue
        if r["kind"] == "image":
            images.append(str(path.resolve()))
            parts.append(f"{header}\n[Image. Open the file to look at it.]")
            continue
        text = _read_text(path.with_name(path.name + EXTRACTED_SUFFIX) if r["kind"] == "pdf" else path)
        limit = min(MAX_INLINE_PER_FILE, budget)
        if limit <= 0:
            parts.append(f"{header}\n[Not included here because the attachments are too long; open the file.]")
            continue
        if len(text) > limit:
            text = text[:limit] + f"\n[… cut off here; the full file is at {r['path']}]"
        budget -= len(text)
        parts.append(f"{header}\n<<<FILE\n{text}\nFILE>>>")
    return "\n\n".join(parts), images
