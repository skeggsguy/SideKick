"""Media ingest: the storage half behind POST /api/media and GET /media/{id}.

Chat photo attachments are saved here as recompressed, metadata-stripped
files under the file jail's first write root (<root>/uploads), so
drive_upload sees them through the same jail (core/paths.py). Env is read
at call time, never import time — the paths.py convention that lets tests
monkeypatch HARNESS_FILE_ROOTS.

Document attachments (PDF/DOCX/XLSX/TXT/MD, plan doc-attachments Step 1)
share the directory and the id scheme but NOT the pipeline: their bytes
are stored RAW — read_document dispatches on the file suffix and
drive_upload ships the file as-is, so any re-write here would corrupt or
mislabel them. The original filename rides a per-file `<stored>.name`
sidecar (sanitized to [A-Za-z0-9._-]) so the id itself stays a pure
content hash and the traversal regex keeps its exact shape.

Two deliberate jobs beyond saving bytes:

- Sniff by magic bytes and reject anything else: images by format alone
  (HEIC and friends stay out of scope), documents by magic PLUS the
  claimed extension — a DOCX and an XLSX are the same PK zip, and the
  stored extension always comes from the sniffed mime, so a client can't
  smuggle a file past read_document's suffix dispatch.
- For images only, the Pillow round-trip: EXIF orientation applied, long
  edge capped at MAX_EDGE px, and the file re-saved WITHOUT metadata. The
  strip is the privacy point — the opt-in Sonnet lane ships these bytes to
  the cloud, and a phone photo carries GPS.

The filename is sha256(stored bytes)[:16] + ext: content-hash dedupe (the
same file attached twice is one file) and an id that GET /media/{id} can
regex-validate — the traversal guard lives here, in the id pattern.
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ..core.paths import allowed_write_roots

#: Long-edge cap for the recompressed output. ~1568 px is what the vision
#: encoder actually resolves; anything larger is wasted bytes and cloud
#: transfer on the opt-in Sonnet lane.
MAX_EDGE = 1568

#: The whole contract for GET /media/{id}: a 16-hex content hash plus one
#: approved extension. fullmatch against this is what makes the id
#: untraversable. The extension list is mirrored in webui/thumbs.js
#: MEDIA_ID_RE (JS can't import this) — pinned synced by tests/test_media.
MEDIA_ID_RE = re.compile(
    r"[0-9a-f]{16}\.(?:jpg|png|webp|gif|pdf|docx|xlsx|txt|md)"
)

_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

#: Document half of _EXT_TO_MIME: the claims sniff_document admits, plus the
#: reverse map that turns a sniffed mime into the stored extension.
_DOC_EXTS = (".pdf", ".docx", ".xlsx", ".txt", ".md")
_DOC_MIME_TO_EXT = {v: k for k, v in _EXT_TO_MIME.items() if k in _DOC_EXTS}


def sniff_image(data: bytes) -> str | None:
    """Magic bytes only -> a supported mime, or None to reject.

    Deliberately not a real format detection: Pillow's own errors are
    vague, and a sniff failure here is a clean 415. HEIC/HEIF and video
    are out of scope (plan: v1 is JPEG/PNG/WebP/GIF).
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):  # 89 50 4E 47
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_document(data: bytes, name: str) -> str | None:
    """Magic bytes + claimed extension -> a supported document mime, or None.

    A document's real type is only half bytes: DOCX and XLSX are the same
    PK zip container, and TXT vs MD is invisible in bytes — so the claimed
    extension (the upload's ?name=) disambiguates, and every accepted pair
    stores under the SNIFFED mime's extension. read_document dispatches on
    the file suffix, so a lying claim either rejects here or stores the
    true type; a mislabelled file never reaches the reader.

    - %PDF- magic + .pdf claim -> application/pdf
    - PK\\x03\\x04 zip magic + .docx/.xlsx claim -> the matching OOXML mime
    - .txt/.md claim + bytes decoding as UTF-8 (latin-1 fallback accepted)
    - anything else -> None (the route makes that 415)
    """
    if not data:
        return None
    ext = Path(name.replace("\\", "/")).suffix.lower()
    if data.startswith(b"%PDF-"):
        return _EXT_TO_MIME[".pdf"] if ext == ".pdf" else None
    if data.startswith(b"PK\x03\x04"):
        return _EXT_TO_MIME[ext] if ext in (".docx", ".xlsx") else None
    if ext in (".txt", ".md"):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            data.decode("latin-1")  # latin-1 never fails; the plan accepts it
        return _EXT_TO_MIME[ext]
    return None


#: Sidecar name cap: roomy for real filenames, short enough that a hostile
#: ?name= can't bloat the Content-Disposition header resolve() feeds GET.
_NAME_MAX = 120


