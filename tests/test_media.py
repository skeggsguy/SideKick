"""Media ingest + serve: POST /api/media and GET /media/{id} (plan
image-attachments Step 3).

All fixtures are Pillow-generated in code — never a real photo, never a
file outside the scratch tmp dir. HARNESS_FILE_ROOTS is monkeypatched per
test (media.py reads it at call time), so uploads land in tmp_path.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import httpx
import pytest
from PIL import Image

from harness.core.approvals import ApprovalWaiter
from harness.core.events import EventBus
from harness.gateway import media
from harness.gateway.app import Gateway, create_app
from harness.gateway.routes import media as media_routes
from harness.store.approvals import ApprovalStore
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore

from test_gateway import FakeAgent


# ---- fixtures ---------------------------------------------------------------


def _jpeg_bytes(width: int = 32, height: int = 24, color=(200, 60, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG")
    return buf.getvalue()


def _png_bytes(mode="RGBA", size=(10, 10), color=(0, 255, 0, 128)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def _gif_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("P", (10, 10)).save(buf, "GIF")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (0, 0, 255)).save(buf, "WEBP")
    return buf.getvalue()


def _exif_jpeg_bytes() -> bytes:
    """A JPEG carrying a Make tag, a GPS sub-IFD AND Orientation=6 — the
    exact metadata the recompress must strip before the bytes can reach the
    cloud lane. Orientation matters: ImageOps.exif_transpose bakes it in and
    plants the REMAINING exif (Make, GPS) back into the new image's info;
    the strip test must pin that it doesn't survive the re-save either."""
    img = Image.new("RGB", (64, 48), (10, 200, 10))
    exif = img.getexif()
    exif[0x010F] = "EvilMaker"  # Make
    exif[0x0112] = 6  # Orientation: rotate 270 (the common phone-photo shape)
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (48, 0, 0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


@pytest.fixture
def file_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))
    return tmp_path


@pytest.fixture
async def client(db, file_root):
    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy: FakeAgent(),
    )
    app = create_app(gw)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"origin": "http://t"}
    ) as c:
        yield c


# ---- sniff_image ------------------------------------------------------------


def test_sniff_accepts_the_four_supported_types():
    assert media.sniff_image(_jpeg_bytes()) == "image/jpeg"
    assert media.sniff_image(_png_bytes()) == "image/png"
    assert media.sniff_image(_gif_bytes()) == "image/gif"
    assert media.sniff_image(_webp_bytes()) == "image/webp"


def test_sniff_rejects_non_images():
    assert media.sniff_image(b"GARBAGE") is None
    assert media.sniff_image(b"%PDF-1.4\nfake pdf body\n%%EOF\n") is None
    assert media.sniff_image(b"") is None


# ---- save_image (unit level) ------------------------------------------------


def test_save_image_rejects_non_images(file_root):
    assert media.save_image(b"GARBAGE" * 20) is None
    assert not (file_root / "uploads").exists()  # rejected before any write


def test_save_image_is_deterministic_and_dedupes(file_root):
    data = _jpeg_bytes()
    first = media.save_image(data)
    second = media.save_image(data)
    assert first["id"] == second["id"]
    # two identical uploads, one file — content-hash dedupe
    assert sorted(p.name for p in (file_root / "uploads").iterdir()) == [first["id"]]
    assert media.save_image(_jpeg_bytes(color=(5, 5, 200)))["id"] != first["id"]


def test_saved_id_matches_the_filename_pattern(file_root):
    for saver in (_jpeg_bytes, _png_bytes, _gif_bytes, _webp_bytes):
        saved = media.save_image(saver())
        assert media.MEDIA_ID_RE.fullmatch(saved["id"]), saved["id"]
        assert Path(saved["path"]).name == saved["id"]
        assert saved["url"] == f"/media/{saved['id']}"
        assert saved["path"].startswith(str(file_root))  # inside the jail


