"""File readers for knowledge ingestion. Supports text, PDF, PPTX, DOCX, MSG, HTML."""

import codecs
import html as html_lib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kiro_crew.bounded_read import bounded_read
from kiro_crew.pdf_extract import PDF_MAX_PAGES, extract_pdf_segments, pdfplumber_available
from kiro_crew.pinned_fs import open_fenced_for_read
from kiro_crew.security import is_sensitive_path

try:
    from pptx import Presentation  # type: ignore[import-untyped]
except ImportError:
    Presentation = None  # type: ignore[assignment,misc]

try:
    import html2text as _html2text_mod
except ImportError:
    _html2text_mod = None  # type: ignore[assignment]

# Opening or closing block-level tags (and <br>) in the regex HTML fallback.
_HTML_BLOCK_TAG_RE = re.compile(r'<(?:br|/?(?:p|div|li|h[1-6]|tr))\b[^>]*>', re.IGNORECASE)


def _decode_text_bytes(raw: bytes) -> str:
    """Decode raw file bytes as text.

    BOM-sniffed UTF-16 LE/BE first: Windows PowerShell tooling
    (New-ModuleManifest, the legacy ISE) writes UTF-16LE, whose bytes miss
    utf-8 and would land in the latin-1 fallback as BOM + interleaved NULs --
    mojibake in the index. The branch is extension-agnostic: any text format
    arriving as BOM'd UTF-16 decodes correctly. A BOM that lies (truncated
    copies, binary that happens to start FF FE) degrades to latin-1 rather
    than failing the file's ingest. Everything else keeps the historical
    utf-8 -> latin-1 chain, read as ``utf-8-sig`` so a UTF-8 BOM (Notepad,
    PowerShell ``-Encoding utf8``) is dropped rather than kept as a U+FEFF
    that hides a first-line ``# heading`` from the markdown chunker.
    Operating on one in-memory buffer (never
    reopening the path) keeps the sensitive-path check race-free, and the
    newline translation mirrors text-mode open() so results are unchanged.
    """
    if raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        try:
            text = raw.decode('utf-16')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _read_error(exc: Exception) -> tuple[str, dict]:
    """The reader result for a file that could not be read.

    ``format: 'error'`` is the sentinel ``IngestionPipeline.ingest_file`` tests:
    it raises rather than indexing, so the file is recorded as failed against its
    source and the surrounding scan continues. The returned text is diagnostic and
    never becomes an item.
    """
    return f'Error reading file: {exc}', {'format': 'error', 'error': str(exc)}


#: Characters of text one PDF may contribute to the index. Ingest has no request
#: deadline the way file-grep does, so the caps are the whole bound on what a
#: document can cost: text here, pages via ``PDF_MAX_PAGES``, wall time below,
#: and memory/CPU in the extractor child's rlimit profile.
_PDF_MAX_CHARS = 4_000_000
#: Wall-clock ceiling for one PDF's extraction. Generous next to the child's own
#: 60 CPU-second limit, since a document can also wait on I/O.
_PDF_WALL_SECS = 120.0


#: AsciiDoc ``include::target[attrs]`` directive, matched the way Asciidoctor's
#: preprocessor does: at the very start of a line, target without spaces at its
#: ends, attribute list closing the line. ``\include::`` (escaped) never matches.
_ADOC_INCLUDE_RE = re.compile(r'^include::([^\s\[](?:[^\[]*[^\s\[])?)\[(.*)\]$')
#: A target with a ``scheme:`` prefix is a URL or an Antora resource id
#: (``partial$x.adoc`` with a module, ``https://...``). Ingest is local-only, so
#: those directives stay as written rather than becoming a network fetch.
_ADOC_SCHEME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
#: Bounds on one document's include expansion. Depth stops deep chains, the
#: count stops fan-out (a partial included many times per level), and the
#: per-file and whole-document caps stop size blow-up.
_ADOC_MAX_INCLUDE_DEPTH = 8
_ADOC_MAX_INCLUDES = 256
_ADOC_MAX_INCLUDE_BYTES = 1_000_000
_ADOC_MAX_CHARS = 4_000_000