def _sanitize_name(name: str) -> str:
    """Display-safe form of an uploaded filename for the sidecar: basename
    only (file inputs send "C:\\fakepath\\x.pdf"), [A-Za-z0-9._-] only — no
    separators, no quotes, nothing needing escapes in a header — leading and
    trailing dots dropped ("..", dotfiles), capped."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip(".")[:_NAME_MAX]


def uploads_dir() -> Path:
    """<first write root>/uploads, created on demand, computed at call time
    (HARNESS_FILE_ROOTS is an env read, so monkeypatching it steers tests)."""
    root = Path(allowed_write_roots()[0]) / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _reencode(data: bytes, mime: str) -> tuple[bytes, str]:
    """Orientation-corrected, downscaled, metadata-free re-encode.

    Returns (bytes, mime); the mime round-trips to itself — each input
    format keeps its format, only the bytes are re-made. Saving a freshly
    opened image without passing ``exif=`` is what drops the metadata;
    Pillow never copies it across on its own — so never "helpfully" pass
    ``exif=img.info["exif"]``: that re-embeds GPS.
    """
    img = Image.open(io.BytesIO(data))
    img.verify()  # full structural check before we commit to the work
    img = Image.open(io.BytesIO(data))  # verify() consumes the image
    img = ImageOps.exif_transpose(img)  # bake orientation in, then drop EXIF

    long_edge = max(img.size)
    if long_edge > MAX_EDGE:  # never upscale a small photo
        scale = MAX_EDGE / long_edge
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.Resampling.LANCZOS,
        )

    buf = io.BytesIO()
    if mime == "image/jpeg":
        img.convert("RGB").save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    if mime == "image/png":
        img.save(buf, "PNG")  # PNG save preserves alpha for RGBA/LA modes
        return buf.getvalue(), "image/png"
    if mime == "image/webp":
        keep = "RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB"
        img.convert(keep).save(buf, "WEBP", quality=85)
        return buf.getvalue(), "image/webp"
    # GIF: re-quantize to a palette if the pipeline left it in RGB (a
    # downscaled P image becomes RGB); Pillow's GIF save does the palette.
    if img.mode not in ("1", "P"):
        img = img.convert("P", palette=Image.Palette.ADAPTIVE)
    img.save(buf, "GIF")
    return buf.getvalue(), "image/gif"


def save_image(data: bytes) -> dict | None:
    """Sniff, re-encode, content-hash-save. -> attachment dict, or None when
    the bytes are not a supported/decodable image (the route makes that 415).

    The dict is the Step 1 attachment shape plus a url: {id, mime, path}
    with ``path`` a STR (it rides Message.attachments through asdict into
    the JSON payload — a Path would not serialize). If the id pattern ever
    changes, this dict's consumers (GET route, /media/{id} urls) change with
    it. Pinned test_media (see .claude/plan image attachments Step 3).
    """
    mime = sniff_image(data)
    if mime is None:
        return None
    try:
        out, out_mime = _reencode(data, mime)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
        # Passed the magic-byte sniff but Pillow can't decode it: a corrupt
        # or truncated upload. Reject like an unsupported type, but say so —
        # the gateway log is the only trace.
        print(f"[media] rejected undecodable {mime} upload: {e}", file=sys.stderr)
        return None

    id = hashlib.sha256(out).hexdigest()[:16]
    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }[out_mime]
    fname = f"{id}{ext}"
    path = uploads_dir() / fname
    if not path.exists():  # content-hash dedupe: same bytes, one file
        path.write_bytes(out)
    return {
        "id": fname,
        "mime": out_mime,
        "path": str(path),
        "url": f"/media/{fname}",
    }


def save_document(data: bytes, name: str) -> dict | None:
    """Sniff, then store the RAW bytes — no re-encode (EXIF stripping is an
    image concern; read_document and drive_upload must see the real file).

    Stored in uploads_dir() as sha256(bytes)[:16] + the SNIFFED mime's
    extension, with the sanitized original filename in a `<stored>.name`
    sidecar — that is what keeps the id a pure content hash (the traversal
    regex shape untouched) while the chip can still show "invoice.pdf".

    -> attachment dict {id, mime, path, url, name}, or None when the
    bytes/claim pair is not a supported document (the route makes that 415).
    Same bytes twice is one file; the sidecar keeps the first upload's name.
    """
    mime = sniff_document(data, name)
    if mime is None:
        return None
    id = hashlib.sha256(data).hexdigest()[:16]
    fname = f"{id}{_DOC_MIME_TO_EXT[mime]}"
    path = uploads_dir() / fname
    stored_name = _sanitize_name(name)
    if not path.exists():  # content-hash dedupe: same bytes, one file
        path.write_bytes(data)
    sidecar = path.with_name(fname + ".name")
    if not sidecar.exists():  # also heals a crash between the two writes
        sidecar.write_text(stored_name, encoding="utf-8")
    return {
        "id": fname,
        "mime": mime,
        "path": str(path),
        "url": f"/media/{fname}",
        "name": stored_name,
    }


def resolve(id: str) -> dict | None:
    """Id -> {path, mime, url, name}, or None if the id is malformed or no
    such file exists. The regex is the whole traversal defense: a
    non-matching id never reaches the filesystem, and a matching one can
    only name a file inside uploads_dir(). `name` is the sidecar's
    sanitized original filename — "" for images and any file predating a
    sidecar (the sidecar itself can never be the id: ".name" fails the
    regex)."""
    if not MEDIA_ID_RE.fullmatch(id):
        return None
    path = uploads_dir() / id
    if not path.is_file():
        return None
    sidecar = path.with_name(id + ".name")
    name = ""
    if sidecar.is_file():
        # Re-sanitize on the way OUT, not just on write: the sidecar sits in
        # a write root (file tools can edit it), and the GET route quotes
        # this value into Content-Disposition — a raw CR/LF there is a 500
        # (h11 refuses the header) at best. errors="replace" keeps a corrupt
        # sidecar from turning a download into a served 500 too.
        name = _sanitize_name(
            sidecar.read_text(encoding="utf-8", errors="replace")
        )
    return {
        "path": path,
        "mime": _EXT_TO_MIME["." + id.rsplit(".", 1)[1]],
        "url": f"/media/{id}",
        "name": name,
    }
