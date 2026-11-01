"""BK-003: HTML-only email bodies for the Gmail read tools (offline).

gmail_read / gmail_get_draft fall back to a stripped text/html body when the
message carries no text/plain part; multipart text+HTML still prefers the
plain part verbatim. All offline: the Gmail API is the same canned-payload
fake used by test_workspace_writes (nested dicts with base64url body.data).
"""

from __future__ import annotations

import base64
from pathlib import Path

from mcp_servers.google_workspace import gmail
from mcp_servers.google_workspace._htmlbody import html_to_text, resolve_body

_FIXTURES = Path(__file__).parent / "fixtures" / "email"
_AGENT_HTML = (_FIXTURES / "agent_auto_reply.html").read_text()

_PIXEL_HOST = "t.example-estate.au"
_LISTING_URL = "https://www.example-estate.au/listing/8891?src=auto&campaign=openhouse"
_UNSUB = "www.example-estate.au/unsubscribe?h=Zm9vYmFy"


# -- fake Gmail API (same payload shape the real API returns) ------------------

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _part(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


_HEADERS = [
    {"name": "From", "value": "jane@example-estate.au"},
    {"name": "To", "value": "tom@example.com"},
    {"name": "Subject", "value": "RE: 12 Wattle Street, Fairview"},
    {"name": "Date", "value": "Sat, 24 Oct 2026 09:20:00 +1000"},
]


def _html_payload(html: str) -> dict:
    return {"mimeType": "text/html", "headers": _HEADERS, "body": {"data": _b64(html)}}


def _multipart_payload(plain: str, html: str) -> dict:
    return {
        "mimeType": "multipart/alternative",
        "headers": _HEADERS,
        "parts": [_part("text/plain", plain), _part("text/html", html)],
    }


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Gettable:
    def __init__(self, result):
        self._result = result

    def get(self, **kwargs):
        return _Req(self._result)


class _FakeGmailService:
    """messages().get and drafts().get both serve the one canned message —
    everything these tests exercise reads through one of those two."""

    def __init__(self, message):
        self._message = message

    def users(self):
        return self

    def messages(self):
        return _Gettable(self._message)

    def drafts(self):
        return _Gettable({"id": "draft1", "message": self._message})


def _message(payload: dict, snippet: str = "TRUNCATED SNIPPET ONLY") -> dict:
    return {"id": "msg1", "snippet": snippet, "payload": payload}


def _read(monkeypatch, message: dict) -> dict:
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: _FakeGmailService(message))
    return gmail.gmail_read("msg1")


# -- gmail_read / gmail_get_draft end to end -----------------------------------

def test_gmail_read_html_only_returns_real_body_not_snippet(monkeypatch):
    """The BK-003 symptom: HTML-only agent auto-reply used to yield the
    snippet. Now the parsed body must come back with real content lines."""
    msg = _message(_html_payload(_AGENT_HTML))
    out = _read(monkeypatch, msg)
    body = out["body"]
    assert body != msg["snippet"]
    lines = body.splitlines()
    assert "Hi Tom," in lines  # at least one non-placeholder body line
    assert "12 Wattle Street, Fairview" in body


def test_gmail_get_draft_html_fallback(monkeypatch):
    """Drafts share the resolver: an HTML-only draft preview shows the body,
    not the snippet (the approval UI reviews real content)."""
    html = "<div>Hello <b>there</b></div><p>Call on <a href='https://x.y/1'>the site</a>.</p>"
    msg = _message(_html_payload(html))
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: _FakeGmailService(msg))
    out = gmail.gmail_get_draft("draft1")
    assert out["body"] == "Hello there\nCall on the site (https://x.y/1)."


def test_gmail_read_multipart_still_prefers_plain(monkeypatch):
    """No behavior change for multipart text+HTML: the plain part is returned
    verbatim, the HTML representation never leaks in."""
    plain = "PLAIN BODY LINE\nsecond line"
    msg = _message(_multipart_payload(plain, "<p>HTMLONLYMARKER</p>"))
    out = _read(monkeypatch, msg)
    assert out["body"] == plain
    assert "HTMLONLYMARKER" not in out["body"]


