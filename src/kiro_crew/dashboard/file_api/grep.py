"""The content search engines behind ``POST /api/file-grep``: ripgrep, the python walk and the document pass."""

from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _GREP_DOC_EXTS,
        _GREP_DOC_MAX_BYTES,
        _GREP_DOC_MAX_CHARS,
        _GREP_LABEL_CHARS,
        _GREP_MAX_DIRS_VISITED,
        _GREP_MAX_FILE_BYTES,
        _GREP_MAX_RESULTS,
        _GREP_PPTX_SLIDE_RE,
        _GREP_PREVIEW_CHARS,
        _GREP_RG_MAX_RECORD_BYTES,
        _GREP_RG_OVERSIZE,
        _GREP_RG_POLL_SECS,
        _GREP_RG_QUEUE_LINES,
        _GREP_RG_TEARDOWN_SECS,
        _GREP_ROW_DEADLINE_STRIDE,
        _SHEET_MAX_EXPANDED_BYTES,
        _SHEET_MAX_MEMBERS,
        _WALK_SKIP_DIRS,
        FileTooLargeError,
        PdfExtraction,
        ZipInventoryRejected,
        _DocSegments,
        _validate_dashboard_path,
        cgroup_scope_argv,
        extract_pdf_segments,
        extract_text,
        is_sensitive_path,
        logger,
        platform_compat,
        popen_limited,
        redact,
        redact_credentials,
        redact_exfiltration_urls,
        redact_path_segments,
        safe_read_prefix,
        sandbox_credential_targets,
        validate_provider_executable,
        vet_zip_inventory_bytes,
        wrap_argv,
    )


def _grep_resolve_root(raw: str) -> tuple[str, bool]:
    """Validate and canonicalize the search root; say whether it is a directory.

    Blocking (``realpath``, ``isdir``, the sensitive-path fence) -- reached only
    through :func:`_run_path_probe`. ``is_sensitive_path`` belongs off the loop
    too: one call resolves the path and walks its ancestors, tens of syscalls on
    whatever mount the caller named.

    An empty path means REFUSED, for either reason -- the validator rejected the
    name, or it is a credential store. The endpoint answers both with the same
    403, so nothing downstream needs to tell them apart. A 403 rather than a 404
    because "not found" would invite probing for the allowed spelling.
    """
    validated = _validate_dashboard_path(raw)
    if validated is None:
        return "", False
    root = os.path.realpath(os.path.expanduser(validated))
    if is_sensitive_path(root):
        return "", False
    return root, os.path.isdir(root)


def _grep_sensitive_globs(root: str) -> list[str]:
    """ripgrep exclusions for the credential stores ``is_sensitive_path`` fences.

    An optimisation (do not read those bytes at all); the authority stays the
    per-hit ``is_sensitive_path`` filter both engines apply. DERIVED from
    :func:`kiro_crew.security.sandbox_credential_targets`, never a second list:
    that function already includes the env-override re-anchors
    (``KIROCREW_HOME``, ``CLAUDE_CONFIG_DIR``, ...), which a ``$HOME`` projection
    here missed, so ripgrep read a relocated store.

    Each target is emitted only when it lies inside *root*, as a root-ANCHORED
    glob. ``is_sensitive_path`` is HOME-anchored -- ``~/.npmrc`` is a store, a
    project's own ``.npmrc`` is an ordinary file the python walk searches -- and an
    unanchored ``!**/.npmrc`` made the rg host quietly return less.
    """
    args: list[str] = []
    root_real = os.path.realpath(root)
    # Should ``sandbox_credential_targets`` raise ``PathResolutionStalled`` (a
    # ``RuntimeError``: the roots could not be canonicalised), it is deliberately
    # NOT caught here. An empty exclusion list would let ripgrep read the stores
    # before the per-hit filter sees them; letting it propagate reaches
    # ``_grep_rg``'s ``RuntimeError`` catch, which returns ``None`` and routes the
    # search to the fail-closed python engine instead.
    for target in sandbox_credential_targets():
        if not target:
            continue
        absolute = os.path.realpath(os.path.expanduser(target))
        try:
            inside = os.path.relpath(absolute, root_real)
        except ValueError:
            continue  # different drives on Windows: not inside
        if inside == os.curdir or inside.startswith(os.pardir):
            continue
        # The LEADING slash anchors: under gitignore semantics a slash-less
        # pattern matches a basename at any depth, so bare `!.ssh` would hide a
        # project's `.ssh` several levels down. Forward slashes on every platform;
        # ripgrep does not read a Windows `relpath`'s backslashes as separators.
        # `--iglob` because ripgrep globs are case-sensitive and `.AWS` is the
        # same directory on a case-insensitive volume.
        args += ["--iglob", "!/" + PurePath(inside).as_posix()]
    return args


