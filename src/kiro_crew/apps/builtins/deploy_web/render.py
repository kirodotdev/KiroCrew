"""deploy-web render — turn an artifact into a standalone, web-ready index.html.

Artifacts aren't all web-ready (design §4.1 / Q1):
  - widget   → the inner ``<mcwidget>`` HTML needs a self-contained shell with
               a FIXED light/default theme inlined (no dashboard to inherit from).
  - markdown → render to HTML with a minimal built-in stylesheet.
  - html     → already a full doc; pass through (wrap only if it has no <html>).

v1 ships a single fixed light theme — no picker/parity. Widgets that rely on
runtime dashboard APIs won't work standalone (documented limitation).
"""
from __future__ import annotations

import html as _html
import re

# Fixed light theme — supplies the same theme CSS vars the dashboard iframe
# normally injects, so widgets that reference var(--bg)/var(--text)/etc. render.
_THEME_CSS = """
:root{--bg:#ffffff;--text:#1a1b26;--muted:#6b7280;--border:#e5e7eb;--card:#f9fafb;--accent:#7c3aed;}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--text);
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.55;}
a{color:var(--accent)}
pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:12px;overflow:auto}
code{background:var(--card);padding:1px 4px;border-radius:4px}
table{border-collapse:collapse}th,td{border:1px solid var(--border);padding:6px 10px}
img{max-width:100%}
""".strip()

_SHELL = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>{title}</title>\n<style>\n{css}\n</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
)

_HTML_DOC_RE = re.compile(r"<html[\s>]", re.IGNORECASE)


def _wrap(body: str, title: str) -> str:
    return _SHELL.format(title=_html.escape(title or "Published with KiroCrew"), css=_THEME_CSS, body=body)


def _render_markdown(md: str) -> str:
    """Minimal, dependency-free markdown → HTML (headings, code, bold/italic, lists, paragraphs).

    Escapes HTML first (untrusted-by-default), then re-introduces a safe subset.
    """
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    in_list = False
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + " ".join(para) + "</p>")
            para.clear()

    def inline(s: str) -> str:
        s = _html.escape(s)
        # Extract code spans into placeholders first so bold/italic/link
        # transforms below can't rewrite content inside <code> (e.g. `**x**`).
        placeholders: list[str] = []

        def _code_placeholder(m: "re.Match[str]") -> str:
            placeholders.append(f"<code>{m.group(1)}</code>")
            return f"\x00{len(placeholders) - 1}\x00"

        s = re.sub(r"`([^`]+)`", _code_placeholder, s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                   lambda m: f'<a href="{_html.escape(_html.unescape(m.group(2)))}">{m.group(1)}</a>', s)
        for i, ph in enumerate(placeholders):
            s = s.replace(f"\x00{i}\x00", ph)
        return s

    for raw in lines:
        if raw.strip().startswith("```"):
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            if not in_code:
                out.append("<pre><code>")
                in_code = True
            else:
                out.append("</code></pre>")
                in_code = False
            continue
        if in_code:
            out.append(_html.escape(raw))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if m:
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            continue
        lm = re.match(r"^\s*[-*]\s+(.*)$", raw)
        if lm:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(lm.group(1))}</li>")
            continue
        if not raw.strip():
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        para.append(inline(raw))

    flush_para()
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def render_standalone(kind: str, content: str, *, title: str = "") -> str:
    """Return a standalone HTML document for the given artifact kind.

    kind ∈ {widget, markdown, html}. Always self-contained; intended to be
    written as ``index.html``.
    """
    k = (kind or "").lower()
    if k == "html":
        if _HTML_DOC_RE.search(content or ""):
            return content  # already a full document
        return _wrap(content or "", title)
    if k in ("markdown", "md"):
        return _wrap(_render_markdown(content or ""), title)
    # widget (default): inner mcwidget body → wrap in the themed shell
    return _wrap(content or "", title)