def test_save_image_strips_exif_and_gps(file_root):
    data = _exif_jpeg_bytes()
    # the fixture must actually carry the metadata, or this proves nothing
    pre = Image.open(io.BytesIO(data)).getexif()
    assert pre[0x010F] == "EvilMaker" and pre.get_ifd(0x8825)
    assert pre[0x0112] == 6  # orientation present, so exif_transpose runs

    saved = media.save_image(data)
    out = Image.open(saved["path"]).getexif()
    assert dict(out) == {}  # no EXIF survives (re-save never passes exif=)
    assert dict(out.get_ifd(0x8825)) == {}  # no GPS coordinates either
    assert Image.open(saved["path"]).size == (48, 64)  # orientation was baked
    # in (dimensions swapped), NOT re-emitted as an EXIF tag


def test_save_image_downcales_only(file_root):
    saved = media.save_image(_jpeg_bytes(width=2000, height=900))
    w, h = Image.open(saved["path"]).size
    assert max(w, h) <= media.MAX_EDGE
    assert abs(w / h - 2000 / 900) < 0.05  # aspect survived

    small = media.save_image(_jpeg_bytes(width=80, height=60))
    assert Image.open(small["path"]).size == (80, 60)  # never upscales


def test_save_image_preserves_png_alpha(file_root):
    saved = media.save_image(_png_bytes())
    assert saved["mime"] == "image/png"
    assert Image.open(saved["path"]).mode == "RGBA"


def test_resolve_rejects_malformed_ids(file_root):
    media.save_image(_png_bytes())  # ensure the dir + one file exist
    # A real decoy at the exact path a traversal id would resolve to
    # (uploads/../../passwd -> <root's parent>/passwd, since Path joins do
    # not normalize ".." but is_file() resolves them on the filesystem):
    # with the regex guard gone, resolve() would happily serve this file —
    # the test must go red.
    (file_root.parent / "passwd").write_bytes(b"TOP-SECRET-MARKER")
    for bad in (
        "zz.jpg",
        "",
        "ab12cd34ef56ab78.exe",
        "AB12CD34EF56AB78.png",  # uppercase: pattern is lowercase hex
        "../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "../../passwd",
        "..",
        "../passwd",
        "0123456789abcdef.png\x00",
        "sub/0123456789abcdef.png",
    ):
        assert media.resolve(bad) is None, bad


# ---- POST /api/media --------------------------------------------------------


async def test_post_saves_each_type(client, file_root):
    for data, mime in (
        (_jpeg_bytes(), "image/jpeg"),
        (_png_bytes(), "image/png"),
        (_gif_bytes(), "image/gif"),
        (_webp_bytes(), "image/webp"),
    ):
        resp = await client.post("/api/media", content=data)
        assert resp.status_code == 200, mime
        body = resp.json()
        assert body["mime"] == mime
        assert body["url"] == f"/media/{body['id']}"
        # path is server-internal — the wire answer is {id, mime, url} only
        assert set(body) == {"id", "mime", "url"}


async def test_post_rejects_non_image_with_415(client):
    resp = await client.post("/api/media", content=b"%PDF-1.4\nnot an image\n")
    assert resp.status_code == 415
    resp = await client.post(
        "/api/media", content=b"\xff\xd8\xff" + b"corrupt jpeg body" * 10
    )
    assert resp.status_code == 415  # sniffs as JPEG, dies in Pillow


async def test_post_rejects_oversize_with_413(client, monkeypatch):
    monkeypatch.setattr(media_routes, "MAX_MEDIA_BYTES", 64)
    resp = await client.post("/api/media", content=_jpeg_bytes())
    assert resp.status_code == 413


async def test_post_rejects_empty_body_with_400(client):
    resp = await client.post("/api/media", content=b"")
    assert resp.status_code == 400


# ---- GET /media/{id} --------------------------------------------------------