def test_gmail_read_nested_html_only_fallback(monkeypatch):
    """multipart/mixed wrapping an html-only alternative + a real attachment:
    the nested HTML part is found and stripped; attachment listing unaffected."""
    payload = {
        "mimeType": "multipart/mixed",
        "headers": _HEADERS,
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [_part("text/html", "<p>Line one</p><p>Line two</p>")],
            },
            {
                "mimeType": "application/pdf",
                "filename": "brochure.pdf",
                "body": {"attachmentId": "att1", "size": 7},
            },
        ],
    }
    out = _read(monkeypatch, _message(payload))
    assert out["body"] == "Line one\nLine two"
    assert out["attachments"][0]["filename"] == "brochure.pdf"


def test_gmail_read_without_plain_or_html_falls_back_to_snippet(monkeypatch):
    msg = _message(
        {"mimeType": "application/octet-stream", "headers": _HEADERS,
         "body": {"size": 0}},
        snippet="the snippet survives",
    )
    out = _read(monkeypatch, msg)
    assert out["body"] == "the snippet survives"


# -- resolve_body unit level ----------------------------------------------------

def test_resolve_body_prefers_plain_over_html():
    payload = _multipart_payload("the plain wins", "<p>the html</p>")
    assert resolve_body(payload) == "the plain wins"


def test_resolve_body_empty_payload():
    assert resolve_body({"mimeType": "text/plain", "body": {}}) == ""


def test_resolve_body_empty_plain_part_falls_back_to_html():
    """An EMPTY text/plain part is no alternative at all — the HTML part is the
    only body the message has, so it is read instead of losing the body. For
    any non-empty plain part the preference stays byte-for-byte as before."""
    payload = _multipart_payload("", "<p>HTML BODY</p>")
    assert resolve_body(payload) == "HTML BODY"


# -- html_to_text unit level ------------------------------------------------------

def test_html_to_text_strips_tags_and_keeps_line_breaks():
    assert html_to_text("<p>alpha</p><p>beta</p>") == "alpha\nbeta"
    assert html_to_text("one<br>two<br/>three") == "one\ntwo\nthree"
    # Table cells: one line each, not glued, no blank line per cell.
    html = "<table><tr><td>beds</td><td>2</td></tr><tr><td>baths</td><td>1</td></tr></table>"
    assert html_to_text(html) == "beds\n2\nbaths\n1"


def test_html_to_text_preserves_links_with_urls():
    out = html_to_text('<a href="https://e.x/p?id=1&amp;t=2">see listing</a>')
    assert out == "see listing (https://e.x/p?id=1&t=2)"
    # A label that already IS the url is not printed twice.
    assert html_to_text('<a href="https://e.x/">https://e.x/</a>') == "https://e.x/"
    # mailto whose label is the address drops the redundant href.
    assert (
        html_to_text('<a href="mailto:a@b.c">a@b.c</a>') == "a@b.c"
    )
    # javascript: links keep their label only.
    assert html_to_text('<a href="javascript:void(0)">click</a>') == "click"


def test_html_to_text_entities_and_whitespace():
    html = "<p>O&#39;Brien&nbsp;&amp; Co<span>​</span>  packed   text</p>"
    assert html_to_text(html) == "O'Brien & Co packed text"


def test_html_to_text_trims_quotes_style_and_script():
    html = (
        "<style>.SECRETSTYLE{color:red}</style>"
        "<p>Keep this line.</p>"
        "<script>boot('SECRETSCRIPT')</script>"
        "<div class='gmail_quote'>On Mon Bob wrote:<blockquote>quoted words</blockquote></div>"
        "<blockquote>more quoted words</blockquote>"
    )
    out = html_to_text(html)
    assert out == "Keep this line."
    for noise in ("SECRETSTYLE", "SECRETSCRIPT", "quoted words", "On Mon Bob"):
        assert noise not in out