class _AdocIncludes:
    """Expand local ``include::`` directives into one AsciiDoc document's text.

    A target is read only when its ``realpath`` lies inside the top-level
    document's own directory (Asciidoctor's safe-mode jail) AND is a member of
    *include_paths*, the canonical set of files the folder scan selected for
    this source, each already passed through ``os.path.normcase`` by the scan.
    The set is used as given, never copied, so a read costs nothing per
    member; only the candidate path is normcased for the lookup. Membership is
    what carries every walk rule (``.kiroignore``,
    ``ignore_patterns``, ``include_extensions``, pruned and dot-named
    directories, ignored basenames, supported suffixes, ``is_sensitive_path``,
    ``confine_to_root``): a file the scan would not index is not a member, so
    it is not inlined either. No part below the root may be dot-named (the
    walk still selects a dot-named file such as ``.env``). The target must
    also be a regular file with no NUL byte that is not already on the include
    stack. The kernel's own path for the opened descriptor is judged again by
    the same jail, dot-name and membership rules, so a
    directory swapped for a symlink around the open cannot redirect the read;
    a hardlinked file is refused because its path says nothing about what it
    aliases. A directive that fails any check -- or names a URL, an absolute
    path, or an unresolved ``{attribute}`` -- stays in the text as written,
    which is what Asciidoctor itself leaves for an include it cannot resolve.
    Attributes such as ``tag=`` or ``lines=`` are not applied: the whole file
    is indexed. Every directive that reaches the filesystem counts toward
    ``_ADOC_MAX_INCLUDES``, failed or not, so a document cannot buy unbounded
    reads with directives that fail late. *included*, when given, receives the
    normcased real path of every target actually inlined, at every depth.
    """

    def __init__(self, root: str, include_paths: frozenset[str],
                 included: set[str] | None = None):
        self.root = root
        self.members = include_paths
        self.included = included
        self.opened: list[str] = []
        self.attempts = 0
        self.chars = 0

    def expand(self, text: str, current: str, stack: tuple[str, ...]) -> str:
        if 'include::' not in text:
            return text
        out = []
        for line in text.split('\n'):
            # Asciidoctor strips trailing whitespace before it looks for a directive.
            m = _ADOC_INCLUDE_RE.match(line.rstrip())
            if m is None:
                out.append(line)
                continue
            body = self._load(m.group(1), current, stack)
            out.append(line if body is None else body)
        return '\n'.join(out)

    def _load(self, target: str, current: str, stack: tuple[str, ...]) -> str | None:
        if ('\x00' in target or '{' in target or _ADOC_SCHEME_RE.match(target)
                or os.path.isabs(target)):
            return None
        if (len(stack) > _ADOC_MAX_INCLUDE_DEPTH or self.attempts >= _ADOC_MAX_INCLUDES
                or self.chars >= _ADOC_MAX_CHARS):
            return None
        self.attempts += 1
        real = os.path.realpath(os.path.join(os.path.dirname(current), target))
        if not self._allowed(real) or real in stack or not os.path.isfile(real):
            return None
        self.opened = [real]
        raw = self._read_pinned(real)
        if raw is None or b'\x00' in raw:
            return None
        body = _decode_text_bytes(raw).rstrip('\n')
        if self.chars + len(body) > _ADOC_MAX_CHARS:
            return None
        self.chars += len(body)
        if self.included is not None:
            # The kernel path the fence accepted, too: that is the file read.
            self.included.update(os.path.normcase(p) for p in self.opened)
        return self.expand(body, real, stack + (real,))

    def _allowed(self, real: str) -> bool:
        """Whether canonical *real* sits under the root and the scan selected it.

        A dot-named part below the root is refused as well: the walk prunes
        dot-named directories but still selects a dot-named file such as
        ``.env``, which an include must not pull into another document. Purely
        lexical: no name is resolved, so the verdict is about this exact string
        and nothing a filesystem change can re-point.
        """
        try:
            if os.path.commonpath([self.root, real]) != self.root:
                return False
        except ValueError:  # different drives on Windows
            return False
        rel = os.path.relpath(real, self.root)
        if any(part.startswith('.') for part in rel.split(os.sep)):
            return False
        return os.path.normcase(real) in self.members

    def _refuse_opened(self, kernel_path: str) -> bool:
        """Fence for the kernel's own path of an opened include.

        That path is already canonical, so it is judged as given, lexically,
        rather than resolved again: a second resolution would answer for
        whatever the name points at by then, not for the descriptor held.
        """
        if not self._allowed(kernel_path):
            return True
        self.opened.append(kernel_path)
        return False

    def _read_pinned(self, real: str) -> bytes | None:
        """Read *real* only if the file actually opened is inside the jail.

        ``open_fenced_for_read`` refuses a link at the final name and a
        hardlinked or non-regular file, then reads the kernel's own path for
        the opened descriptor; when that path differs from *real* it goes to
        ``_refuse_opened``, which judges that exact string. That path names the
        inode already held, so a parent directory swapped for a symlink at any
        moment cannot redirect the read.
        """
        try:
            fd = open_fenced_for_read(real, fence=self._refuse_opened)
        except OSError:
            return None
        try:
            f = os.fdopen(fd, 'rb')
        except OSError:
            os.close(fd)
            return None
        try:
            with f:
                if os.fstat(f.fileno()).st_size > _ADOC_MAX_INCLUDE_BYTES:
                    return None
                raw = bounded_read(f, _ADOC_MAX_INCLUDE_BYTES + 1)
        except OSError:
            return None
        if len(raw) > _ADOC_MAX_INCLUDE_BYTES:
            return None
        return raw