async def test_get_serves_the_saved_image(client):
    post = await client.post("/api/media", content=_jpeg_bytes(color=(3, 200, 3)))
    media_id = post.json()["id"]

    resp = await client.get(f"/media/{media_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert "immutable" in resp.headers["cache-control"]
    # JPEG is lossy: compare the fixture color with a small tolerance
    r, g, b = Image.open(io.BytesIO(resp.content)).convert("RGB").getpixel((0, 0))
    assert max(abs(r - 3), abs(g - 200), abs(b - 3)) <= 8


async def test_get_rejects_traversal_and_unknown_ids(client, file_root):
    # Routable-but-malformed ids DO reach resolve() — its regex answers
    # "no such media" (proving our handler ran, not Starlette's router).
    for nasty in ("zz.jpg", "AB12CD34EF56AB78.png", "0123456789abcdef.png"):
        resp = await client.get(f"/media/{nasty}")
        assert resp.status_code == 404, nasty
        assert resp.json()["detail"] == "no such media", nasty
    # Encoded traversal shapes never even reach the handler: Starlette's
    # router rejects %2e/%2f path segments before routing (defence-in-depth
    # on top of the regex — resolve() would kill them anyway).
    for nasty in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "..%2F..%2Fpasswd"):
        resp = await client.get(f"/media/{nasty}")
        assert resp.status_code == 404, nasty
    # A decoy planted where "uploads/../../passwd" would resolve is the
    # resolve()-level red-check (test_resolve_rejects_malformed_ids) — the
    # HTTP layer can't reach it, so it stays a unit-level pin.
    assert (await client.get("/media/../passwd")).status_code == 404  # httpx normalized it to /passwd


async def test_media_router_lands_before_the_static_mount(client):
    # "/" serves the webui; /media must win for the router (app.py includes
    # routers before mounting). An unknown /media/... is the router's 404,
    # not the static handler's "not found" page.
    resp = await client.get("/media/deadbeefdeadbeef.png")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "no such media"


# ---- document ingest (plan doc-attachments Step 1) ---------------------------
#
# Fixtures are magic-byte fakes, not real PDF/OOXML files: sniff_document
# reads magic + claimed extension only, and save_document stores bytes raw —
# a real Word file would only make the fixture opaque. read_document's own
# tests own the parsing side: _PDF_BYTES is UNPARSEABLE (pypdf "startxref not
# found") — anything touching read_document uses test_read_document's make_fixture_pdf.

_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
_ZIP_BYTES = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"fake-zip-body" * 8  # > 64 B
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_sniff_document_accepts_pdf_zip_and_text():
    assert media.sniff_document(_PDF_BYTES, "invoice.pdf") == "application/pdf"
    assert media.sniff_document(_ZIP_BYTES, "a.docx") == _DOCX_MIME
    assert media.sniff_document(_ZIP_BYTES, "b.xlsx") == _XLSX_MIME
    assert media.sniff_document("héllo wörld".encode(), "notes.txt") == "text/plain"
    assert media.sniff_document(b"# head\n- item\n", "readme.md") == "text/markdown"
    # the latin-1 fallback is plan-sanctioned: bytes that fail strict UTF-8
    # still pass as text when the claim is a text extension
    assert media.sniff_document(b"caf\xe9 report\n", "cafe.txt") == "text/plain"
    # claims are matched case- and path-insensitively (file inputs send
    # "C:\fakepath\..." names)
    assert media.sniff_document(_PDF_BYTES, "INVOICE.PDF") == "application/pdf"
    assert media.sniff_document(_PDF_BYTES, "C:\\fakepath\\inv.pdf") == "application/pdf"


def test_sniff_document_disambiguates_zip_by_claimed_extension():
    # the same zip bytes, two mimes — and every other claim rejects
    docx = media.sniff_document(_ZIP_BYTES, "x.docx")
    xlsx = media.sniff_document(_ZIP_BYTES, "x.xlsx")
    assert docx == _DOCX_MIME and xlsx == _XLSX_MIME and docx != xlsx
    for claim in ("x.txt", "x.pdf", "x.zip", "noext", ""):
        assert media.sniff_document(_ZIP_BYTES, claim) is None, claim


def test_sniff_document_rejects_mismatched_and_unknown():
    assert media.sniff_document(_PDF_BYTES, "invoice.txt") is None  # claim lies
    assert media.sniff_document(_PDF_BYTES, "") is None  # PDF needs the claim
    assert media.sniff_document(b"GARBAGE" * 20, "x.pdf") is None  # bytes lie
    assert media.sniff_document(b"GARBAGE" * 20, "x.exe") is None
    assert media.sniff_document(b"", "empty.txt") is None


