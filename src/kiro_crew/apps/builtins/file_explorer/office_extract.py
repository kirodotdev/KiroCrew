"""Structured content extraction from Office documents for the Files app.

Where :mod:`kiro_crew.doc_parser` flattens a document to plain text (for
search and attachment ingestion), this module preserves *structure* so the
Files viewer can render documents faithfully:

- ``.docx`` → ordered blocks (headings, paragraphs, tables)
- ``.xlsx`` → per-sheet 2-D value grids (shared strings and cached formula
  values resolved)
- ``.pptx`` → per-slide shapes with slide-relative positions, theme-resolved
  colours, run formatting (bold/italic/size), embedded image references,
  and simple tables — enough for a positioned slide-canvas rendering

Security posture mirrors ``doc_parser``: the XML comes from user-supplied
files, so parsing goes through defusedxml (never stdlib ``xml.etree``, which
resolves external entities — XXE), every ZIP member read is capped by its
*actual* decompressed size, and member counts are bounded.  Failures raise
:class:`OfficeExtractError` with an HTTP-ish status the caller can map.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

# NEVER fall back to stdlib xml.etree: it resolves external entities (XXE).
# Optional so a stale install degrades to "extraction unavailable" instead of
# breaking the whole backend at import time (same posture as doc_parser).
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ModuleNotFoundError:  # pragma: no cover — exercised via monkeypatch
    _xml_fromstring = None  # type: ignore[assignment]

# ── Limits ──

MAX_EXTRACT_MEMBER_BYTES = 30 * 1024 * 1024  # per ZIP member, decompressed
MAX_ZIP_MEMBERS = 4000
MAX_XLSX_ROWS = 500
MAX_XLSX_SHEETS = 10
MAX_PPTX_SLIDES = 100
MAX_DOCX_BLOCKS = 2000
XLSX_MAX_COLS = 16384  # Excel's own column limit (XFD)
MAX_XLSX_RENDER_COLS = 256  # widest row the viewer will materialize
MAX_XLSX_TOTAL_CELLS = 200_000  # workbook-wide cap across all sheets

OFFICE_EXTS: set[str] = {".docx", ".xlsx", ".pptx"}

_P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_SCHEME_ALIAS = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}


class OfficeExtractError(Exception):
    """Raised when a document cannot be safely extracted."""

    def __init__(self, msg: str, status: int = 415) -> None:
        super().__init__(msg)
        self.status = status


def _require_parser() -> Callable[[bytes], Any]:
    if _xml_fromstring is None:
        raise OfficeExtractError("defusedxml is not installed — Office extraction unavailable", 500)
    return _xml_fromstring


def _member_bytes(zf: zipfile.ZipFile, name: str) -> bytes | None:
    """Read one ZIP member, bounded by its ACTUAL decompressed size."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_EXTRACT_MEMBER_BYTES:
        raise OfficeExtractError("document member too large to extract", 413)
    with zf.open(info) as fh:
        data = fh.read(MAX_EXTRACT_MEMBER_BYTES + 1)
    if len(data) > MAX_EXTRACT_MEMBER_BYTES:
        raise OfficeExtractError("document member too large to extract", 413)
    return data


def _member_text(zf: zipfile.ZipFile, name: str) -> str | None:
    data = _member_bytes(zf, name)
    return None if data is None else data.decode("utf-8", errors="replace")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def extract_structured(path: Path, data: bytes | None = None) -> dict[str, Any]:
    """Extract structured content from *path* (dispatch by extension).

    The caller is responsible for path-safety checks (allow-list containment,
    sensitive-path denial) and any whole-file size cap — this module only
    guards the ZIP internals.  When *data* is given, THOSE bytes are parsed
    (the caller read them through its hardened gate) and the path is never
    reopened, so a file swapped after validation cannot be disclosed.
    """
    ext = path.suffix.lower()
    if ext not in OFFICE_EXTS:
        raise OfficeExtractError(f"extraction not supported for {ext or 'this file type'}")
    st = path.stat()
    src: Any = io.BytesIO(data) if data is not None else path
    try:
        with zipfile.ZipFile(src) as zf:
            if len(zf.namelist()) > MAX_ZIP_MEMBERS:
                raise OfficeExtractError("archive has too many members", 413)
            if ext == ".docx":
                out = _extract_docx(zf)
            elif ext == ".xlsx":
                out = _extract_xlsx(zf)
            else:
                out = _extract_pptx(zf)
    except zipfile.BadZipFile as exc:
        raise OfficeExtractError(f"not a readable Office file: {exc}") from exc
    except SyntaxError as exc:  # ElementTree.ParseError subclasses SyntaxError
        raise OfficeExtractError(f"malformed document XML: {exc}") from exc
    out["path"] = str(path)
    out["size"] = st.st_size
    out["mtime"] = int(st.st_mtime)
    return out