def _grep_hit(path: str, line: int, preview: str, label: str = "") -> dict:
    """One result row.

    ``label`` names the location INSIDE a document; a text hit has none.
    Deliberately no column: the rail finds the match in ``preview`` itself, and
    ripgrep's column is a BYTE offset into a preview that is decoded text.

    EVERY string here is REDACTED, at the one chokepoint every hit passes
    through. The preview is the matching LINE, so a file with an API key on it
    puts that key in the response for a file the user never asked to open; the
    label is author-chosen document content; a PATH segment can itself be
    credential-shaped, which is why the file API's listings redact paths too. The
    sensitive-path fence covers credential STORES, not a secret pasted into an
    ordinary file. Redaction runs BEFORE the cut: half a token matches no pattern.
    """
    safe, _ = redact_credentials(preview.rstrip("\n"))
    safe, _ = redact_exfiltration_urls(safe)
    hit: dict = {
        # Segment-wise so two paths that both redact to a tag stay two rows; a
        # clean path is returned byte-for-byte.
        "file": redact_path_segments(path, redact),
        "line": line,
        "preview": safe[:_GREP_PREVIEW_CHARS],
    }
    if label:
        safe_label, _ = redact_credentials(label)
        safe_label, _ = redact_exfiltration_urls(safe_label)
        hit["label"] = safe_label[:_GREP_LABEL_CHARS]
    return hit


def _grep_rg_executable() -> str | None:
    """The ripgrep this endpoint may run, as an absolute path, or None.

    The gateway's ``$PATH`` can include trees the agent writes -- the project
    checkout, the workspace root -- and an ``rg`` planted there would run with
    the gateway's environment on the user's next keystroke.
    :func:`validate_provider_executable` is the repo's executable-provenance
    chokepoint (agent-writable containment, ownership, world-writability along
    the whole parent chain, symlinks, Windows ACLs, the operator's strict mode);
    a subset re-spelled here left the parent chain unchecked. ``rg`` is handed
    no credentials, so the default relaxed policy applies and a user-owned
    Homebrew install works. A refusal is not an error: the python engine
    answers the same question more slowly.

    Blocking (``which`` and the provenance walk both stat) -- transfer pool only.
    """
    found = shutil.which("rg")
    if not found:
        return None
    try:
        return validate_provider_executable(found)
    except ValueError as exc:
        logger.warning("file_grep: refusing rg at %s: %s", found, exc)
        return None


def _grep_rg_argv(root: str, executable: str = "rg") -> list[str]:
    """The ``rg`` argv, built so ripgrep answers the SAME question the fallback does.

    ``--fixed-strings``: both python passes match ``re.escape(query)``, so
    ``config(`` is a search, not a regex parse error. ``--ignore-case`` rather
    than ``--smart-case``: both python passes fold case unconditionally.
    ``--no-ignore``: ``os.walk`` cannot honour ignore files, so ripgrep must not
    either; the noisy directories are pruned by ``_WALK_SKIP_DIRS`` on both
    sides. ``--max-count 1`` and ``--max-filesize`` match the fallback's
    one-hit-per-file rule and its size ceiling.

    *executable* is the absolute path :func:`_grep_rg_executable` vetted; the
    bare name is a default for tests.
    """
    cmd = [
        executable,
        # A config file (RIPGREP_CONFIG_PATH, inherited by the child) can carry
        # `--pre=<binary>`, which runs an arbitrary executable, or a `--smart-case`
        # that breaks the parity above.
        "--no-config",
        "--json",
        "--fixed-strings",
        "--ignore-case",
        "--no-ignore",
        "--max-count",
        "1",
        "--max-filesize",
        str(_GREP_MAX_FILE_BYTES),
        "--no-messages",
        "--hidden",
    ]
    # Case-SENSITIVE, matching the python walk's exact-name directory screen:
    # `--iglob` here would make ripgrep skip a `Node_Modules` the fallback still
    # descends into.
    for ignored in sorted(_WALK_SKIP_DIRS):
        cmd += ["--glob", f"!**/{ignored}"]
    # The document pass owns these and matches `splitext(name)[1].lower()`, so
    # `--iglob`: a case-sensitive glob left `REPORT.DOCX` to ripgrep's binary
    # heuristics and the file came back twice.
    for ext in sorted(_GREP_DOC_EXTS):
        cmd += ["--iglob", f"!**/*{ext}"]
    cmd += _grep_sensitive_globs(root)
    # The query is NOT in the argv: a child's arguments are readable by every
    # account on the host through `/proc/<pid>/cmdline`, and this handler treats
    # the query as secret-class text (it redacts it before every SEL write).
    # `--file -` reads the pattern from stdin. A query starting with `-` also
    # cannot be read as a flag when it is not on the command line at all.
    cmd += ["--file", "-"]
    # Every glob above is NEGATED. One non-negated glob flips ripgrep's glob set
    # into allowlist mode and silently excludes every file it does not name.
    cmd += ["--", root]
    return cmd