def test_save_document_stores_raw_bytes_with_sidecar(file_root):
    saved = media.save_document(_PDF_BYTES, "Invoice #7.pdf")
    assert media.MEDIA_ID_RE.fullmatch(saved["id"]), saved["id"]
    assert saved["id"].endswith(".pdf")  # ext from the SNIFFED mime
    assert Path(saved["path"]).read_bytes() == _PDF_BYTES  # raw, no re-encode
    assert saved["name"] == "Invoice_7.pdf"  # sanitized: " #" collapses to _
    sidecar = file_root / "uploads" / (saved["id"] + ".name")
    assert sidecar.read_text(encoding="utf-8") == "Invoice_7.pdf"
    assert saved["path"].startswith(str(file_root))  # inside the jail
    assert saved["url"] == f"/media/{saved['id']}"


def test_save_document_sanitizes_hostile_names(file_root):
    for nasty, want in (
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\x\\report.pdf", "report.pdf"),
        ("..\x00evil.pdf", "_evil.pdf"),
    ):
        saved = media.save_document(_PDF_BYTES, nasty)
        assert saved["name"] == want, nasty
        assert "/" not in saved["name"] and "\\" not in saved["name"]
    capped = media.save_document(_PDF_BYTES, "a" * 300 + ".pdf")
    assert len(capped["name"]) == media._NAME_MAX


def test_save_document_dedupes(file_root):
    first = media.save_document(_PDF_BYTES, "one.pdf")
    second = media.save_document(_PDF_BYTES, "renamed.pdf")
    assert first["id"] == second["id"]
    # same bytes twice: one file + one sidecar, sidecar keeps the first name
    assert sorted(p.name for p in (file_root / "uploads").iterdir()) == [
        first["id"],
        first["id"] + ".name",
    ]
    assert (file_root / "uploads" / (first["id"] + ".name")).read_text() == "one.pdf"
    # different bytes, different id — and the ext follows the sniffed type
    other = media.save_document(_ZIP_BYTES, "sheet.xlsx")
    assert other["id"].endswith(".xlsx") and other["id"] != first["id"]


def test_save_document_rejects_non_documents(file_root):
    assert media.save_document(b"GARBAGE" * 20, "x.exe") is None
    assert media.save_document(_PDF_BYTES, "doc.txt") is None
    assert not (file_root / "uploads").exists()  # rejected before any write


def test_resolve_returns_sidecar_name_with_fallback(file_root):
    doc = media.save_document(_PDF_BYTES, "report.pdf")
    img = media.save_image(_png_bytes())  # old-shape id must still resolve
    assert media.resolve(doc["id"])["name"] == "report.pdf"
    assert media.resolve(img["id"])["name"] == ""  # images have no sidecar
    # the sidecar is never itself resolvable: ".name" fails the regex
    assert media.resolve(doc["id"] + ".name") is None


async def test_get_document_re_sanitizes_a_tampered_sidecar(client, file_root):
    """The sidecar lives INSIDE a write root, so resolve() cannot trust what
    it holds: the GET route quotes it into Content-Disposition, and a raw
    CR/LF there is a 500 (h11 refuses the header) at best. Sanitizing only on
    write protects the upload path, not the serve path."""
    post = await client.post(
        "/api/media", content=_PDF_BYTES, params={"name": "safe.pdf"}
    )
    doc_id = post.json()["id"]
    sidecar = file_root / "uploads" / (doc_id + ".name")

    sidecar.write_text('evil\r\nX-Injected: 1"; x.pdf', encoding="utf-8")
    resp = await client.get(f"/media/{doc_id}")
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd, (
        "the .name sidecar sits INSIDE a write root (file tools can edit it) — "
        "media.resolve must re-sanitize on READ, not just on upload"
    )
    assert cd == 'attachment; filename="evil_X-Injected_1_x.pdf"'

    # a corrupt (non-UTF-8) sidecar must not turn the download into a 500
    sidecar.write_bytes(b"\xff\xfereport.pdf")
    resp = await client.get(f"/media/{doc_id}")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="_report.pdf"'