def test_html_to_text_trims_hidden_wrappers_and_tracking_pixels():
    html = (
        "<div style='display:none'>hidden preheader text</div>"
        "<p>visible line</p>"
        f"<a href='https://{_PIXEL_HOST}/o?id=x'><img width='1' height='1' alt='track' /></a>"
        f"<img src='https://{_PIXEL_HOST}/px.gif' style='display:none'>"
    )
    out = html_to_text(html)
    assert out == "visible line"
    assert _PIXEL_HOST not in out and "hidden preheader" not in out


def test_html_to_text_trims_signature_dividers_keeps_signature():
    html = "<p>intro</p><p>--</p><p>Jane Doe, Agent 0400 000 000</p>"
    out = html_to_text(html)
    assert "--" not in out.splitlines()
    assert out == "intro\nJane Doe, Agent 0400 000 000"


def test_html_to_text_drops_nested_children_of_hidden_and_quote_wrappers():
    """Real quote wrappers hold SEVERAL nested blocks, and hidden preheaders
    often hold a nested div; every child of the dropped subtree must stay
    dropped, not just the text before the first inner closing tag."""
    html = (
        "<div style='display:none'>HIDDEN1<div>HIDDEN2</div>HIDDEN3</div>"
        "<div class='gmail_quote'>QUOTED1<div>QUOTED2</div>QUOTED3"
        "<blockquote>QUOTED4</blockquote></div>"
        "<div class='gmail_quote'><a href='https://q.example/x'>QUOTED5</div>"
        "<p>visible line</p>"
    )
    out = html_to_text(html)
    assert out == "visible line", (
        "a skip-subtree tracker keyed on tag NAMES desyncs on nested same-name elements — track EVERY "
        "open tag + the drop's root index; void tags return early in handle_endtag (startendtag calls BOTH)"
    )
    for noise in ("HIDDEN1", "HIDDEN2", "HIDDEN3", "QUOTED1", "QUOTED2",
                  "QUOTED3", "QUOTED4", "QUOTED5", "q.example"):
        assert noise not in out, noise


def test_html_to_text_suppresses_non_fetchable_hrefs():
    """cid: inline-image refs and data: payloads are not destinations the model
    can use; a data: URI can be huge and would truncate the real body."""
    html = (
        '<p>see <a href="cid:image001@abc123">the photo</a></p>'
        '<p><a href="data:text/html;base64,QUJDREVGR0g=">inline blob</a></p>'
    )
    assert html_to_text(html) == "see the photo\ninline blob"


# -- the full agent auto-reply fixture against the acceptance criteria -----------

def test_agent_reply_fixture_readable_and_trimmed():
    body = html_to_text(_AGENT_HTML)
    lines = body.splitlines()

    # Readable body: tags stripped, line breaks kept, entities decoded.
    assert "Hi Tom," in lines
    assert "Open house: Saturday 11am\u201312pm" in lines
    assert "Call the listing agent: Jane Doe" in lines
    assert "Prefer a private inspection? O'Brien & Co can arrange weekday viewings too." in lines

    # Links preserved as text (url); url-as-label and mailto printed once.
    assert f"See the full listing here ({_LISTING_URL})" in lines
    assert body.count(_UNSUB) == 1
    assert body.count("jane.doe@example-estate.au") == 1

    # Noise trimmed: quoted reply, hidden preheader, style/script, pixel link.
    for noise in (
        "is this property still available",  # blockquote body
        "On Sat, 24 Oct 2026",  # gmail_quote header
        "You're receiving this because",  # display:none preheader
        "SECRETSTYLE",  # <style> content
        "SECRETSCRIPT",  # <script> content
        _PIXEL_HOST,  # tracking pixel src + wrapping <a href>
        "<",  # no tag fragments survive
    ):
        assert noise not in body, noise

    # Signature: divider trimmed, contact detail kept (it is lead info).
    assert "--" not in lines
    assert "Licensed Estate Agent, O'Brien & Co" in lines
    assert "D: 02 5550 1234 M: 0400 118 220" in lines