def _grep_rg_teardown(
    proc: "subprocess.Popen[str]",
    reader: "threading.Thread | None",
    lines: "queue.Queue[str | None] | None",
) -> None:
    """Stop the child and let the reader thread finish, on every exit path.

    A killed child that is never waited on is a zombie for the life of the
    gateway, and one search runs per keystroke. The reader blocks on a FULL
    queue, so once this side stops consuming its ``put`` never returns and the
    thread wedges: closing the pipe ends its iteration, draining lets a waiting
    ``put`` proceed, and both are needed.
    """
    if proc.poll() is None:
        try:
            platform_compat.kill_process_tree(proc.pid)
        except (ProcessLookupError, OSError):
            pass  # exited between the poll and the signal; this is a `finally`
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
    if reader is not None and lines is not None:
        limit = time.monotonic() + _GREP_RG_TEARDOWN_SECS
        while reader.is_alive() and time.monotonic() < limit:
            try:
                lines.get_nowait()
            except queue.Empty:
                reader.join(0.02)
        if reader.is_alive():
            logger.warning("file_grep: rg reader thread did not exit")
    try:
        proc.wait(timeout=_GREP_RG_TEARDOWN_SECS)
    except subprocess.TimeoutExpired:
        logger.warning("file_grep: rg did not exit after kill; not reaped")
    except (ProcessLookupError, OSError):
        pass  # already reaped; raising here would cost the caller its answer


def _grep_rg_hit_of(record_line: str) -> dict | None:
    """One ``rg --json`` line as a hit, or None when it is not a reportable match."""
    try:
        record = json.loads(record_line)
    except ValueError:
        return None
    if record.get("type") != "match":
        return None
    data = record.get("data") or {}
    path = (data.get("path") or {}).get("text") or ""
    # The authoritative gate; the argv's globs only save ripgrep the read.
    if not path or is_sensitive_path(path):
        return None
    text = (data.get("lines") or {}).get("text") or ""
    return _grep_hit(path, int(data.get("line_number", 0)), text)


