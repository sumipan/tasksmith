"""Convert plain task bodies (``tasksmith/v1``) and Asana ``html_notes``."""

from __future__ import annotations

import html
import re

from tasksmith import body as body_format
from tasksmith.language import SECTION_IDS, language
from tasksmith.store import TaskStoreError

MAX_HTML_CHARS = 60000


_BODY_RE = re.compile(r"^\s*<body>(.*)</body>\s*$", re.S)
_FORMAT_RE = re.compile(r"format:\s*(tasksmith/v\d+)")
_P_TAG_RE = re.compile(r"</?p\b", re.I)
_META_LI_RE = re.compile(r"<li>\s*([^<:]+):\s*([^<]*?)\s*</li>", re.I)
_LIST_SECTIONS = ("deliverables", "acceptance")

# ---------------------------------------------------------------------------
# エスケープ・リンク化
# ---------------------------------------------------------------------------


def escape_text(text: str) -> str:
    """Escape plain text for embedding in html_notes."""
    return html.escape(text or "", quote=False)


# URL は ASCII のみ。日本語の地の文（「。」「）」など）が直後に続いても URL に含めない。
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_URL_TRAILING_PUNCT = ".,;:!?'"
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.I | re.S)
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _trim_url(url: str) -> str:
    """末尾の句読点と、対応の取れない閉じ括弧を URL から外す。"""
    while url:
        last = url[-1]
        if last in _URL_TRAILING_PUNCT:
            url = url[:-1]
        elif last == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
        else:
            break
    return url


def linkify_text(text: str) -> str:
    """plain text を escape し、URL だけ <a href> にする。

    読み戻しは :func:`section_plain_text`（``<a>`` を URL 文字列に戻してから unescape）。
    """
    src = text or ""
    out: list[str] = []
    pos = 0
    for m in _URL_RE.finditer(src):
        url = _trim_url(m.group(0))
        if not url or "://" not in url:
            continue
        end = m.start() + len(url)
        out.append(escape_text(src[pos : m.start()]))
        out.append(f'<a href="{html.escape(url, quote=True)}">{escape_text(url)}</a>')
        pos = end
    out.append(escape_text(src[pos:]))
    return "".join(out)


def _unwrap_anchor(m: re.Match[str]) -> str:
    inner = m.group(2)
    href_m = _HREF_RE.search(m.group(1))
    if href_m is None:
        return inner
    href = href_m.group(1) if href_m.group(1) is not None else href_m.group(2)
    # 外部 URL は表示文字列 = href。Asana のメンション（表示がタスク名等）は href を残す
    if html.unescape(re.sub(r"<[^>]+>", "", inner)).strip() == html.unescape(href).strip():
        return inner
    return href


def section_plain_text(fragment: str | None) -> str:
    """節の HTML 断片を plain text に戻す（``<a>`` は URL 文字列、それ以外の文字は unescape）。

    ``<a>`` 以外のタグ（``<ul>`` / ``<li>`` 等）は ``html.unescape`` と同じく触らない。
    """
    return html.unescape(_ANCHOR_RE.sub(_unwrap_anchor, fragment or ""))


# ---------------------------------------------------------------------------
# 旧い版（nexus-cp/v1 / v2 / v3）と tasksmith/v1 の HTML の節の読み書き
# ---------------------------------------------------------------------------


def _h2_section_re() -> re.Pattern[str]:
    """Pattern of the v3 / tasksmith/v1 section headings (built from the installed pack)."""
    headings = language().headings
    return re.compile(
        r"<h2>(" + "|".join(re.escape(headings[sid]) for sid in SECTION_IDS) + r")</h2>",
    )


def serialize_html_sections(sections: dict[str, str]) -> str:
    headings = language().headings
    parts = ["<body>"]
    for sid in SECTION_IDS:
        parts.append(f"<h2>{headings[sid]}</h2>")
        fragment = sections.get(sid, "")
        if fragment:
            parts.append(fragment)
    parts.append("</body>")
    return "".join(parts)