def _missing_dep(fmt: str, package: str) -> tuple[str, dict]:
    """The reader result when an optional extraction dependency is absent.

    Carries the same ``format: 'error'`` sentinel as :func:`_read_error`. ``.pptx``
    has no declared dependency, so this is the normal path for that format rather
    than an anomaly.
    """
    message = f'{fmt} support requires {package}: pip install {package}'
    return message, {'format': 'error', 'error': f'{fmt} support requires {package}'}


#: The MAPI string properties a .msg indexes, each a top-level
#: ``__substg1.0_<id><type>`` stream (type 001F UTF-16LE, 001E 8-bit).
_MSG_PROPS = {
    'subject': '0037', 'sender': '0C1A', 'smtp': '5D01', 'address': '0C1F',
    'address_type': '0C1E', 'to': '0E04', 'cc': '0E03', 'body': '1000',
}
#: PR_CLIENT_SUBMIT_TIME, else PR_MESSAGE_DELIVERY_TIME, in ``__properties_version1.0``.
_MSG_DATE_TAGS = (0x00390040, 0x0E060040)
#: PR_MESSAGE_CODEPAGE, else PR_INTERNET_CPID: the code page of the 8-bit strings.
_MSG_CODEPAGE_TAGS = (0x3FFD0003, 0x3FDE0003)
_MSG_PROPERTIES = '__properties_version1.0'
#: The OLE compound-file signature every Outlook .msg starts with.
_OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


def _msg_ole(olefile, fh):
    """``OleFileIO`` over *fh* that raises on every spec defect, minus its duplicate scan.

    ``DEFECT_INCORRECT`` stops any stream at the FAT, which olefile trims to the
    file's sectors; the skipped scan walks a list per directory entry, which is
    quadratic in entries on a crafted file.
    """
    ole = olefile.OleFileIO(raise_defects=olefile.DEFECT_INCORRECT)
    ole._check_duplicate_stream = lambda first_sect, minifat=False: None
    ole.open(fh)
    return ole