def _grep_rg(root: str, query: str, deadline: float) -> tuple[list[dict], bool] | None:
    """Text pass through ``rg --json``, or None when ripgrep produced no verdict.

    None is the "fall back to python" signal: a missing binary, a failed spawn
    and an error exit (>1; 1 is "no matches") with no hits printed are all "no
    verdict", and an empty list would tell the user "no matches" about a search
    that never ran. A TIMEOUT is NOT a fallback, and neither is an error exit
    after hits were printed: the deadline is shared, so the python pass would
    have nothing left. The hits already parsed are returned, marked truncated.

    Records are read INCREMENTALLY and the child is stopped at
    ``_GREP_MAX_RESULTS``. Buffering the whole output would bound memory only by
    how many files match: neither ``--max-count`` nor ``--max-filesize`` bounds
    the COUNT of records.
    """
    executable = _grep_rg_executable()
    if executable is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return [], True
    out: list[dict] = []
    cleanup: str | None = None
    proc: "subprocess.Popen[str] | None" = None
    reader: "threading.Thread | None" = None
    lines: "queue.Queue[str | None] | None" = None
    oversize = 0
    try:
        wrapped, cleanup = wrap_argv(_grep_rg_argv(root, executable))
        proc = popen_limited(
            cgroup_scope_argv(wrapped),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            # `rg --json` is UTF-8 by definition; the host locale would corrupt
            # both the path and the preview.
            encoding="utf-8",
            errors="replace",
        )
        # One write and a close cannot block: the query is capped far inside a
        # pipe buffer, and rg starts searching at EOF. The handler refuses a
        # newline in the query -- `--file` is line-delimited, so two lines would
        # be two patterns OR-ed together where the fallback matches one literal.
        if proc.stdin is not None:
            try:
                proc.stdin.write(query + "\n")
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # rg exited before reading; the error-exit branch falls back
        assert proc.stdout is not None
        # A thread because the read BLOCKS: rg prints nothing for a non-matching
        # file, so a rare query over a large tree is silent for its whole
        # traversal and an inline read could not observe the deadline. Not
        # select/poll: those do not accept pipe handles on Windows. Daemon so a
        # given-up wait cannot keep the interpreter alive.
        lines = queue.Queue(maxsize=_GREP_RG_QUEUE_LINES)
        queued = lines
        stdout = proc.stdout

        def _drain() -> None:
            try:
                for line in stdout:
                    if len(line) > _GREP_RG_MAX_RECORD_BYTES:
                        queued.put(_GREP_RG_OVERSIZE)  # reported short, not dropped
                        continue
                    queued.put(line)
            except Exception:  # the pipe closing under a kill is expected
                pass
            finally:
                # Non-blocking: teardown may have stopped consuming.
                try:
                    queued.put_nowait(None)
                except queue.Full:
                    pass

        reader = threading.Thread(target=_drain, name="file-grep-rg", daemon=True)
        reader.start()
        capped = False
        while True:
            if len(out) >= _GREP_MAX_RESULTS:
                capped = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                capped = True
                break
            try:
                record_line = lines.get(timeout=min(remaining, _GREP_RG_POLL_SECS))
            except queue.Empty:
                # The sentinel put is non-blocking, so a full queue at that
                # instant DROPS it. A dead reader with an empty queue is therefore
                # a COMPLETE search, not a cap; otherwise rg has not spoken yet
                # and the `remaining <= 0` check above ends the wait.
                if not reader.is_alive() and lines.empty():
                    break
                continue
            if record_line is None:
                break
            if record_line == _GREP_RG_OVERSIZE:
                oversize += 1
                continue
            hit = _grep_rg_hit_of(record_line)
            if hit is not None:
                out.append(hit)
        if capped:
            return out, True  # the `finally` kills, drains and reaps
        code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return out, True
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        # RuntimeError is the fail-closed sandbox refusal: a host with no backend
        # takes the python engine rather than a 500 per keystroke. Same catch as
        # `_run_git_bounded`.
        logger.warning("file_grep: rg did not answer; falling back to python", exc_info=True)
        return None
    finally:
        if proc is not None:
            _grep_rg_teardown(proc, reader, lines)
        if cleanup:
            try:
                os.unlink(cleanup)  # `wrap_argv`'s launcher temp file is ours
            except OSError:
                pass
    if code > 1:
        # An error exit AFTER hits were printed is a partial answer, not a missing
        # one: ripgrep exits 2 for an unreadable directory even under
        # `--no-messages`, and the python engine would start on the same spent
        # deadline. Only an error exit with NOTHING printed takes the fallback.
        if out:
            logger.warning("file_grep: rg exited %s after %s hit(s); partial", code, len(out))
            return out, True
        logger.warning("file_grep: rg exited %s; falling back to python", code)
        return None
    if oversize:
        logger.warning("file_grep: skipped %s oversize rg record(s)", oversize)
    return out, bool(oversize)


def _grep_python(root: str, query: str, deadline: float) -> tuple[list[dict], bool]:
    """Text pass without ripgrep. Same answer shape, same one-hit-per-file rule.

    The explicit ``is_sensitive_path`` call is what makes the two engines
    visibly symmetric; :func:`kiro_crew.hooks.safe_read_prefix` is the authority
    behind it (it re-resolves through ``realpath``) and bounds the bytes read.
    """
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    out: list[dict] = []
    dirs_visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() >= deadline:
            return out, True
        dirs_visited += 1
        if dirs_visited > _GREP_MAX_DIRS_VISITED:
            return out, True
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _WALK_SKIP_DIRS and not is_sensitive_path(os.path.join(dirpath, d))
        ]
        for name in sorted(filenames):
            if len(out) >= _GREP_MAX_RESULTS:
                return out, True
            if time.monotonic() >= deadline:
                return out, True
            if os.path.splitext(name)[1].lower() in _GREP_DOC_EXTS:
                continue  # the document pass owns these
            full = os.path.join(dirpath, name)
            # ripgrep does not follow symlinks while traversing, so neither does
            # this walk -- and a link can point outside the root the caller named.
            if os.path.islink(full):
                continue
            if is_sensitive_path(full):
                continue
            try:
                raw = safe_read_prefix(full, _GREP_MAX_FILE_BYTES + 1)
            except (OSError, FileTooLargeError):
                continue
            # A NUL anywhere, not only in a header sniff: ripgrep considers the
            # whole file binary at its first NUL, wherever that is.
            if not raw or b"\x00" in raw:
                continue  # refused, empty, or binary
            if len(raw) > _GREP_MAX_FILE_BYTES:
                continue  # over the ceiling: ripgrep's --max-filesize skips it too
            for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    out.append(_grep_hit(full, number, line))
                    break
    return out, False