def _split_by_heading_re(
    inner: str,
    heading_re: re.Pattern[str],
    allowed: tuple[tuple[tuple[str, ...], dict[str, str]], ...],
) -> dict[str, str] | None:
    """Split by heading; ``allowed`` pairs a heading tuple with its heading -> section id map."""
    headings = [h.strip() for h in heading_re.findall(inner)]
    id_by_heading: dict[str, str] | None = None
    for names, mapping in allowed:
        if tuple(headings) == names:
            id_by_heading = mapping
            break
    if id_by_heading is None:
        return None
    pieces = heading_re.split(inner)
    if pieces[0].strip():
        return None
    result: dict[str, str] = {}
    for i in range(1, len(pieces), 2):
        result[id_by_heading[pieces[i].strip()]] = pieces[i + 1].strip("\n")
    return result



def parse_html_sections(html_notes: str | None) -> dict[str, str] | None:
    """Split html_notes into section fragments for tasksmith/v1 only."""
    if not html_notes:
        return None
    m = _BODY_RE.match(html_notes)
    if not m:
        return None
    inner = m.group(1)
    headings = language().headings
    id_by_heading = {t: sid for sid, t in headings.items()}
    allowed = ((tuple(headings[sid] for sid in SECTION_IDS), id_by_heading),)
    v = _split_by_heading_re(inner, _h2_section_re(), allowed)
    if v is None:
        return None
    meta = v.get("control") or ""
    if not _FORMAT_RE.search(meta):
        return None
    if html_format_version_from_sections(v) != body_format.FORMAT_VERSION:
        return None
    return v


def html_format_version_from_sections(sections: dict[str, str]) -> str | None:
    meta = sections.get("control") or ""
    m = _FORMAT_RE.search(meta)
    return m.group(1) if m else None



def html_format_version(html_notes: str | None) -> str | None:
    sections = parse_html_sections(html_notes)
    if sections is None:
        return None
    return html_format_version_from_sections(sections)


def parse_html_meta_field(sections: dict[str, str], key: str) -> str | None:
    """制御メタデータの ``<li>key: value</li>`` から value を取り出す。"""
    meta = sections.get("control") or ""
    for m in _META_LI_RE.finditer(meta):
        if m.group(1).strip() == key:
            return m.group(2).strip() or None
    return None



# ---------------------------------------------------------------------------
# html_to_body
# ---------------------------------------------------------------------------