def media_member(path: Path, member: str, data: bytes | None = None) -> tuple[bytes, str]:
    """Return one embedded media member's bytes and its filename.

    Only the Office media folders are reachable; traversal is rejected.
    When *data* is given the already-gated bytes are parsed instead of
    reopening the path (see ``extract_structured``).
    """
    if path.suffix.lower() not in OFFICE_EXTS:
        raise OfficeExtractError("member streaming only for Office files")
    if not member.startswith(("ppt/media/", "word/media/", "xl/media/")) or ".." in member:
        raise OfficeExtractError("member not allowed", 403)
    src: Any = io.BytesIO(data) if data is not None else path
    try:
        with zipfile.ZipFile(src) as zf:
            data = _member_bytes(zf, member)
    except zipfile.BadZipFile as exc:
        raise OfficeExtractError(f"not a readable Office file: {exc}") from exc
    if data is None:
        raise OfficeExtractError(f"no such member: {member}", 404)
    return data, posixpath.basename(member)


# ── DOCX: ordered blocks ──


def _extract_docx(zf: zipfile.ZipFile) -> dict[str, Any]:
    """word/document.xml → paragraphs, headings, and tables (in order)."""
    parse = _require_parser()
    xml_text = _member_bytes(zf, "word/document.xml")
    if xml_text is None:
        raise OfficeExtractError("not a valid .docx (missing word/document.xml)")
    root = parse(xml_text)
    blocks: list[dict[str, Any]] = []

    def para_text(p_el: Any) -> str:
        return "".join(t.text or "" for t in p_el.iter() if _localname(t.tag) == "t")

    def para_style(p_el: Any) -> str:
        for el in p_el.iter():
            if _localname(el.tag) == "pStyle":
                for k, v in el.attrib.items():
                    if _localname(k) == "val":
                        return v or ""
        return ""

    body = next((el for el in root if _localname(el.tag) == "body"), None)
    if body is None:
        raise OfficeExtractError("empty .docx body")
    for el in body:
        name = _localname(el.tag)
        if name == "p":
            txt = para_text(el)
            style = para_style(el)
            kind = "p"
            if style.lower().startswith("heading"):
                lvl = "".join(ch for ch in style if ch.isdigit()) or "1"
                kind = f"h{min(int(lvl), 6)}"
            elif style.lower() == "title":
                kind = "h1"
            if txt.strip() or kind != "p":
                blocks.append({"type": kind, "text": txt})
        elif name == "tbl":
            rows: list[list[str]] = []
            for tr in el.iter():
                if _localname(tr.tag) != "tr":
                    continue
                cells = [
                    " ".join(para_text(p) for p in tc.iter() if _localname(p.tag) == "p").strip()
                    for tc in tr
                    if _localname(tc.tag) == "tc"
                ]
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append({"type": "table", "rows": rows})
        if len(blocks) >= MAX_DOCX_BLOCKS:
            blocks.append({"type": "p", "text": "… (truncated)"})
            break
    return {"kind": "docx", "blocks": blocks}


# ── XLSX: per-sheet value grids ──