def _grep_xlsx_segments(data: bytes, path: str, deadline: float) -> _DocSegments:
    """One workbook as ``("Sheet1 · row 12", row text)`` segments.

    Two gates run BEFORE openpyxl sees the bytes, because it opens the container
    itself: the shared inventory vet bounds the declared member count, and the
    ``infolist()`` sum bounds expansion, which the first does not. Both are the
    sheet endpoint's own ceilings, not a second spelling of them. Neither bounds
    TIME, and the character cap cannot see an EMPTY row, so the row loop also
    watches the deadline: a workbook of millions of empty rows is one small
    member that yields no text.
    """
    try:
        vet_zip_inventory_bytes(data, max_members=_SHEET_MAX_MEMBERS)
    except ZipInventoryRejected:
        return (), True  # refused by policy: a settled answer, not a short read
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as probe:
            if sum(i.file_size for i in probe.infolist()) > _SHEET_MAX_EXPANDED_BYTES:
                logger.warning("file_grep: workbook %s expands too large; skipped", path)
                return (), True
    except (OSError, zipfile.BadZipFile):
        return (), True
    # Imported only past both ceilings: ~100ms of parser setup that a refusal
    # should not pay for.
    try:
        import openpyxl
    except ImportError:
        return (), True
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        logger.warning("file_grep: cannot read workbook %s", path, exc_info=True)
        return (), True
    segments: list[tuple[str, str]] = []
    budget = _GREP_DOC_MAX_CHARS
    whole = True
    try:
        for sheet in book.worksheets:
            for index, row in enumerate(sheet.iter_rows(values_only=True), 1):
                if index % _GREP_ROW_DEADLINE_STRIDE == 0 and time.monotonic() >= deadline:
                    return tuple(segments), False
                # The row is assembled INSIDE the remaining budget, never joined
                # first and capped after: openpyxl hands back the same shared
                # string for every cell that references it, so a wide row is N
                # references until a join makes it N copies -- and the expansion
                # gate counts the string once, as one zip member.
                cells: list[str] = []
                for cell in row:
                    if cell is None:
                        continue
                    piece = str(cell)
                    if len(piece) >= budget:
                        cells.append(piece[:budget])
                        budget = 0
                        break
                    cells.append(piece)
                    budget -= len(piece) + 1  # the tab that joins it
                if not cells:
                    continue
                segments.append((f"{sheet.title} · row {index}", "\t".join(cells)))
                if budget <= 0:
                    return tuple(segments), False  # a match past the cap is hidden
    except Exception:
        # The rows already collected are real, but the reader did not see the rest.
        logger.warning("file_grep: workbook %s failed mid-read", path, exc_info=True)
        whole = False
    finally:
        book.close()
    return tuple(segments), whole


def _grep_pdf_segments(data: bytes, path: str, deadline: float) -> PdfExtraction:
    """Extract a PDF in the bounded child; the caller reads ``failure`` itself.

    Returned rather than folded into ``_DocSegments`` because a PDF has a third
    outcome the other formats do not: the child was stopped by its ceiling
    (memory, CPU, the deadline). That is a document SKIPPED, counted like one
    over the byte cap, not a parse that ended early -- and ``_grep_docs`` is
    where skips are counted.
    """
    outcome = extract_pdf_segments(data, max_chars=_GREP_DOC_MAX_CHARS, deadline=deadline)
    if outcome.resource_failure:
        logger.warning("file_grep: PDF %s skipped: extractor %s", path, outcome.failure)
    return outcome