def test_resolve_rejects_traversal_shaped_doc_ids(file_root):
    media.save_document(_PDF_BYTES, "x.pdf")
    # Decoys at the exact filesystem paths these ids would reach if the
    # regex guard were gone — uploads/ is <root>/uploads, so "../../passwd.pdf"
    # lands beside the root. Without the decoys the loop proves nothing: a
    # resolve() with MEDIA_ID_RE = /.*/ would still answer None (no such file).
    (file_root.parent / "passwd.pdf").write_bytes(b"TOP-SECRET-MARKER")
    (file_root / "passwd.pdf").write_bytes(b"TOP-SECRET-MARKER")
    (file_root / "passwd").write_bytes(b"TOP-SECRET-MARKER")
    for bad in (
        "../../passwd.pdf",
        "../passwd.pdf",
        "../passwd",
        "..",
        ".pdf",
        "0123456789abcdef\\pdf",  # backslash separator
        "0123456789abcdef.PDF",  # uppercase ext is not in the list
        "sub/0123456789abcdef.pdf",
    ):
        assert media.resolve(bad) is None, bad


async def test_post_document_round_trips_name(client, file_root):
    resp = await client.post(
        "/api/media", content=_PDF_BYTES, params={"name": "invoice.pdf"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mime"] == "application/pdf"
    assert body["id"].endswith(".pdf")
    assert body["name"] == "invoice.pdf"
    assert set(body) == {"id", "mime", "url", "name"}  # path stays internal

    get = await client.get(f"/media/{body['id']}")
    assert get.status_code == 200
    assert get.content == _PDF_BYTES  # raw bytes, byte-identical
    assert get.headers["content-type"] == "application/pdf"
    assert "attachment" in get.headers["content-disposition"]
    assert '"invoice.pdf"' in get.headers["content-disposition"]
    assert "immutable" in get.headers["cache-control"]  # cache headers kept


async def test_get_image_still_serves_inline(client):
    # images unchanged: no Content-Disposition on the thumbnail response
    post = await client.post("/api/media", content=_jpeg_bytes())
    resp = await client.get(f"/media/{post.json()['id']}")
    assert resp.status_code == 200
    assert "content-disposition" not in resp.headers


async def test_post_doc_cap_and_branch_isolation(client, monkeypatch):
    monkeypatch.setattr(media_routes, "MAX_DOC_BYTES", 64)
    resp = await client.post(
        "/api/media", content=_ZIP_BYTES, params={"name": "a.docx"}
    )
    assert resp.status_code == 413  # document over the (patched) doc cap
    # the image branch keeps its OWN cap: this jpeg is >64 B but far <10 MB
    resp = await client.post("/api/media", content=_jpeg_bytes())
    assert resp.status_code == 200


async def test_post_rejects_unknown_doc_type_with_415(client):
    resp = await client.post(
        "/api/media", content=b"nothing useful", params={"name": "x.exe"}
    )
    assert resp.status_code == 415
    resp = await client.post("/api/media", content=_PDF_BYTES)  # no name claim
    assert resp.status_code == 415
    resp = await client.post("/api/media", content=b"", params={"name": "x.txt"})
    assert resp.status_code == 400  # empty still 400 on the doc branch


def test_media_id_pattern_synced_across_py_and_js():
    """The id regex is the traversal guard AND the webui's src guard. JS
    can't import the Python, so all three extension lists are pinned equal
    here (gotchas: an id-pattern change must sync thumbs.js + test_media)."""
    py_exts = set(media.MEDIA_ID_RE.pattern.split("(?:")[1].split(")")[0].split("|"))
    assert py_exts == {ext[1:] for ext in media._EXT_TO_MIME}
    js_src = (Path(media.__file__).parent / "webui" / "thumbs.js").read_text()
    m = re.search(
        r"MEDIA_ID_RE = /\^\[0-9a-f\]\{16\}\\\.\(\?:([a-z|]+)\)\$/", js_src
    )
    assert m, "thumbs.js MEDIA_ID_RE drifted from the pinned literal shape"
    assert set(m.group(1).split("|")) == py_exts