def _xlsx_col_index(ref: str) -> int:
    """'BC12' → zero-based column index (54)."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(n - 1, 0)


def _extract_xlsx(zf: zipfile.ZipFile) -> dict[str, Any]:
    """Workbook sheets → capped 2-D value grids (cached formula values)."""
    parse = _require_parser()
    wb_xml = _member_bytes(zf, "xl/workbook.xml")
    if wb_xml is None:
        raise OfficeExtractError("not a valid .xlsx (missing xl/workbook.xml)")
    wb = parse(wb_xml)
    sheets_meta: list[tuple[str, str]] = []
    for el in wb.iter():
        if _localname(el.tag) == "sheet":
            rid = next((v for k, v in el.attrib.items() if _localname(k) == "id"), "")
            sheets_meta.append((el.attrib.get("name", "Sheet"), rid))
    rid_target: dict[str, str] = {}
    rels_xml = _member_bytes(zf, "xl/_rels/workbook.xml.rels")
    if rels_xml is not None:
        for el in parse(rels_xml).iter():
            if _localname(el.tag) == "Relationship":
                tgt = el.attrib.get("Target", "")
                tgt = tgt.lstrip("/") if tgt.startswith("/") else "xl/" + tgt
                rid_target[el.attrib.get("Id", "")] = tgt
    shared: list[str] = []
    ss_xml = _member_bytes(zf, "xl/sharedStrings.xml")
    if ss_xml is not None:
        for si in parse(ss_xml):
            if _localname(si.tag) == "si":
                shared.append("".join(t.text or "" for t in si.iter() if _localname(t.tag) == "t"))
    out_sheets: list[dict[str, Any]] = []
    total_cells = 0
    for idx, (name, rid) in enumerate(sheets_meta[:MAX_XLSX_SHEETS], 1):
        member = rid_target.get(rid) or f"xl/worksheets/sheet{idx}.xml"
        ws_xml = _member_bytes(zf, member)
        if ws_xml is None:
            continue
        rows: list[list[str]] = []
        truncated = False
        for row_el in parse(ws_xml).iter():
            if _localname(row_el.tag) != "row":
                continue
            if len(rows) >= MAX_XLSX_ROWS or total_cells >= MAX_XLSX_TOTAL_CELLS:
                truncated = True
                break
            cells: list[str] = []
            for c_el in row_el:
                if _localname(c_el.tag) != "c":
                    continue
                ref = c_el.attrib.get("r", "")
                col = _xlsx_col_index(ref) if ref else len(cells)
                if col >= MAX_XLSX_RENDER_COLS:
                    # A sparse far-right reference pads every cell up to its
                    # column; hundreds of such rows multiply into an OOM.
                    # The viewer materializes at most this many columns.
                    truncated = True
                    continue
                ctype = c_el.attrib.get("t", "n")
                val = ""
                if ctype == "inlineStr":
                    val = "".join(t.text or "" for t in c_el.iter() if _localname(t.tag) == "t")
                else:
                    for child in c_el:
                        if _localname(child.tag) == "v":
                            val = child.text or ""
                    if ctype == "s" and val != "":
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            pass
                    elif ctype == "b":
                        val = "TRUE" if val == "1" else "FALSE"
                while len(cells) < col:
                    cells.append("")
                cells.append(val)
            total_cells += len(cells)
            rows.append(cells)
        out_sheets.append({"name": name, "rows": rows, "truncated": truncated})
    return {"kind": "xlsx", "sheets": out_sheets}


# ── PPTX: positioned shapes with theme-resolved colours ──

_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def _extract_pptx(zf: zipfile.ZipFile) -> dict[str, Any]:  # noqa: C901
    """ppt/slides/slideN.xml → rich per-slide content.

    Emits positioned shapes (slide-relative percentages), theme-resolved run
    colours, formatting, embedded image references, and tables.  A flat
    ``lines`` list is kept per slide as a plain-text fallback for older or
    simpler consumers.
    """
    parse = _require_parser()

    def rels_of(member: str) -> dict[str, str]:
        d, b = posixpath.split(member)
        xml_text = _member_bytes(zf, posixpath.join(d, "_rels", b + ".rels"))
        out: dict[str, str] = {}
        if xml_text is None:
            return out
        for el in parse(xml_text).iter():
            if _localname(el.tag) == "Relationship":
                tgt = el.attrib.get("Target", "")
                if tgt.startswith("/"):
                    tgt = tgt.lstrip("/")
                else:
                    tgt = posixpath.normpath(posixpath.join(d, tgt))
                out[el.attrib.get("Id", "")] = tgt
        return out

    def parse_member(member: str) -> Any | None:
        data = _member_bytes(zf, member)
        return None if data is None else parse(data)

    # Theme colour scheme
    theme_clrs: dict[str, str] = {}
    theme_members = sorted(n for n in zf.namelist() if re.fullmatch(r"ppt/theme/theme\d+\.xml", n))
    if theme_members:
        troot = parse_member(theme_members[0])
        if troot is not None:
            for scheme in troot.iter(f"{_A_NS}clrScheme"):
                for slot in scheme:
                    for c in slot:
                        ln = _localname(c.tag)
                        if ln == "srgbClr":
                            theme_clrs[_localname(slot.tag)] = "#" + c.attrib.get("val", "000000")
                        elif ln == "sysClr":
                            theme_clrs[_localname(slot.tag)] = "#" + c.attrib.get(
                                "lastClr", "000000"
                            )
                break

    def _lum_adjust(hexc: str, mod: int | None, off: int | None) -> str:
        try:
            n = int(hexc.lstrip("#"), 16)
        except ValueError:
            return hexc
        r, g, b = (n >> 16) & 255, (n >> 8) & 255, n & 255
        mx, mn = max(r, g, b) / 255, min(r, g, b) / 255
        light = (mx + mn) / 2
        if mod is not None:
            light *= mod / 100000
        if off is not None:
            light += off / 100000
        light = max(0.0, min(1.0, light))
        cur = (mx + mn) / 2 or 1e-6
        k = light / cur
        r2, g2, b2 = (min(255, int(v * k)) for v in (r, g, b))
        return f"#{r2:02x}{g2:02x}{b2:02x}"

    def color_from(parent: Any) -> str | None:
        if parent is None:
            return None
        for c in parent:
            ln = _localname(c.tag)
            base = None
            if ln == "srgbClr":
                base = "#" + c.attrib.get("val", "000000")
            elif ln == "schemeClr":
                v = c.attrib.get("val", "")
                base = theme_clrs.get(_SCHEME_ALIAS.get(v, v))
            elif ln == "sysClr":
                base = "#" + c.attrib.get("lastClr", "000000")
            if base is None:
                continue
            mod = off = None
            for t in c:
                tn = _localname(t.tag)
                if tn == "lumMod":
                    mod = int(t.attrib.get("val", "100000"))
                elif tn == "lumOff":
                    off = int(t.attrib.get("val", "0"))
            if mod is not None or off is not None:
                base = _lum_adjust(base, mod, off)
            return base
        return None

    def fill_color(el: Any) -> str | None:
        if el is None:
            return None
        for c in el:
            ln = _localname(c.tag)
            if ln == "solidFill":
                return color_from(c)
            if ln == "gradFill":
                for gs in c.iter(f"{_A_NS}gs"):
                    return color_from(gs)
        return None

    # Slide geometry
    sld_cx, sld_cy = 12192000, 6858000
    proot = parse_member("ppt/presentation.xml")
    if proot is not None:
        sz = proot.find(f"{_P_NS}sldSz")
        if sz is not None:
            sld_cx = int(sz.attrib.get("cx", sld_cx))
            sld_cy = int(sz.attrib.get("cy", sld_cy))

    def xfrm_box(xfrm: Any, tf: Callable[..., tuple[int, int, int, int]] | None = None):
        if xfrm is None:
            return None
        o, e = xfrm.find(f"{_A_NS}off"), xfrm.find(f"{_A_NS}ext")
        if o is None or e is None:
            return None
        x, y = int(o.attrib.get("x", 0)), int(o.attrib.get("y", 0))
        w, h = int(e.attrib.get("cx", 0)), int(e.attrib.get("cy", 0))
        if tf:
            x, y, w, h = tf(x, y, w, h)
        return {
            "x": round(x / sld_cx * 100, 2),
            "y": round(y / sld_cy * 100, 2),
            "w": round(w / sld_cx * 100, 2),
            "h": round(h / sld_cy * 100, 2),
        }

    def ph_key(sp: Any) -> tuple[str, str] | None:
        for ph in sp.iter(f"{_P_NS}ph"):
            t = ph.attrib.get("type", "body")
            if t == "ctrTitle":
                t = "title"
            return (t, ph.attrib.get("idx", ""))
        return None

    layout_cache: dict[str, dict] = {}

    def placeholder_boxes(member: str) -> dict:
        """Placeholder key → inherited box, for a layout or base-slide member."""
        if member in layout_cache:
            return layout_cache[member]
        out: dict = {}
        root = parse_member(member)
        if root is not None:
            for sp in root.iter(f"{_P_NS}sp"):
                k = ph_key(sp)
                if not k:
                    continue
                sp_pr = sp.find(f"{_P_NS}spPr")
                box = xfrm_box(sp_pr.find(f"{_A_NS}xfrm") if sp_pr is not None else None)
                if box:
                    out.setdefault(k, box)
                    out.setdefault((k[0], ""), box)
        layout_cache[member] = out
        return out

    def paras_of(tx_body: Any) -> tuple[list[dict], list[str]]:
        paras: list[dict] = []
        lines: list[str] = []
        for p_el in tx_body.findall(f"{_A_NS}p"):
            p_pr = p_el.find(f"{_A_NS}pPr")
            algn = p_pr.attrib.get("algn", "l") if p_pr is not None else "l"
            lvl = int(p_pr.attrib.get("lvl", "0")) if p_pr is not None else 0
            bullet = False
            def_c = def_sz = None
            def_b = False
            if p_pr is not None:
                for c in p_pr:
                    ln = _localname(c.tag)
                    if ln in ("buChar", "buAutoNum"):
                        bullet = True
                    elif ln == "buNone":
                        bullet = False
                    elif ln == "defRPr":
                        def_sz = c.attrib.get("sz")
                        def_b = c.attrib.get("b") == "1"
                        for f in c:
                            if _localname(f.tag) == "solidFill":
                                def_c = color_from(f)
            runs: list[dict] = []
            text = ""
            for r_el in p_el:
                ln = _localname(r_el.tag)
                if ln == "br":
                    runs.append({"t": "\n"})
                    continue
                if ln not in ("r", "fld"):
                    continue
                t_el = r_el.find(f"{_A_NS}t")
                t = t_el.text if t_el is not None and t_el.text else ""
                if not t:
                    continue
                r_pr = r_el.find(f"{_A_NS}rPr")
                sz = c2 = None
                b = it = False
                if r_pr is not None:
                    sz = r_pr.attrib.get("sz")
                    b = r_pr.attrib.get("b") == "1"
                    it = r_pr.attrib.get("i") == "1"
                    for f in r_pr:
                        if _localname(f.tag) == "solidFill":
                            c2 = color_from(f)
                sz = sz or def_sz
                run: dict[str, Any] = {"t": t}
                if b or (def_b and r_pr is None):
                    run["b"] = True
                if it:
                    run["i"] = True
                if sz:
                    run["sz"] = round(int(sz) / 100, 1)
                if c2 or def_c:
                    run["c"] = c2 or def_c
                runs.append(run)
                text += t
                if len(runs) >= 60:
                    break
            paras.append({"algn": algn, "lvl": lvl, "bullet": bullet, "runs": runs})
            if text.strip():
                lines.append(text)
        return paras, lines

    def walk_shapes(
        container: Any,
        ph_boxes: dict,
        slide_rels: dict[str, str],
        tf: Callable[..., tuple[int, int, int, int]] | None = None,
    ) -> tuple[list[dict], list[str]]:
        shapes: list[dict] = []
        lines: list[str] = []
        for el in container:
            ln = _localname(el.tag)
            if ln == "sp":
                sp_pr = el.find(f"{_P_NS}spPr")
                box = xfrm_box(sp_pr.find(f"{_A_NS}xfrm") if sp_pr is not None else None, tf)
                if box is None:
                    k = ph_key(el)
                    if k:
                        box = ph_boxes.get(k) or ph_boxes.get((k[0], ""))
                fill = fill_color(sp_pr) if sp_pr is not None else None
                tx_body = el.find(f"{_P_NS}txBody")
                if tx_body is None:
                    if fill and box:
                        shapes.append({"kind": "text", "paras": [], "fill": fill, **box})
                    continue
                paras, ls = paras_of(tx_body)
                lines.extend(ls)
                sh: dict[str, Any] = {"kind": "text", "paras": paras}
                if fill:
                    sh["fill"] = fill
                if box:
                    sh.update(box)
                if paras or fill:
                    shapes.append(sh)
            elif ln == "pic":
                blip = el.find(f"{_P_NS}blipFill/{_A_NS}blip")
                rid = blip.attrib.get(f"{_R_NS}embed", "") if blip is not None else ""
                member = slide_rels.get(rid)
                sp_pr = el.find(f"{_P_NS}spPr")
                box = xfrm_box(sp_pr.find(f"{_A_NS}xfrm") if sp_pr is not None else None, tf)
                if member:
                    sh = {"kind": "image", "member": member}
                    if box:
                        sh.update(box)
                    shapes.append(sh)
            elif ln == "graphicFrame":
                box = xfrm_box(el.find(f"{_P_NS}xfrm"), tf)
                rows: list[list[str]] = []
                for tr in el.iter(f"{_A_NS}tr"):
                    cells = [
                        "".join(t.text or "" for t in tc.iter(f"{_A_NS}t")).strip()
                        for tc in tr.findall(f"{_A_NS}tc")
                    ]
                    if cells:
                        rows.append(cells)
                        lines.append(" | ".join(c for c in cells if c))
                if rows:
                    sh = {"kind": "table", "rows": rows[:60]}
                    if box:
                        sh.update(box)
                    shapes.append(sh)
            elif ln == "grpSp":
                g_pr = el.find(f"{_P_NS}grpSpPr")
                gx = g_pr.find(f"{_A_NS}xfrm") if g_pr is not None else None
                sub_tf = tf
                if gx is not None:
                    o = gx.find(f"{_A_NS}off")
                    e2 = gx.find(f"{_A_NS}ext")
                    co = gx.find(f"{_A_NS}chOff")
                    ce = gx.find(f"{_A_NS}chExt")
                    if o is not None and e2 is not None and co is not None and ce is not None:
                        ox, oy = int(o.attrib.get("x", 0)), int(o.attrib.get("y", 0))
                        ex, ey = int(e2.attrib.get("cx", 1)), int(e2.attrib.get("cy", 1))
                        cox = int(co.attrib.get("x", 0))
                        coy = int(co.attrib.get("y", 0))
                        cex = int(ce.attrib.get("cx", ex)) or ex or 1
                        cey = int(ce.attrib.get("cy", ey)) or ey or 1

                        def make_tf(
                            ox: int = ox,
                            oy: int = oy,
                            ex: int = ex,
                            ey: int = ey,
                            cox: int = cox,
                            coy: int = coy,
                            cex: int = cex,
                            cey: int = cey,
                            outer: Callable[..., tuple[int, int, int, int]] | None = tf,
                        ) -> Callable[..., tuple[int, int, int, int]]:
                            def _tf(x: int, y: int, w: int, h: int):
                                nx = ox + (x - cox) * ex // cex
                                ny = oy + (y - coy) * ey // cey
                                nw, nh = w * ex // cex, h * ey // cey
                                return outer(nx, ny, nw, nh) if outer else (nx, ny, nw, nh)

                            return _tf

                        sub_tf = make_tf()
                sub_shapes, sub_lines = walk_shapes(el, ph_boxes, slide_rels, sub_tf)
                shapes.extend(sub_shapes)
                lines.extend(sub_lines)
            if len(shapes) >= 80:
                break
        return shapes, lines

    def bg_of(root: Any) -> str | None:
        for bg in root.iter(f"{_P_NS}bg"):
            bg_pr = bg.find(f"{_P_NS}bgPr")
            if bg_pr is not None:
                c = fill_color(bg_pr)
                if c:
                    return c
            bg_ref = bg.find(f"{_P_NS}bgRef")
            if bg_ref is not None:
                return color_from(bg_ref)
        return None

    slide_names = sorted(
        (n for n in zf.namelist() if _SLIDE_RE.match(n)),
        key=lambda n: int(_SLIDE_RE.match(n).group(1)),  # type: ignore[union-attr]
    )
    if not slide_names:
        raise OfficeExtractError("not a valid .pptx (no slides)")

    slides: list[dict[str, Any]] = []
    for name in slide_names[:MAX_PPTX_SLIDES]:
        root = parse_member(name)
        if root is None:
            continue
        srels = rels_of(name)
        layout = next((t for t in srels.values() if "slideLayout" in t), None)
        ph_boxes: dict = {}
        base_slide = None
        if layout:
            ph_boxes = dict(placeholder_boxes(layout))
            lrels = rels_of(layout)
            # The OOXML relationship type's own spelling; cannot be renamed.
            base_slide = next(
                (t for t in lrels.values() if "slideMaster" in t), None  # wokeignore:rule=master
            )
            if base_slide:
                for k, v in placeholder_boxes(base_slide).items():
                    ph_boxes.setdefault(k, v)
        bg = bg_of(root)
        if bg is None and layout:
            lroot = parse_member(layout)
            bg = bg_of(lroot) if lroot is not None else None
        if bg is None and base_slide:
            mroot = parse_member(base_slide)
            bg = bg_of(mroot) if mroot is not None else None
        sp_tree = next(iter(root.iter(f"{_P_NS}spTree")), None)
        shapes, lines = walk_shapes(sp_tree, ph_boxes, srels) if sp_tree is not None else ([], [])
        num = int(_SLIDE_RE.match(name).group(1))  # type: ignore[union-attr]
        slides.append({"n": num, "bg": bg, "shapes": shapes, "lines": lines})
    return {
        "kind": "pptx",
        "slides": slides,
        "slideW": sld_cx,
        "slideH": sld_cy,
        "truncated": len(slide_names) > MAX_PPTX_SLIDES,
    }