def _grep_doc_segments(data: bytes, path: str, ext: str, deadline: float) -> _DocSegments:
    """Extract already-authorized document bytes as ``(label, text)`` segments.

    The label stands in for a line number: ``slide 7`` for a deck, ``Sheet1 · row
    12`` for a worksheet, ``page 3`` for a PDF. A Word file gets an EMPTY label --
    its paragraphs carry no location a reader could navigate to.

    Parsers receive only ``BytesIO``, so none can reopen the path after the
    safe-read identity check. ``.docx``/``.pptx`` go through
    :func:`kiro_crew.doc_parser.extract_text`, already hardened against zip bombs
    and entity expansion. Its text is requested one character PAST the cap:
    coming back longer is the only way to tell a document cut at the cap from one
    that ended there.

    ``.pdf`` is not handled here: its extractor runs out of process and can be
    STOPPED rather than merely cut short, which ``_grep_docs`` counts as a skip.
    """
    if ext == ".xlsx":
        return _grep_xlsx_segments(data, path, deadline)
    text = extract_text(
        path,
        filename=os.path.basename(path),
        max_chars=_GREP_DOC_MAX_CHARS + 1,
        fileobj=io.BytesIO(data),
    )
    whole = len(text) <= _GREP_DOC_MAX_CHARS
    if not text:
        return (), True
    if ext == ".pptx":
        segments: list[tuple[str, str]] = []
        for block in text.split("\n\n"):
            head, _, body = block.partition("\n")
            slide = _GREP_PPTX_SLIDE_RE.match(head.strip())
            if slide and body:
                segments.append((f"slide {slide.group(1)}", body))
            elif block.strip():
                segments.append(("", block))
        return tuple(segments), whole
    return (("", text),), whole


def _grep_docs(root: str, query: str, deadline: float, taken: int) -> tuple[list[dict], int, bool]:
    """Document pass: (hits, documents skipped, truncated).

    A document is skipped when it is over the byte cap or when the PDF
    extractor child was stopped by its ceiling (memory, CPU, the deadline) --
    either way its text was never read, and the answer is marked partial.

    Runs AFTER the text pass inside the SAME deadline, so a tree of large
    documents can never slow a plain-text search down. ``skipped_docs`` is what
    the rail's status line names, and it is a FLOOR: a spent deadline ends the
    walk rather than counting its way through the rest of the tree, which would
    hold a transfer worker for up to ``_GREP_MAX_DIRS_VISITED`` directories after
    the budget was gone.
    """
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    out: list[dict] = []
    skipped = 0
    dirs_visited = 0
    doc_truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() >= deadline:
            return out, skipped + 1, True
        dirs_visited += 1
        if dirs_visited > _GREP_MAX_DIRS_VISITED:
            return out, skipped, True
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _WALK_SKIP_DIRS and not is_sensitive_path(os.path.join(dirpath, d))
        ]
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in _GREP_DOC_EXTS:
                continue
            full = os.path.join(dirpath, name)
            # Same rule as `_grep_python`: a link can point outside the root the
            # caller named, and `safe_read_prefix` refuses only credential stores,
            # not an ordinary out-of-root document.
            if os.path.islink(full):
                continue
            if is_sensitive_path(full):
                continue
            if taken + len(out) >= _GREP_MAX_RESULTS:
                return out, skipped, True
            if time.monotonic() >= deadline:
                return out, skipped + 1, True
            try:
                data = safe_read_prefix(full, _GREP_DOC_MAX_BYTES + 1)
            except (OSError, FileTooLargeError):
                continue
            if data is None:
                continue
            if len(data) > _GREP_DOC_MAX_BYTES:
                skipped += 1
                continue
            if ext == ".pdf":
                pdf = _grep_pdf_segments(data, full, deadline)
                if pdf.resource_failure:
                    # The child hit a ceiling: the document was not read, so it
                    # is a skip AND the answer is partial. A parse the child
                    # refused is a settled answer, like a workbook that is not a
                    # zip, and yields no segments and no flag.
                    skipped += 1
                    doc_truncated = True
                    continue
                segments = pdf.segments
                whole = pdf.failure is not None or not pdf.truncated
            else:
                segments, whole = _grep_doc_segments(data, full, ext, deadline)
            if not whole:
                doc_truncated = True
            for label, text in segments:
                match = pattern.search(text)
                if match is None:
                    continue
                position = match.start()
                # One hit per document, like one hit per text file.
                line_start = text.rfind("\n", 0, position) + 1
                line_end = text.find("\n", position)
                preview = text[line_start:] if line_end < 0 else text[line_start:line_end]
                out.append(_grep_hit(full, 0, preview.strip(), label=label))
                break
    return out, skipped, doc_truncated
