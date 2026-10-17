"""File readers for knowledge ingestion. Supports text, PDF, PPTX, DOCX, HTML."""

import codecs
import os
import re
import time
from pathlib import Path

from kiro_crew.pdf_extract import PDF_MAX_PAGES, extract_pdf_segments, pdfplumber_available
from kiro_crew.pinned_fs import open_fenced_for_read
from kiro_crew.security import is_sensitive_path

try:
    from pptx import Presentation  # type: ignore[import-untyped]
except ImportError:
    Presentation = None  # type: ignore[assignment,misc]

try:
    from docx import Document  # type: ignore[import-untyped]
    from docx.oxml.ns import qn  # type: ignore[import-untyped]
    from docx.text.paragraph import Paragraph  # type: ignore[import-untyped]
except ImportError:
    Document = None  # type: ignore[assignment,misc]

try:
    import html2text as _html2text_mod
except ImportError:
    _html2text_mod = None  # type: ignore[assignment]


def _decode_text_bytes(raw: bytes) -> str:
    """Decode raw file bytes as text.

    BOM-sniffed UTF-16 LE/BE first: Windows PowerShell tooling
    (New-ModuleManifest, the legacy ISE) writes UTF-16LE, whose bytes miss
    utf-8 and would land in the latin-1 fallback as BOM + interleaved NULs --
    mojibake in the index. The branch is extension-agnostic: any text format
    arriving as BOM'd UTF-16 decodes correctly. A BOM that lies (truncated
    copies, binary that happens to start FF FE) degrades to latin-1 rather
    than failing the file's ingest. Everything else keeps the historical
    utf-8 -> latin-1 chain. Operating on one in-memory buffer (never
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
            text = raw.decode('utf-8')
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
                raw = f.read(_ADOC_MAX_INCLUDE_BYTES + 1)
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


class FileReader:
    # Binary formats need optional runtime deps: .pdf -> pdfplumber and .docx ->
    # python-docx (both declared in setup.cfg). .pptx -> python-pptx is NOT declared,
    # so .pptx is intentionally kept out of SUPPORTED even though _read_pptx exists.
    # Every source-code extension listed in ingestion.CODE_EXTS must appear here:
    # SUPPORTED is the folder-scan gate (folder_watcher._walk), and a source's
    # include_extensions can only narrow it, so an extension missing here is
    # silently skipped before any reader or chunker runs. test_knowledge.py pins
    # CODE_EXTS as a subset of this set.
    SUPPORTED = {
        '', '.md', '.txt', '.org', '.py', '.java', '.ts', '.js', '.rs', '.go',
        '.html', '.htm', '.docx', '.pdf',
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
        if Document is None:
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
                    lines.append(f'{"#" * level} {text}')
                else:
                    lines.append(text)
            return '\n'.join(lines), {'format': 'docx', 'content_type': 'markdown', 'paragraph_count': len(doc.paragraphs)}
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
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text, {'format': 'html'}