def _msg_text(ole, size: int) -> str:
    """Subject, From, Date, To and Cc lines, a blank line, then the plain-text body."""
    names = {}
    for key, prop in _MSG_PROPS.items():
        for kind in ('001F', '001E'):
            name = f'__substg1.0_{prop}{kind}'
            if ole.exists(name) and ole.get_size(name) > 0:
                names[key] = name
                break
    has_props = ole.exists(_MSG_PROPERTIES) and ole.get_size(_MSG_PROPERTIES) > 0
    read = [*names.values(), *([_MSG_PROPERTIES] if has_props else [])]
    # Streams occupy disjoint sectors, so more than the file means a looping FAT chain.
    if sum(ole.get_size(name) for name in read) > size:
        raise ValueError('malformed .msg: its streams are larger than the file')

    props = {}
    if has_props:
        raw = ole.openstream(_MSG_PROPERTIES).read()
        # A 32-byte header, then 16-byte entries: tag, flags, an 8-byte value.
        props = {
            int.from_bytes(raw[i:i + 4], 'little'): int.from_bytes(raw[i + 8:i + 16], 'little')
            for i in range(32, len(raw) - 15, 16)
        }
    # PT_LONG: the code page is the value's low 4 bytes.
    codepage = next((props[tag] for tag in _MSG_CODEPAGE_TAGS if props.get(tag)), 0) & 0xFFFFFFFF
    try:
        encoding = codecs.lookup(f'cp{codepage}').name if codepage else None
    except LookupError:
        encoding = None

    def value(key: str) -> str:
        name = names.get(key)
        if name is None:
            return ''
        raw = ole.openstream(name).read()
        if name.endswith('001F'):
            text = raw.decode('utf-16-le', 'replace')
        else:
            text = raw.decode(encoding, 'replace') if encoding else _decode_text_bytes(raw)
        return text.replace('\r\n', '\n').rstrip('\x00')

    date = ''
    filetime = next((props[tag] for tag in _MSG_DATE_TAGS if props.get(tag)), 0)
    if filetime:
        stamp = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=filetime // 10)
        date = stamp.strftime('%Y-%m-%d %H:%M:%S UTC')
    sender = value('sender').strip()
    address = value('smtp').strip()
    if not address and value('address_type').strip().upper() == 'SMTP':
        address = value('address').strip()
    if address and address != sender:
        sender = f'{sender} <{address}>' if sender else address
    fields = (('Subject', value('subject').strip()), ('From', sender), ('Date', date),
              ('To', value('to').strip()), ('Cc', value('cc').strip()))
    header = '\n'.join(f'{label}: {text}' for label, text in fields if text)
    return f'{header}\n\n{value("body")}'.strip()