_BLOCK_RE = re.compile(
    r"(<h3>.*?</h3>|<pre>.*?</pre>|<ul>.*?</ul>|<ol>.*?</ol>)", re.S | re.I
)
_LI_RE = re.compile(r"<li>(.*?)</li>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def _inline_text(fragment: str) -> str:
    text = _ANCHOR_RE.sub(_unwrap_anchor, fragment or "")
    text = _BR_RE.sub("\n", text)
    return html.unescape(_TAG_RE.sub("", text))


def _fragment_to_text(fragment: str) -> str:
    pieces: list[str] = []
    for part in _BLOCK_RE.split(fragment or ""):
        if not part:
            continue
        head = part[:5].lower()
        if head.startswith("<h3>"):
            pieces.append("### " + _inline_text(part[4:-5]).strip())
        elif head.startswith("<pre>"):
            content = html.unescape(_TAG_RE.sub("", part[5:-6]))
            fence = body_format._fence_for(content)
            pieces.append(f"{fence}\n{content}\n{fence}")
        elif head.startswith(("<ul>", "<ol>")):
            items = [" ".join(_inline_text(x).split()) for x in _LI_RE.findall(part)]
            pieces.append("\n".join(f"- {x}" for x in items if x))
        else:
            text = _inline_text(part).strip("\n")
            if text.strip():
                pieces.append(text)
    return "\n".join(p for p in pieces if p)


def _meta_to_text(fragment: str) -> str:
    lines = [language().meta_tip]
    for li in _LI_RE.findall(fragment or ""):
        text = " ".join(_inline_text(li).split())
        m = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", text)
        if not m:
            continue  # 注意書きの行（版ごとに文言が違う）は捨てて、先頭の 1 行に揃える
        key, value = m.group(1).strip(), m.group(2).strip()
        if key == "format":
            value = body_format.FORMAT_VERSION
        lines.append(f"- {key}: {value}".rstrip())
    return "\n".join(lines)



def html_to_body(html_notes: str | None) -> str | None:
    """Map Asana html_notes to a tasksmith/v1 plain body, or None if unsupported."""
    if html_format_version(html_notes) != body_format.FORMAT_VERSION:
        return None
    assert html_notes is not None
    sections = parse_html_sections(html_notes)
    if sections is None:
        return None
    text_sections: dict[str, str] = {}
    for sid in SECTION_IDS:
        fragment = sections.get(sid, "")
        if sid == "control":
            text_sections[sid] = _meta_to_text(fragment)
        else:
            text_sections[sid] = _fragment_to_text(fragment)
    return body_format.serialize_sections(text_sections)


# ---------------------------------------------------------------------------
# body_to_html
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^(`{3,})")


def _meta_to_html(text: str) -> str:
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        item = line[2:].strip() if line.startswith("- ") else line
        items.append(f"<li>{escape_text(item)}</li>")
    return "<ul>" + "".join(items) + "</ul>" if items else ""


def _text_to_html(text: str, *, lists: bool) -> str:
    """節のテキストを HTML の断片にする。``lists`` なら ``- `` の連続を ``<ul>`` にする。"""
    pieces: list[tuple[str, str]] = []  # (kind, html)
    pending_text: list[str] = []
    pending_items: list[str] = []

    def flush_text() -> None:
        if pending_text:
            pieces.append(("text", linkify_text("\n".join(pending_text))))
            pending_text.clear()

    def flush_items() -> None:
        if pending_items:
            li = "".join(f"<li>{linkify_text(x)}</li>" for x in pending_items)
            pieces.append(("ul", f"<ul>{li}</ul>"))
            pending_items.clear()

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        fence_m = _FENCE_OPEN_RE.match(line)
        if fence_m:
            fence = fence_m.group(1)
            j = i + 1
            content: list[str] = []
            while j < len(lines) and not (
                lines[j].strip().startswith(fence) and set(lines[j].strip()) == {"`"}
            ):
                content.append(lines[j])
                j += 1
            flush_text()
            flush_items()
            pieces.append(("pre", f"<pre>{escape_text(chr(10).join(content))}</pre>"))
            i = j + 1
            continue
        if line.startswith("### "):
            flush_text()
            flush_items()
            pieces.append(("h3", f"<h3>{escape_text(line[4:].strip())}</h3>"))
        elif lists and line.startswith("- "):
            flush_text()
            pending_items.append(line[2:].strip())
        else:
            flush_items()
            pending_text.append(line)
        i += 1
    flush_text()
    flush_items()

    out: list[str] = []
    prev_kind = ""
    for kind, frag in pieces:
        if out and not (prev_kind == "h3" and kind == "pre"):
            out.append("\n")
        out.append(frag)
        prev_kind = kind
    return "".join(out)


def body_to_html(body: str) -> str:
    """本文のテキスト（固定フォーマット）を Asana の ``html_notes`` にする。

    ``<p>`` を含む、または 60,000 文字を超えるときは ``TaskStoreError``。
    """
    sections = body_format.parse_sections(body)
    if sections is None:
        raise ValueError(body_format._NOT_CONTROLLED)
    parts = ["<body>"]
    headings = language().headings
    for sid in SECTION_IDS:
        text = sections.get(sid, "")
        if sid == "control":
            fragment = _meta_to_html(text)
        else:
            fragment = _text_to_html(text, lists=sid in _LIST_SECTIONS)
        parts.append(f"<h2>{headings[sid]}</h2>")
        if fragment:
            parts.append(fragment)
    parts.append("</body>")
    result = "".join(parts)
    if _P_TAG_RE.search(result):
        raise TaskStoreError("body HTML contains forbidden <p> tags")
    if len(result) > MAX_HTML_CHARS:
        raise TaskStoreError(f"body HTML exceeds {MAX_HTML_CHARS} characters")
    return result
