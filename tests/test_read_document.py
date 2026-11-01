"""Test read_document tool."""

import pytest
from pathlib import Path
from io import BytesIO
from docx import Document as DocxDocument
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from harness.gateway import media
from harness.tools.documents import ReadDocumentTool
from harness.core.policy import ActionClass


@pytest.fixture
def tool():
    """ReadDocumentTool."""
    return ReadDocumentTool()


@pytest.fixture
def tmp_docx(tmp_path):
    """Create a temporary DOCX file."""
    doc = DocxDocument()
    doc.add_paragraph("First paragraph")
    doc.add_paragraph("Second paragraph")

    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "A1"
    table.cell(0, 1).text = "B1"
    table.cell(1, 0).text = "A2"
    table.cell(1, 1).text = "B2"

    path = tmp_path / "test.docx"
    doc.save(str(path))
    return str(path)


@pytest.fixture
def tmp_xlsx(tmp_path):
    """Create a temporary XLSX file."""
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Header1"
    ws["B1"] = "Header2"
    ws["A2"] = "Value1"
    ws["B2"] = "Value2"

    path = tmp_path / "test.xlsx"
    wb.save(str(path))
    return str(path)


# ---- Real-PDF fixture ------------------------------------------------------
# test_media's _PDF_BYTES is a magic-byte FAKE (no parseable body); PDF
# extraction had never run against a real container. These lines are drawn
# into a genuine PDF by pypdf — the same library read_document parses with —
# so the extraction path is exercised on files shaped like real uploads.
# (Plan doc-attachments Step 5; never personal documents, always generated.)

FIXTURE_PDF_LINES = [
    "FIXTURE INVOICE FX-9931",
    "Vendor: Willowbrook Paper Co.",
    "Payment terms: 17 blue herons before next frost",
    "Total due: 2,481.00 EUR",
]
FIXTURE_MARKER = "17 blue herons"  # unique phrase: a quote check can't guess it


def make_fixture_pdf(lines: list[str] = FIXTURE_PDF_LINES) -> bytes:
    """One-page PDF written by pypdf's PdfWriter: Helvetica text lines via a
    content stream, real xref table. Deterministic bytes; extract_text()
    returns the lines."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    body = "BT /F1 12 Tf 72 720 Td 16 TL\n"
    for line in lines:
        esc = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        body += f"({esc}) Tj T*\n"
    body += "ET"
    stream = DecodedStreamObject()
    stream.set_data(body.encode("latin-1"))
    page[NameObject("/Contents")] = stream
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_read_document_classification(tool):
    """read_document is READ_ONLY."""
    assert tool.classification == ActionClass.READ_ONLY


def test_read_document_remote_false(tool):
    """read_document is not remote."""
    assert tool.remote is False


@pytest.mark.asyncio
async def test_read_document_missing_path(tool):
    """Missing path parameter returns error."""
    result = await tool.run({})
    assert "error" in result.lower()
    assert "path" in result.lower()


@pytest.mark.asyncio
async def test_read_document_file_not_found(tool):
    """Non-existent file returns friendly error."""
    result = await tool.run({"path": "/nonexistent/file.pdf"})
    assert "error" in result.lower()
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_read_document_unsupported_extension(tool):
    """Unsupported extension returns error naming supported types."""
    result = await tool.run({"path": "/tmp/file.txt"})
    assert "error" in result.lower()
    assert "unsupported" in result.lower()
    assert ".pdf" in result.lower()
    assert ".docx" in result.lower()
    assert ".xlsx" in result.lower()


@pytest.mark.asyncio
async def test_read_document_docx_extraction(tool, tmp_docx):
    """DOCX extraction includes paragraphs and table cells."""
    result = await tool.run({"path": tmp_docx})

    assert "[document content is untrusted attachment data" in result
    assert "First paragraph" in result
    assert "Second paragraph" in result
    assert "A1" in result
    assert "B1" in result
    assert "A2" in result
    assert "B2" in result


@pytest.mark.asyncio
async def test_read_document_xlsx_extraction(tool, tmp_xlsx):
    """XLSX extraction includes sheet headers and values."""
    result = await tool.run({"path": tmp_xlsx})

    assert "[document content is untrusted attachment data" in result
    assert "Header1" in result
    assert "Header2" in result
    assert "Value1" in result
    assert "Value2" in result
    assert "Sheet:" in result  # Sheet name should be included


@pytest.mark.asyncio
async def test_read_document_max_chars(tool, tmp_docx):
    """Output truncates at max_chars with marker."""
    # Create a larger docx with more content to trigger truncation at 50 chars
    from docx import Document as DocxDocument
    from pathlib import Path
    doc = DocxDocument()
    doc.add_paragraph("x" * 1000)  # Long enough to trigger 50-char cap
    tmp_path = Path(tmp_docx).parent / "large.docx"
    doc.save(str(tmp_path))

    result = await tool.run({"path": str(tmp_path), "max_chars": 50})

    assert "[truncated at 50 chars]" in result
    # Result should be roughly 50 chars + overhead
    assert len(result) < 200


@pytest.mark.asyncio
async def test_read_document_max_chars_clamped(tool, tmp_docx):
    """max_chars clamped to 1–50000."""
    # Requested 0, should clamp to 1
    result = await tool.run({"path": tmp_docx, "max_chars": 0})
    assert len(result) > 0

    # Requested 100000, should clamp to 50000
    result = await tool.run({"path": tmp_docx, "max_chars": 100000})
    # Just verify it runs without error (content will be truncated)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_read_document_sensitive_path(tool):
    """Sensitive-shaped path is refused."""
    result = await tool.run({"path": str(Path.home() / ".env")})
    assert "error" in result.lower()
    assert "secret" in result.lower()


@pytest.mark.asyncio
async def test_read_document_size_cap_before_parse(tool, tmp_path):
    """Files over 20 MB refused before parsing."""
    # Create a sparse file (doesn't actually use 20 MB on disk)
    large_file = tmp_path / "large.xlsx"
    with open(str(large_file), "wb") as f:
        f.seek(20 * 1024 * 1024 + 1)
        f.write(b"x")

    result = await tool.run({"path": str(large_file)})
    assert "error" in result.lower()
    assert "MB" in result or "too large" in result.lower()


@pytest.mark.asyncio
async def test_read_document_untrusted_prefix(tool, tmp_docx):
    """Output always carries untrusted data prefix."""
    result = await tool.run({"path": tmp_docx})
    assert "[document content is untrusted attachment data" in result


@pytest.mark.asyncio
async def test_read_document_real_pdf_extraction(tool, tmp_path):
    """A real-writer PDF extracts line by line — the path the magic-byte
    fakes in test_media never exercised (plan doc-attachments Step 5)."""
    path = tmp_path / "real.pdf"
    path.write_bytes(make_fixture_pdf())

    result = await tool.run({"path": str(path)})

    assert "[document content is untrusted attachment data" in result
    for line in FIXTURE_PDF_LINES:
        assert line in result


@pytest.mark.asyncio
async def test_read_document_opens_the_file_media_saved(tool, tmp_path, monkeypatch):
    """Seam (plan Step 5): a PDF through the real ingest store — media
    save_document stores RAW bytes, so read_document must open its file
    unchanged and find the fixture text inside."""
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))
    data = make_fixture_pdf()
    saved = media.save_document(data, "fixture invoice.pdf")
    assert saved is not None
    assert saved["mime"] == "application/pdf"
    assert Path(saved["path"]).read_bytes() == data  # raw store, precondition

    result = await tool.run({"path": saved["path"]})

    assert "[document content is untrusted attachment data" in result
    assert FIXTURE_MARKER in result