class FileReader:
    # Binary formats need optional runtime deps: .pdf -> pdfplumber, .docx ->
    # python-docx and .msg -> olefile (all declared in setup.cfg). .pptx ->
    # python-pptx is NOT declared, so .pptx is intentionally kept out of
    # SUPPORTED even though _read_pptx exists.
    # Every source-code extension listed in ingestion.CODE_EXTS must appear here:
    # SUPPORTED is the folder-scan gate (folder_watcher._walk), and a source's
    # include_extensions can only narrow it, so an extension missing here is
    # silently skipped before any reader or chunker runs. test_knowledge.py pins
    # CODE_EXTS as a subset of this set.
    SUPPORTED = {
        '', '.md', '.txt', '.org', '.py', '.java', '.ts', '.js', '.rs', '.go',
        '.html', '.htm', '.docx', '.pdf', '.msg',
        # AsciiDoc reads as text; .asc stays out because it also names PGP armor.
        '.adoc', '.asciidoc',
        '.csv', '.log', '.json', '.jsonl', '.ndjson', '.yaml', '.yml',
        '.sh', '.rb', '.ps1', '.psm1', '.psd1', '.php', '.phtml', '.c', '.cpp', '.h',
        '.cs', '.kt', '.kts', '.swift', '.scala',
    }

    _DISPATCH = {
        '.pdf': '_read_pdf',
        '.pptx': '_read_pptx',
        '.docx': '_read_docx',
        '.msg': '_read_msg',
        '.html': '_read_html',
        '.htm': '_read_html',
        '.adoc': '_read_asciidoc',
        '.asciidoc': '_read_asciidoc',
    }

    def read(
        self, path: str, *, include_paths: frozenset[str] | None = None,
        included: set[str] | None = None,
    ) -> tuple[str, dict]:
        """Read *path*. *include_paths* and *included* reach only the AsciiDoc
        reader and every other reader ignores them; see :meth:`_read_asciidoc`."""
        if is_sensitive_path(path):
            raise PermissionError(f"Refusing to read sensitive path: {path}")
        p = Path(path)
        ext = p.suffix.lower()
        base_meta = {
            'format': ext.lstrip('.'),
            'title': p.stem,
            'file_size': os.path.getsize(path),
            'extension': ext,
        }
        method_name = self._DISPATCH.get(ext)
        if method_name == '_read_asciidoc':
            text, meta = self._read_asciidoc(path, include_paths, included)
        elif method_name:
            text, meta = getattr(self, method_name)(path)
        else:
            text, meta = self._read_text(path, ext.lstrip('.'))
        base_meta.update(meta)
        base_meta['line_count'] = text.count('\n') + 1 if text else 0
        return text, base_meta

    def _read_text(self, path: str, fmt: str) -> tuple[str, dict]:
        try:
            # One open, one read: decoding the buffer (rather than reopening
            # the path per encoding attempt) means the file that was
            # sensitive-path-checked is the file that gets indexed.
            with open(path, 'rb') as f:
                raw = f.read()
            return _decode_text_bytes(raw), {'format': fmt}
        except Exception as e:
            return _read_error(e)

    def _read_asciidoc(
        self, path: str, include_paths: frozenset[str] | None = None,
        included: set[str] | None = None,
    ) -> tuple[str, dict]:
        """Read AsciiDoc as text, with local ``include::`` directives expanded.

        Included content is indexed inside the document that includes it, so a
        search hit carries its surrounding context. Expansion runs only when
        *include_paths* is given -- the folder scan's canonical walk set, with
        every member already normcased -- and only into its members; see
        :class:`_AdocIncludes`. Every other caller (single-file sources,
        uploads, artifacts, agent docs, remote syncs) passes ``None`` and the
        directives stay as text. A document with no ``include::`` at all never
        touches the set. *included* receives the real path of each target
        inlined, so the scan can tell when one leaves the walk.
        """
        fmt = Path(path).suffix.lower().lstrip('.')
        text, meta = self._read_text(path, fmt)
        if (meta.get('format') == 'error' or include_paths is None
                or 'include::' not in text):
            return text, meta
        real = os.path.realpath(path)
        includes = _AdocIncludes(os.path.dirname(real), include_paths, included)
        includes.chars = len(text)
        return includes.expand(text, real, (real,)), meta

    def _read_pdf(self, path: str) -> tuple[str, dict]:
        """Extract a PDF through the memory-bounded child (``kiro_crew.pdf_extract``).

        The file handle is the child's stdin, so the bytes that were
        sensitive-path-checked are the bytes parsed, and never a copy through this
        process. A child stopped by its ceiling -- memory, CPU, or the wall clock
        -- is the same ``format: 'error'`` sentinel as any other unreadable file:
        ``IngestionPipeline.ingest_file`` records it against the source and the
        scan continues. Text cut at the character or page cap is indexed and
        marked ``truncated``.
        """
        if not pdfplumber_available():
            return _missing_dep('PDF', 'pdfplumber')
        try:
            with open(path, 'rb') as fh:
                outcome = extract_pdf_segments(
                    fh,
                    max_chars=_PDF_MAX_CHARS,
                    max_pages=PDF_MAX_PAGES,
                    deadline=time.monotonic() + _PDF_WALL_SECS,
                )
        except OSError as e:
            return _read_error(e)
        if outcome.failure is not None:
            return _read_error(RuntimeError(f'PDF extraction failed: {outcome.failure}'))
        meta: dict = {'format': 'pdf', 'page_count': outcome.pages}
        if outcome.truncated:
            meta['truncated'] = True
        return '\n'.join(text for _label, text in outcome.segments), meta

    def _read_pptx(self, path: str) -> tuple[str, dict]:
        if Presentation is None:
            return _missing_dep('PPTX', 'python-pptx')
        try:
            prs = Presentation(path)
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                title = ''
                body_parts = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        text = shape.text_frame.text.strip()
                        if shape == slide.shapes.title:
                            title = text
                        else:
                            body_parts.append(text)
                notes = ''
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                    notes = slide.notes_slide.notes_text_frame.text.strip()
                section = f'## Slide {i}: {title}\n{chr(10).join(body_parts)}'
                if notes:
                    section += f'\n{notes}'
                parts.append(section)
            return '\n\n'.join(parts), {'format': 'pptx', 'slide_count': len(prs.slides)}
        except Exception as e:
            return _read_error(e)

    def _read_docx(self, path: str) -> tuple[str, dict]:
        # Imported here, not at module scope: this module is on the gateway's
        # boot import chain and python-docx (+ lxml) costs ~75 ms to import,
        # which only a .docx read needs.
        try:
            from docx import Document  # type: ignore[import-untyped]
            from docx.oxml.ns import qn  # type: ignore[import-untyped]
            from docx.text.paragraph import Paragraph  # type: ignore[import-untyped]
        except ImportError:
            return _missing_dep('DOCX', 'python-docx')
        try:
            doc = Document(path)
            lines = []
            # Walk physical paragraphs in body order, including nested tables
            # and content-control (w:sdt / w:customXml) wrappers, whose subtree
            # holds ordinary block content Word emits from templates.
            # Grid-cell proxies repeat merged cells; revision wrappers stay out,
            # just as they do in python-docx's top-level paragraphs collection.
            containers = {
                qn('w:tbl'), qn('w:tr'), qn('w:tc'),
                qn('w:sdt'), qn('w:sdtContent'), qn('w:customXml'),
            }
            pending = list(reversed(doc.element.body))
            while pending:
                element = pending.pop()
                if element.tag in containers:
                    pending.extend(reversed(element))
                    continue
                if element.tag != qn('w:p'):
                    continue
                para = Paragraph(element, doc)
                style = para.style.name if para.style else ''
                text = para.text
                if style.startswith('Heading'):
                    try:
                        level = int(style.split()[-1])
                    except (ValueError, IndexError):
                        level = 1
                    # Markdown stops at six '#'. Word's Heading 7-9 map to
                    # level 6, so chunk_markdown still sees them as headings.
                    level = max(1, min(level, 6))
                    lines.append(f'{"#" * level} {text}')
                else:
                    lines.append(text)
            return '\n'.join(lines), {'format': 'docx', 'content_type': 'markdown', 'paragraph_count': len(doc.paragraphs)}
        except Exception as e:
            return _read_error(e)

    def _read_msg(self, path: str) -> tuple[str, dict]:
        """An Outlook .msg, an OLE compound file, parsed in this process by olefile.

        A .msg without the OLE signature is text (ROS message definitions, Tcl
        message catalogs) and reads as text. For the rest, work stays proportional
        to the file: the header's FAT sector count is checked before olefile loops
        over it, and :func:`_msg_ole` and :func:`_msg_text` bound the rest. One
        open serves the check and the parse.
        """
        # Imported here for the same boot import chain reason as python-docx.
        try:
            import olefile  # type: ignore[import-untyped]
        except ImportError:
            return _missing_dep('MSG', 'olefile')
        try:
            with open(path, 'rb') as fh:
                size = os.fstat(fh.fileno()).st_size
                header = fh.read(512)
                if not header.startswith(_OLE_MAGIC):
                    return _decode_text_bytes(header + fh.read()), {'format': 'msg'}
                fh.seek(0)
                # One FAT sector maps sector_size / 4 sectors of sector_size bytes.
                shift = int.from_bytes(header[0x1E:0x20], 'little')
                fat_sectors = int.from_bytes(header[0x2C:0x30], 'little')
                if shift not in (9, 12) or fat_sectors > size // (1 << (2 * shift - 2)) + 2:
                    return _read_error(ValueError('malformed .msg header'))
                with _msg_ole(olefile, fh) as ole:
                    return _msg_text(ole, size), {'format': 'msg'}
        except Exception as e:
            return _read_error(e)

    def _read_html(self, path: str) -> tuple[str, dict]:
        try:
            # Same single-open buffer decode as _read_text: HTML saved by
            # Windows tooling can arrive as BOM'd UTF-16 too.
            with open(path, 'rb') as f:
                html = _decode_text_bytes(f.read())
        except Exception as e:
            return _read_error(e)
        if _html2text_mod is not None:
            h = _html2text_mod.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            return h.handle(html), {'format': 'html'}
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        # Source whitespace, newlines included, renders as one space. A block
        # tag ends a line, marked with a NUL sentinel so that only block tags
        # (not source newlines or a decoded '&#10;') split the text.
        text = re.sub(r'[\s\x00]+', ' ', text)
        text = _HTML_BLOCK_TAG_RE.sub('\x00', text)
        text = re.sub(r'<[^>]+>', ' ', text)
        # Entities decode after tag stripping: a literal '&lt;div>' in the
        # page is text to index, never a tag to strip.
        text = html_lib.unescape(text)
        lines = (re.sub(r'\s+', ' ', line).strip() for line in text.split('\x00'))
        return '\n'.join(line for line in lines if line), {'format': 'html'}
