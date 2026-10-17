"""AsciiDoc ``include::`` expansion in the knowledge FileReader.

Included content is indexed inside the document that includes it. A target is
read only when it is a file the folder scan selected for the source (the
``include_paths`` set) and it lies inside the top-level document's directory;
every other directive stays in the text as written. Sets here are built with
the real folder walk, so the walk's ignore rules are exercised, not restated.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge import folder_watcher, readers
from kiro_crew.knowledge.folder_watcher import FolderWatcher, walk_filters
from kiro_crew.knowledge.readers import FileReader


def _walk_set(root, **props):
    screened: set[str] = set()
    FolderWatcher(None, None)._walk(str(root), screened_out=screened, **walk_filters(props))
    return frozenset(os.path.normcase(r) for r in screened)


#: The swap tests inject the race by patching ``os.open``, which is how
#: ``platform_compat.open_file_no_reparse`` opens on POSIX. Windows opens through
#: ``CreateFileW``, so the patch never fires there and the swap never happens.
_os_open_seam = pytest.mark.skipif(
    os.name == "nt", reason="the swap is injected through os.open, which Windows does not use"
)


def _read(path, root=None, **props):
    include_paths = _walk_set(root or path.parent, **props)
    return FileReader().read(str(path), include_paths=include_paths)


def test_relative_and_nested_includes_are_expanded(tmp_path):
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "intro.adoc").write_text(
        "intro text\ninclude::deeper.adoc[]\n", encoding="utf-8"
    )
    (tmp_path / "parts" / "deeper.adoc").write_text("deeper text\n", encoding="utf-8")
    (tmp_path / "snippet.py").write_text("print('snippet')\n", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text(
        "= Guide\n\ninclude::parts/intro.adoc[leveloffset=+1]\n\n"
        "----\ninclude::snippet.py[]\n----\nend\n",
        encoding="utf-8",
    )

    text, _meta = _read(doc)

    assert "include::" not in text
    assert "intro text\ndeeper text\n\n----\nprint('snippet')\n----\nend" in text


def test_reader_without_an_include_set_never_expands(tmp_path):
    (tmp_path / "part.adoc").write_text("PART", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text("include::part.adoc[]", encoding="utf-8")
    text, meta = FileReader().read(str(doc))
    assert text == "include::part.adoc[]"
    assert meta["format"] == "adoc"


def test_asciidoc_extension_also_expands(tmp_path):
    (tmp_path / "part.adoc").write_text("part body", encoding="utf-8")
    doc = tmp_path / "guide.asciidoc"
    doc.write_text("include::part.adoc[]", encoding="utf-8")
    text, meta = _read(doc)
    assert text == "part body"
    assert meta["format"] == "asciidoc"


@pytest.mark.parametrize("trailing", ["   ", "\t", " \t "])
def test_trailing_whitespace_after_the_directive_still_expands(tmp_path, trailing):
    (tmp_path / "part.adoc").write_text("part body", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text(f"before\ninclude::part.adoc[]{trailing}\nafter", encoding="utf-8")
    text, _meta = _read(doc)
    assert text == "before\npart body\nafter"


def test_leading_whitespace_still_keeps_the_directive_with_trailing_space(tmp_path):
    (tmp_path / "part.adoc").write_text("part body", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text("  include::part.adoc[]  ", encoding="utf-8")
    text, _meta = _read(doc)
    assert text == "  include::part.adoc[]  "


def test_include_cycle_stops_and_keeps_the_directive(tmp_path):
    (tmp_path / "a.adoc").write_text("A\ninclude::b.adoc[]", encoding="utf-8")
    (tmp_path / "b.adoc").write_text("B\ninclude::a.adoc[]", encoding="utf-8")
    text, _meta = _read(tmp_path / "a.adoc")
    assert text == "A\nB\ninclude::a.adoc[]"


def test_target_outside_the_document_directory_is_not_read(tmp_path):
    (tmp_path / "outside.adoc").write_text("SECRET-OUTSIDE", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "guide.adoc"
    doc.write_text("include::../outside.adoc[]", encoding="utf-8")
    # The walk root is the parent, so the target IS in the set: the jail refuses it.
    assert os.path.normcase(os.path.realpath(tmp_path / "outside.adoc")) in _walk_set(tmp_path)
    text, _meta = _read(doc, root=tmp_path)
    assert "SECRET-OUTSIDE" not in text
    assert text == "include::../outside.adoc[]"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlink_inside_the_directory_pointing_out_is_not_read(tmp_path):
    (tmp_path / "outside.adoc").write_text("SECRET-OUTSIDE", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    try:
        os.symlink(tmp_path / "outside.adoc", docs / "link.adoc")
    except OSError:
        pytest.skip("symlink creation not permitted")
    doc = docs / "guide.adoc"
    doc.write_text("include::link.adoc[]", encoding="utf-8")
    text, _meta = _read(doc)
    assert "SECRET-OUTSIDE" not in text
    assert text == "include::link.adoc[]"


@pytest.mark.parametrize(
    "directive",
    [
        "include::https://example.com/remote.adoc[]",
        "include::partial$shared.adoc[]",
        "include::{partialsdir}/shared.adoc[]",
        "\\include::shared.adoc[]",
        "  include::shared.adoc[]",
        "include::missing.adoc[]",
    ],
)
def test_unresolvable_directives_stay_as_written(tmp_path, directive):
    (tmp_path / "shared.adoc").write_text("SHARED", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text(directive, encoding="utf-8")
    text, _meta = _read(doc)
    assert text == directive


def test_nul_target_stays_as_written(tmp_path):
    doc = tmp_path / "guide.adoc"
    original = "= Guide\ninclude::bad\x00.adoc[]\nend\n"
    doc.write_text(original, encoding="utf-8")

    text, meta = _read(doc)

    assert text == original
    assert meta["format"] == "adoc"


def test_absolute_target_stays_as_written(tmp_path):
    shared = tmp_path / "shared.adoc"
    shared.write_text("SHARED", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text(f"include::{shared}[]", encoding="utf-8")
    text, _meta = _read(doc)
    assert text == f"include::{shared}[]"


def test_target_the_walk_marks_sensitive_is_not_read(tmp_path, monkeypatch):
    (tmp_path / "creds.adoc").write_text("TOKEN", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text("include::creds.adoc[]", encoding="utf-8")
    flagged = os.path.realpath(tmp_path / "creds.adoc")
    monkeypatch.setattr(folder_watcher, "is_sensitive_path", lambda p, *a: p == flagged)
    text, _meta = _read(doc)
    assert text == "include::creds.adoc[]"


def test_binary_and_oversized_targets_are_not_read(tmp_path, monkeypatch):
    (tmp_path / "blob.txt").write_bytes(b"abc\x00def")
    (tmp_path / "big.adoc").write_text("x" * 50, encoding="utf-8")
    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDE_BYTES", 40)
    doc = tmp_path / "guide.adoc"
    doc.write_text("include::blob.txt[]\ninclude::big.adoc[]", encoding="utf-8")
    text, _meta = _read(doc)
    assert text == "include::blob.txt[]\ninclude::big.adoc[]"


def test_depth_limit_stops_a_long_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDE_DEPTH", 2)
    for i in range(4):
        (tmp_path / f"l{i}.adoc").write_text(f"L{i}\ninclude::l{i + 1}.adoc[]", encoding="utf-8")
    (tmp_path / "l4.adoc").write_text("L4", encoding="utf-8")
    text, _meta = _read(tmp_path / "l0.adoc")
    assert text == "L0\nL1\nL2\ninclude::l3.adoc[]"


def test_fan_out_and_size_caps_bound_the_expansion(tmp_path, monkeypatch):
    (tmp_path / "p.adoc").write_text("PPPP", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text("\n".join(["include::p.adoc[]"] * 5), encoding="utf-8")

    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDES", 3)
    text, _meta = _read(doc)
    assert text.count("PPPP") == 3
    assert text.count("include::p.adoc[]") == 2

    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDES", 256)
    monkeypatch.setattr(readers, "_ADOC_MAX_CHARS", len(doc.read_text()) + 8)
    text, _meta = _read(doc)
    assert text.count("PPPP") == 2


def test_markdown_include_lines_are_not_expanded(tmp_path):
    (tmp_path / "part.adoc").write_text("PART", encoding="utf-8")
    md = tmp_path / "notes.md"
    md.write_text("include::part.adoc[]", encoding="utf-8")
    text, _meta = _read(md)
    assert text == "include::part.adoc[]"


def test_failed_directives_count_toward_the_include_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDES", 2)
    (tmp_path / "blob.txt").write_bytes(b"abc\x00def")
    (tmp_path / "good.adoc").write_text("GOOD", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text(
        "include::blob.txt[]\ninclude::missing.adoc[]\ninclude::good.adoc[]", encoding="utf-8"
    )
    text, _meta = _read(doc)
    assert "GOOD" not in text
    assert text == "include::blob.txt[]\ninclude::missing.adoc[]\ninclude::good.adoc[]"


def test_kiroignore_excluded_target_stays_as_written(tmp_path):
    (tmp_path / ".kiroignore").write_text("secret.adoc\n", encoding="utf-8")
    (tmp_path / "secret.adoc").write_text("SECRET-IGNORED", encoding="utf-8")
    doc = tmp_path / "README.adoc"
    doc.write_text("include::secret.adoc[]", encoding="utf-8")
    text, _meta = _read(doc)
    assert text == "include::secret.adoc[]"


def test_ignore_patterns_excluded_target_stays_as_written(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "notes.adoc").write_text("SECRET-PATTERN", encoding="utf-8")
    doc = tmp_path / "README.adoc"
    doc.write_text("include::private/notes.adoc[]", encoding="utf-8")
    text, _meta = _read(doc, ignore_patterns=["private/*"])
    assert text == "include::private/notes.adoc[]"


def test_include_extensions_narrowing_applies_to_targets(tmp_path):
    (tmp_path / "notes.txt").write_text("TXT", encoding="utf-8")
    doc = tmp_path / "README.adoc"
    doc.write_text("include::notes.txt[]", encoding="utf-8")
    text, _meta = _read(doc, include_extensions=[".adoc"])
    assert text == "include::notes.txt[]"


@pytest.mark.parametrize(
    "target",
    [
        "server.pem",
        "config/private.key",
        "node_modules/pkg/part.adoc",
        "build/part.adoc",
        "package-lock.json",
        "x.tmp",
        ".git/config",
        ".env",
        "sub/.hidden/part.adoc",
    ],
)
def test_target_the_walk_does_not_select_stays_as_written(tmp_path, target):
    path = tmp_path / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("url = https://user:SECRET-TOKEN@example.com/repo", encoding="utf-8")
    doc = tmp_path / "README.adoc"
    doc.write_text(f"include::{target}[]", encoding="utf-8")
    text, _meta = _read(doc)
    assert "SECRET-TOKEN" not in text
    assert text == f"include::{target}[]"


@pytest.mark.parametrize(
    "target", ["notes.txt", "data.json", "LICENSE", "build.adoc", "sub/out.py"]
)
def test_text_targets_the_walk_selects_expand(tmp_path, target):
    path = tmp_path / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("WALKED", encoding="utf-8")
    doc = tmp_path / "README.adoc"
    doc.write_text(f"include::{target}[]", encoding="utf-8")
    text, _meta = _read(doc)
    assert text == "WALKED"


def test_kernel_path_judgement_requires_jail_and_membership(tmp_path):
    root = os.path.realpath(tmp_path)
    member = os.path.join(root, "part.adoc")
    outside = os.path.join(os.path.dirname(root), "part.adoc")
    includes = readers._AdocIncludes(
        root, frozenset({os.path.normcase(member), os.path.normcase(outside)})
    )
    assert not includes._refuse_opened(member)
    assert includes._refuse_opened(os.path.join(root, "other.adoc"))
    assert includes._refuse_opened(outside)
    assert includes._refuse_opened(os.path.join(root, ".env"))


class _LookupOnlySet(frozenset):
    """A member set that answers lookups but refuses to be walked."""

    def __iter__(self):
        raise AssertionError("include set was iterated")


def test_include_set_is_looked_up_not_copied(tmp_path):
    part = tmp_path / "part.adoc"
    part.write_text("PART", encoding="utf-8")
    plain = tmp_path / "plain.adoc"
    plain.write_text("no directives here", encoding="utf-8")
    doc = tmp_path / "guide.adoc"
    doc.write_text("include::part.adoc[]", encoding="utf-8")
    members = _LookupOnlySet(_walk_set(tmp_path))

    assert FileReader().read(str(plain), include_paths=members)[0] == "no directives here"
    assert FileReader().read(str(doc), include_paths=members)[0] == "PART"


@_os_open_seam
@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_parent_swapped_for_a_symlink_after_the_check_is_not_read(tmp_path, monkeypatch):
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "part.adoc").write_text("SECRET-OUTSIDE", encoding="utf-8")
    docs = tmp_path / "docs"
    parts = docs / "parts"
    parts.mkdir(parents=True)
    (parts / "part.adoc").write_text("inside", encoding="utf-8")
    doc = docs / "guide.adoc"
    doc.write_text("include::parts/part.adoc[]", encoding="utf-8")
    try:
        os.symlink(secret_dir, tmp_path / "probe")
    except OSError:
        pytest.skip("symlink creation not permitted")
    include_paths = _walk_set(tmp_path)

    real_open = os.open
    target = os.path.realpath(parts / "part.adoc")

    def swapping_open(path, flags, *args, **kwargs):
        if os.fspath(path) == target and parts.is_dir() and not parts.is_symlink():
            os.rename(parts, docs / "parts-old")
            os.symlink(secret_dir, parts)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(readers.os, "open", swapping_open)
    text, _meta = FileReader().read(str(doc), include_paths=include_paths)
    assert "SECRET-OUTSIDE" not in text
    assert text == "include::parts/part.adoc[]"


@_os_open_seam
@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_parent_swapped_only_during_the_open_is_not_read(tmp_path, monkeypatch):
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "part.adoc").write_text("SECRET-OUTSIDE", encoding="utf-8")
    docs = tmp_path / "docs"
    parts = docs / "parts"
    parts.mkdir(parents=True)
    (parts / "part.adoc").write_text("inside", encoding="utf-8")
    doc = docs / "guide.adoc"
    doc.write_text("include::parts/part.adoc[]", encoding="utf-8")
    try:
        os.symlink(secret_dir, tmp_path / "probe")
    except OSError:
        pytest.skip("symlink creation not permitted")
    # Walk the parent so the outside file is a member too: only the jail stops it.
    include_paths = _walk_set(tmp_path)
    assert os.path.normcase(os.path.realpath(secret_dir / "part.adoc")) in include_paths

    real_open = os.open
    target = os.path.realpath(parts / "part.adoc")
    swaps = []

    def swap_around_open(path, flags, *args, **kwargs):
        if os.fspath(path) != target or swaps:
            return real_open(path, flags, *args, **kwargs)
        os.rename(parts, docs / "parts-old")
        os.symlink(secret_dir, parts)
        swaps.append(1)
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            os.unlink(parts)
            os.rename(docs / "parts-old", parts)

    monkeypatch.setattr(readers.os, "open", swap_around_open)
    text, _meta = FileReader().read(str(doc), include_paths=include_paths)
    assert swaps == [1]
    assert "SECRET-OUTSIDE" not in text
    assert text == "include::parts/part.adoc[]"


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hardlinks")
def test_hardlinked_target_is_not_read(tmp_path):
    outside = tmp_path / "outside.adoc"
    outside.write_text("SECRET-OUTSIDE", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    try:
        os.link(outside, docs / "alias.adoc")
    except OSError:
        pytest.skip("hardlink creation not permitted")
    doc = docs / "guide.adoc"
    doc.write_text("include::alias.adoc[]", encoding="utf-8")
    assert os.path.normcase(os.path.realpath(docs / "alias.adoc")) in _walk_set(docs)
    text, _meta = _read(doc)
    assert text == "include::alias.adoc[]"


@_os_open_seam
@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_witness_parent_swapped_before_the_fence_is_not_read(tmp_path, monkeypatch):
    from kiro_crew import pinned_fs

    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "part.adoc").write_text("SECRET-OUTSIDE", encoding="utf-8")
    docs = tmp_path / "docs"
    parts = docs / "parts"
    parts.mkdir(parents=True)
    (parts / "part.adoc").write_text("inside", encoding="utf-8")
    doc = docs / "guide.adoc"
    doc.write_text("include::parts/part.adoc[]", encoding="utf-8")
    try:
        os.symlink(secret_dir, tmp_path / "probe")
    except OSError:
        pytest.skip("symlink creation not permitted")
    include_paths = _walk_set(tmp_path)

    real_open = os.open
    real_fd_path = pinned_fs.fd_real_path
    target = os.path.realpath(parts / "part.adoc")
    events = []

    def swap_around_open(path, flags, *args, **kwargs):
        if os.fspath(path) != target or events:
            return real_open(path, flags, *args, **kwargs)
        os.rename(parts, docs / "parts-old")
        os.symlink(secret_dir, parts)
        events.append("open")
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            os.unlink(parts)
            os.rename(docs / "parts-old", parts)

    def repoint_witness(fd):
        witness = real_fd_path(fd)
        # The witness names the outside file; re-point its parent into the
        # jail so a fence that resolves the name again would approve it.
        os.rename(secret_dir, tmp_path / "secret-old")
        os.symlink(parts, secret_dir)
        events.append("witness")
        return witness

    monkeypatch.setattr(readers.os, "open", swap_around_open)
    monkeypatch.setattr(pinned_fs, "fd_real_path", repoint_witness)
    text, _meta = FileReader().read(str(doc), include_paths=include_paths)
    assert events == ["open", "witness"]
    assert "SECRET-OUTSIDE" not in text
    assert text == "include::parts/part.adoc[]"


class _ReadingPipeline:
    """Pipeline stand-in that reads each file with the real FileReader."""

    def __init__(self, store, source_id):
        self.store = store
        self.source_id = source_id
        self.texts = {}
        self.calls = {}
        self.reads = []

    async def ingest_file(self, path, *, on_committed=None, **kwargs):
        name = os.path.basename(path)
        self.reads.append(name)
        self.calls[name] = kwargs["include_paths"]
        self.texts[name], _meta = FileReader().read(
            path, include_paths=kwargs["include_paths"], included=kwargs.get("included")
        )
        item_id = self.store.add_item(name, self.texts[name], "doc", source_id=self.source_id)
        if on_committed is not None:
            on_committed([item_id])
        return "job"


@pytest.mark.asyncio
async def test_folder_scan_expands_only_selected_files(tmp_path):
    from kiro_crew.knowledge.store import KnowledgeStore

    folder = tmp_path / "docs"
    (folder / "parts").mkdir(parents=True)
    (folder / ".kiroignore").write_text("secret.adoc\n", encoding="utf-8")
    (folder / "secret.adoc").write_text("SECRET-IGNORED", encoding="utf-8")
    (folder / "parts" / "part.adoc").write_text("SELECTED-PART", encoding="utf-8")
    (folder / "notes.md").write_text("include::parts/part.adoc[]", encoding="utf-8")
    (folder / "README.adoc").write_text(
        "= Readme\ninclude::parts/part.adoc[]\ninclude::secret.adoc[]\n", encoding="utf-8"
    )
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        source_id = store.add_source("docs", "local_folder", str(folder))
        pipeline = _ReadingPipeline(store, source_id)
        source = {
            "id": source_id,
            "uri": str(folder),
            "source_type": "local_folder",
            "properties": json.dumps({}),
        }
        await FolderWatcher(store, pipeline).scan_source(source)
    finally:
        store.close()

    readme = pipeline.texts["README.adoc"]
    assert "SELECTED-PART" in readme
    assert "SECRET-IGNORED" not in readme
    assert "include::secret.adoc[]" in readme
    assert "secret.adoc" not in pipeline.texts
    # Every ingested file is handed the same set; only the AsciiDoc reader uses it.
    assert set(pipeline.calls) == {"README.adoc", "part.adoc", "notes.md"}
    assert all(paths == pipeline.calls["README.adoc"] for paths in pipeline.calls.values())
    assert os.path.normcase(os.path.realpath(folder / "notes.md")) in pipeline.calls["notes.md"]
    assert pipeline.texts["notes.md"] == "include::parts/part.adoc[]"


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
async def test_name_repointed_after_the_walk_cannot_admit_a_rejected_file(tmp_path, monkeypatch):
    from kiro_crew.knowledge.store import KnowledgeStore

    folder = tmp_path / "docs"
    folder.mkdir()
    part = folder / "part.adoc"
    part.write_text("benign", encoding="utf-8")
    secret = folder / "cookies.txt"
    secret.write_text("SECRET-COOKIE", encoding="utf-8")
    (folder / "README.adoc").write_text("include::cookies.txt[]\n", encoding="utf-8")
    flagged = os.path.realpath(secret)
    monkeypatch.setattr(folder_watcher, "is_sensitive_path", lambda p, *a: p == flagged)
    try:
        os.symlink(secret, tmp_path / "probe")
    except OSError:
        pytest.skip("symlink creation not permitted")

    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        source_id = store.add_source("docs", "local_folder", str(folder))
        pipeline = _ReadingPipeline(store, source_id)
        watcher = FolderWatcher(store, pipeline)
        real_walk = watcher._walk

        def walk_then_repoint(*args, **kwargs):
            found = real_walk(*args, **kwargs)
            # The walk screened part.adoc and rejected cookies.txt; now the
            # selected name becomes a link to the rejected file.
            part.unlink()
            os.symlink(secret, part)
            return found

        monkeypatch.setattr(watcher, "_walk", walk_then_repoint)
        source = {
            "id": source_id,
            "uri": str(folder),
            "source_type": "local_folder",
            "properties": json.dumps({}),
        }
        await watcher.scan_source(source)
    finally:
        store.close()

    readme = pipeline.texts["README.adoc"]
    assert "SECRET-COOKIE" not in readme
    assert readme.strip() == "include::cookies.txt[]"


def _canon(path):
    return os.path.normcase(os.path.realpath(path))


def _targets(store, source_id, includer):
    """The include record for *includer*, or None when it has none."""
    row = store.db.execute(
        "SELECT targets FROM folder_include_targets WHERE source_id = ? AND file_path = ?",
        (source_id, str(includer)),
    ).fetchone()
    return None if row is None else json.loads(row["targets"])


def _set_record(store, source_id, includer, raw):
    store.db.execute(
        "INSERT OR REPLACE INTO folder_include_targets (source_id, file_path, targets) "
        "VALUES (?, ?, ?)",
        (source_id, str(includer), raw),
    )
    store.db.commit()


async def _scan_twice(tmp_path, folder, between):
    """Scan *folder*, run *between*, scan again; return the pipeline after each."""
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        source_id = store.add_source("docs", "local_folder", str(folder))
        source = {
            "id": source_id,
            "uri": str(folder),
            "source_type": "local_folder",
            "properties": json.dumps({}),
        }
        first = _ReadingPipeline(store, source_id)
        await FolderWatcher(store, first).scan_source(source)
        between()
        second = _ReadingPipeline(store, source_id)
        await FolderWatcher(store, second).scan_source(source)
    finally:
        store.close()
    return first, second


def _guide_with_part(folder):
    (folder / "parts").mkdir(parents=True)
    (folder / "parts" / "part.adoc").write_text("SECRET-PART", encoding="utf-8")
    guide = folder / "guide.adoc"
    guide.write_text("= Guide\ninclude::parts/part.adoc[]\nend\n", encoding="utf-8")
    return guide


@pytest.mark.asyncio
async def test_includer_is_reread_when_its_part_becomes_ignored(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    stat = guide.stat()

    def ignore_part():
        (folder / ".kiroignore").write_text("parts/part.adoc\n", encoding="utf-8")
        os.utime(guide, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    first, second = await _scan_twice(tmp_path, folder, ignore_part)

    assert "SECRET-PART" in first.texts["guide.adoc"]
    assert second.reads == ["guide.adoc"]
    assert "SECRET-PART" not in second.texts["guide.adoc"]
    assert "include::parts/part.adoc[]" in second.texts["guide.adoc"]


@pytest.mark.asyncio
async def test_includer_is_reread_when_its_part_is_deleted(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)

    first, second = await _scan_twice(
        tmp_path, folder, lambda: (folder / "parts" / "part.adoc").unlink()
    )

    assert "SECRET-PART" in first.texts["guide.adoc"]
    assert second.reads == ["guide.adoc"]
    assert "SECRET-PART" not in second.texts["guide.adoc"]
    assert guide.exists()


@pytest.mark.asyncio
async def test_reread_survives_a_sweep_that_stops_before_the_includer(tmp_path, monkeypatch):
    from kiro_crew.knowledge.store import KnowledgeStore

    folder = tmp_path / "docs"
    _guide_with_part(folder)
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        source_id = store.add_source("docs", "local_folder", str(folder))
        source = {
            "id": source_id,
            "uri": str(folder),
            "source_type": "local_folder",
            "properties": json.dumps({}),
        }
        first = _ReadingPipeline(store, source_id)
        await FolderWatcher(store, first).scan_source(source)
        (folder / "parts" / "part.adoc").unlink()

        # The sweep that sees the removal pauses before it reaches any file.
        stopped = _ReadingPipeline(store, source_id)
        watcher = FolderWatcher(store, stopped)
        monkeypatch.setattr(watcher, "_is_paused", lambda _sid: True)
        result = await watcher.scan_source(source)

        later = _ReadingPipeline(store, source_id)
        await FolderWatcher(store, later).scan_source(source)
    finally:
        store.close()

    assert "SECRET-PART" in first.texts["guide.adoc"]
    assert result["status"] == "paused" and result["deleted"] == 1
    assert stopped.reads == []
    assert later.reads == ["guide.adoc"]
    assert "SECRET-PART" not in later.texts["guide.adoc"]


@pytest.mark.asyncio
async def test_includer_invalidation_survives_cancellation_before_deletion(tmp_path, monkeypatch):
    from kiro_crew.knowledge.store import KnowledgeStore

    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    part = folder / "parts" / "part.adoc"
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        source_id = store.add_source("docs", "local_folder", str(folder))
        source = {
            "id": source_id,
            "uri": str(folder),
            "source_type": "local_folder",
            "properties": json.dumps({}),
        }
        pipeline = _ReadingPipeline(store, source_id)
        watcher = FolderWatcher(store, pipeline)
        await watcher.scan_source(source)
        assert "SECRET-PART" in pipeline.texts["guide.adoc"]
        part.unlink()

        cancelled_delete = AsyncMock(side_effect=asyncio.CancelledError)
        with monkeypatch.context() as patch:
            patch.setattr(watcher, "_handle_deleted", cancelled_delete)
            with pytest.raises(asyncio.CancelledError):
                await watcher.scan_source(source)
        cancelled_delete.assert_awaited_once()

        kept = await asyncio.to_thread(watcher._load_state, source_id)
        assert str(part) in kept
        assert _targets(store, source_id, guide) == [_canon(part)]

        pipeline.reads.clear()
        result = await watcher.scan_source(source)
        assert result["deleted"] == 1
        assert str(part) not in await asyncio.to_thread(watcher._load_state, source_id)
        assert pipeline.reads == ["guide.adoc"]
        assert "SECRET-PART" not in pipeline.texts["guide.adoc"]
        assert "include::parts/part.adoc[]" in pipeline.texts["guide.adoc"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_removal_elsewhere_does_not_force_a_reread(tmp_path):
    folder = tmp_path / "docs"
    (folder / "a").mkdir(parents=True)
    (folder / "a-b").mkdir()
    (folder / "a" / "part.adoc").write_text("PART", encoding="utf-8")
    (folder / "a" / "guide.adoc").write_text("include::part.adoc[]", encoding="utf-8")
    # A sibling whose name shares a's prefix: not below a.
    (folder / "a-b" / "gone.txt").write_text("GONE", encoding="utf-8")
    # A non-AsciiDoc file above the removed path is never forced.
    (folder / "notes.md").write_text("notes", encoding="utf-8")

    first, second = await _scan_twice(
        tmp_path, folder, lambda: (folder / "a-b" / "gone.txt").unlink()
    )

    assert first.texts["guide.adoc"] == "PART"
    assert second.reads == []


@pytest.fixture
def deduped_includer(tmp_path, request):
    from kiro_crew.knowledge.chunker import HeadingAwareChunker
    from kiro_crew.knowledge.ingestion import IngestionPipeline
    from kiro_crew.knowledge.store import KnowledgeStore

    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    param = getattr(request, "param", False)
    if param is True:
        guide.write_text("include::parts/part.adoc[]", encoding="utf-8")
    if param in ("literal", "literal_and_copy"):
        # Bytes, not write_text, so the bytes hash equals the text hash on
        # Windows too, where write_text writes CRLF.
        guide.write_bytes(_read(guide)[0].encode("utf-8"))
    if param in ("twins", "twins_and_copy"):
        # A second includer of the same part, so both expand to the same text.
        (folder / "guide2.adoc").write_text(guide.read_text(encoding="utf-8"), encoding="utf-8")
    holder_folder = tmp_path / "holder"
    holder_folder.mkdir()
    (holder_folder / "copy.adoc").write_text(_read(guide)[0], encoding="utf-8")
    if param in ("twins_and_copy", "literal_and_copy"):
        # Same expanded text, no include, and not above parts/, so removing the
        # part does not invalidate it.
        (folder / "other").mkdir()
        # Bytes, so a legacy copy's bytes hash equals its text hash on Windows too.
        (folder / "other" / "copy.adoc").write_bytes(_read(guide)[0].encode("utf-8"))
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            side_effect=lambda contents: [
                {"category": "document", "summary": "s", "entities": []} for _ in contents
            ]
        )
        pipeline = IngestionPipeline(
            store=store,
            extractor=extractor,
            chunker=HeadingAwareChunker(),
            reader=FileReader(),
            embedder=None,
            dedup_enabled=False,
        )
        watcher = FolderWatcher(store, pipeline)
        holder_id = store.add_source("holder", "local_folder", str(holder_folder))
        source_id = store.add_source("docs", "local_folder", str(folder))
        holder = {"id": holder_id, "uri": str(holder_folder), "source_type": "local_folder"}
        source = {"id": source_id, "uri": str(folder), "source_type": "local_folder"}
        assert asyncio.run(watcher.scan_source(holder))["failed"] == 0
        assert asyncio.run(watcher.scan_source(source))["failed"] == 0
        before = watcher._load_state(source_id)[str(guide)]
        assert before["status"] == "deduped" and json.loads(before["item_ids"]) == []
        assert before["text_hash"]
        assert (before["text_hash"] == before["content_hash"]) == (
            param in ("literal", "literal_and_copy")
        )
        holder_item = store.db.execute(
            "SELECT id FROM items WHERE source_id = ? AND content_hash = ?",
            (holder_id, before["text_hash"]),
        ).fetchone()["id"]
        assert source_id in store.sources_holding_item(holder_item)
        yield store, watcher, source, guide, holder_id, holder_item, before
    finally:
        store.close()


@pytest.mark.parametrize("pause_before_ingest", [False, True])
def test_invalidated_deduped_includer_releases_its_old_claim(
    deduped_includer, monkeypatch, pause_before_ingest
):
    store, watcher, source, guide, holder_id, holder_item, before = deduped_includer
    (guide.parent / "parts" / "part.adoc").unlink()
    assert watcher._hash_file(str(guide)) == before["content_hash"]

    with monkeypatch.context() as patch:
        if pause_before_ingest:
            patch.setattr(watcher, "_is_paused", lambda _sid: True)
        result = asyncio.run(watcher.scan_source(source))

    assert result["deleted"] == 1
    assert source["id"] not in store.sources_holding_item(holder_item)
    if pause_before_ingest:
        assert result["status"] == "paused"
        kept = watcher._load_state(source["id"])[str(guide)]
        # Retired: it names no document, and its empty bytes hash forces the re-read.
        assert kept["text_hash"] is None and kept["content_hash"] == ""
        assert json.loads(kept["item_ids"]) == []
        assert _targets(store, source["id"], guide) == [_canon(guide.parent / "parts/part.adoc")]
        assert asyncio.run(watcher.scan_source(source))["failed"] == 0

    after = watcher._load_state(source["id"])[str(guide)]
    assert after["status"] == "done" and after["text_hash"] != before["text_hash"]
    assert json.loads(after["item_ids"])
    store.delete_source_cascade(holder_id)
    assert (
        store.db.execute(
            "SELECT id FROM items WHERE content_hash = ?", (before["text_hash"],)
        ).fetchall()
        == []
    )
    assert (
        store.db.execute(
            "SELECT item_id FROM source_locations WHERE item_id = ?", (holder_item,)
        ).fetchall()
        == []
    )
    guide.unlink()
    assert asyncio.run(watcher.scan_source(source))["deleted"] == 1
    assert (
        store.db.execute("SELECT id FROM items WHERE source_id = ?", (source["id"],)).fetchall()
        == []
    )


def test_deduped_includer_records_its_targets_with_its_claim(deduped_includer):
    store, _watcher, source, guide, _holder_id, _holder_item, _before = deduped_includer
    assert _targets(store, source["id"], guide) == [_canon(guide.parent / "parts/part.adoc")]


def test_dropping_stale_items_rolls_back_as_one(tmp_path):
    from kiro_crew._sqlite_compat import sqlite3
    from kiro_crew.knowledge.store import KnowledgeStore

    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        source_id = store.add_source("docs", "local_folder", str(folder))
        source = {"id": source_id, "uri": str(folder), "source_type": "local_folder"}
        watcher = FolderWatcher(store, _ReadingPipeline(store, source_id))
        asyncio.run(watcher.scan_source(source))
        before = watcher._load_state(source_id)[str(guide)]
        store.db.execute(
            "CREATE TEMP TRIGGER refuse_drop BEFORE UPDATE ON folder_file_state "
            "WHEN NEW.item_ids = '[]' BEGIN SELECT RAISE(ABORT, 'drop refused'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="drop refused"):
            watcher._retire_stale_include_text(source_id, [str(guide)])

        assert not store.db.in_transaction
        assert watcher._load_state(source_id)[str(guide)] == before
        assert all(store.get_item(i) for i in json.loads(before["item_ids"]))
        assert _targets(store, source_id, guide)
    finally:
        store.close()


@pytest.mark.parametrize("deduped_includer", [True], indirect=True)
@pytest.mark.parametrize("cancel_before_delete", [False, True])
def test_removed_part_does_not_preserve_includers_shared_claim(
    deduped_includer, monkeypatch, cancel_before_delete
):
    store, watcher, source, guide, _holder_id, holder_item, before = deduped_includer
    source_id = source["id"]
    part = guide.parent / "parts" / "part.adoc"
    part_state = watcher._load_state(source_id)[str(part)]
    assert part_state["text_hash"] == before["text_hash"]
    assert part_state["status"] == "deduped" and json.loads(part_state["item_ids"]) == []
    assert store.detach_source_location_by_hash(source_id, before["text_hash"]) == 0
    assert source_id in store.sources_holding_item(holder_item)
    part.unlink()

    detach = store.detach_source_location_by_hash
    invalidation_detaches = []

    def record_detach(sid, text_hash, *, exclude_folder_paths=()):
        result = detach(sid, text_hash, exclude_folder_paths=exclude_folder_paths)
        if exclude_folder_paths:
            assert list(exclude_folder_paths) == [str(part)]
            invalidation_detaches.append(result)
        return result

    monkeypatch.setattr(store, "detach_source_location_by_hash", record_detach)
    if cancel_before_delete:
        cancelled_delete = AsyncMock(side_effect=asyncio.CancelledError)
        with monkeypatch.context() as patch:
            patch.setattr(watcher, "_handle_deleted", cancelled_delete)
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(watcher.scan_source(source))
        cancelled_delete.assert_awaited_once()
        assert str(part) in watcher._load_state(source_id)
        assert _targets(store, source_id, guide) == [_canon(part)]
        assert source_id not in store.sources_holding_item(holder_item)
        assert invalidation_detaches == [1]

    result = asyncio.run(watcher.scan_source(source))
    assert result["deleted"] == 1 and result["failed"] == 0
    after = watcher._load_state(source_id)
    assert str(part) not in after
    assert after[str(guide)]["text_hash"] != before["text_hash"]
    assert source_id not in store.sources_holding_item(holder_item)
    assert invalidation_detaches == ([1, 0] if cancel_before_delete else [1])


def _remove_part_and_record_detaches(store, watcher, source, guide, monkeypatch):
    """Delete the shared part, sweep, and return what each invalidation detach did."""
    detach = store.detach_source_location_by_hash
    invalidation_detaches = []

    def record_detach(sid, text_hash, *, exclude_folder_paths=()):
        result = detach(sid, text_hash, exclude_folder_paths=exclude_folder_paths)
        if exclude_folder_paths:
            invalidation_detaches.append(result)
        return result

    (guide.parent / "parts" / "part.adoc").unlink()
    monkeypatch.setattr(store, "detach_source_location_by_hash", record_detach)
    result = asyncio.run(watcher.scan_source(source))
    assert result["deleted"] == 1 and result["failed"] == 0
    return invalidation_detaches


@pytest.mark.parametrize("deduped_includer", ["twins"], indirect=True)
def test_includers_sharing_a_hash_in_one_batch_release_the_claim(deduped_includer, monkeypatch):
    store, watcher, source, guide, holder_id, holder_item, before = deduped_includer
    source_id = source["id"]
    twin = watcher._load_state(source_id)[str(guide.parent / "guide2.adoc")]
    assert twin["status"] == "deduped" and json.loads(twin["item_ids"]) == []
    assert twin["text_hash"] == before["text_hash"]

    detaches = _remove_part_and_record_detaches(store, watcher, source, guide, monkeypatch)

    assert source_id not in store.sources_holding_item(holder_item)
    # One release per shared text hash.
    assert detaches == [1]
    after = watcher._load_state(source_id)
    assert all(
        after[str(guide.parent / name)]["text_hash"] != before["text_hash"]
        for name in ("guide.adoc", "guide2.adoc")
    )
    store.delete_source_cascade(holder_id)
    assert (
        store.db.execute(
            "SELECT id FROM items WHERE content_hash = ?", (before["text_hash"],)
        ).fetchall()
        == []
    )


@pytest.mark.parametrize("deduped_includer", ["twins_and_copy"], indirect=True)
def test_row_outside_the_batch_keeps_the_shared_claim(deduped_includer, monkeypatch):
    store, watcher, source, guide, _holder_id, holder_item, before = deduped_includer
    source_id = source["id"]
    copy_path = str(guide.parent / "other" / "copy.adoc")
    copy = watcher._load_state(source_id)[copy_path]
    assert copy["status"] == "deduped" and copy["text_hash"] == before["text_hash"]

    detaches = _remove_part_and_record_detaches(store, watcher, source, guide, monkeypatch)

    assert detaches == [0]
    assert source_id in store.sources_holding_item(holder_item)
    after = watcher._load_state(source_id)[copy_path]
    assert after["status"] == "deduped" and after["text_hash"] == before["text_hash"]


@pytest.mark.parametrize("owned", [False, True], ids=["deduped", "done"])
@pytest.mark.parametrize("deferral", ["none", "pause", "budget"])
def test_epoch_includer_drops_removed_text(deduped_includer, monkeypatch, owned, deferral):
    store, watcher, source, guide, holder_id, _holder_item, before = deduped_includer
    if owned:
        store.delete_source_cascade(holder_id)
    state = watcher._load_state(source["id"])[str(guide)]
    assert state["status"] == ("done" if owned else "deduped")
    os.utime(guide, (0, 0))
    assert guide.stat().st_mtime == 0
    (guide.parent / "parts" / "part.adoc").unlink()

    with monkeypatch.context() as patch:
        if deferral == "pause":
            patch.setattr(watcher, "_is_paused", lambda _sid: True)
        elif deferral == "budget":
            fresh = guide.parent / "fresh.txt"
            fresh.write_text("A distinct document consumes the sweep budget.", encoding="utf-8")
            os.utime(fresh, (100, 100))
        result = asyncio.run(watcher.scan_source(source, chunk_budget=1))
    assert result["deleted"] == 1 and result["failed"] == 0
    if deferral == "pause":
        assert result["status"] == "paused"
        assert _targets(store, source["id"], guide) == [_canon(guide.parent / "parts/part.adoc")]
        assert asyncio.run(watcher.scan_source(source))["failed"] == 0
    elif deferral == "budget":
        # The stale includer goes ahead of the newer file the budget then defers.
        assert result["budget_reached"] == 1
        assert str(guide.parent / "fresh.txt") not in watcher._load_state(source["id"])
    assert _targets(store, source["id"], guide) is None

    after = watcher._load_state(source["id"])[str(guide)]
    assert after["status"] == "done" and after["text_hash"] != before["text_hash"]
    ids = json.loads(after["item_ids"])
    assert ids
    texts = [store.get_item(item_id)["content"] for item_id in ids]
    assert all("SECRET-PART" not in text for text in texts)
    assert any("include::parts/part.adoc[]" in text for text in texts)


@pytest.mark.parametrize("text", ["ordinary text", ""])
def test_unchanged_epoch_file_keeps_hash_shortcut(tmp_path, monkeypatch, text):
    from kiro_crew.knowledge.store import KnowledgeStore

    folder = tmp_path / "docs"
    folder.mkdir()
    doc = folder / "plain.txt"
    doc.write_text(text, encoding="utf-8")
    os.utime(doc, (0, 0))
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        source_id = store.add_source("docs", "local_folder", str(folder))
        source = {"id": source_id, "uri": str(folder), "source_type": "local_folder"}
        pipeline = _ReadingPipeline(store, source_id)
        watcher = FolderWatcher(store, pipeline)
        assert asyncio.run(watcher.scan_source(source))["failed"] == 0
        state = watcher._load_state(source_id)[str(doc)]
        assert state["mtime"] == 0 and state["content_hash"]
        hash_file = MagicMock(side_effect=AssertionError("unchanged file was hashed"))
        monkeypatch.setattr(watcher, "_hash_file", hash_file)
        pipeline.reads.clear()
        assert asyncio.run(watcher.scan_source(source))["failed"] == 0
        hash_file.assert_not_called()
        assert pipeline.reads == []
    finally:
        store.close()


@pytest.mark.parametrize("deduped_includer", ["literal"], indirect=True)
@pytest.mark.parametrize("missing_hash", [None, ""], ids=["null", "empty"])
def test_invalidation_releases_legacy_plaintext_claim(deduped_includer, monkeypatch, missing_hash):
    store, watcher, source, guide, holder_id, holder_item, before = deduped_includer
    store.db.execute(
        "UPDATE folder_file_state SET text_hash = ? WHERE source_id = ? AND file_path = ?",
        (missing_hash, source["id"], str(guide)),
    )
    # A literal copy has no record of its own; give it one so the part's
    # removal makes it stale.
    part = _canon(guide.parent / "parts/part.adoc")
    _set_record(store, source["id"], guide, json.dumps([part]))
    (guide.parent / "parts" / "part.adoc").unlink()
    monkeypatch.setattr(watcher, "_is_paused", lambda _sid: True)

    result = asyncio.run(watcher.scan_source(source))

    assert result["status"] == "paused" and result["deleted"] == 1
    assert source["id"] not in store.sources_holding_item(holder_item)
    store.delete_source_cascade(holder_id)
    assert not store.db.execute(
        "SELECT id FROM items WHERE content_hash = ?", (before["text_hash"],)
    ).fetchall()


@pytest.mark.parametrize("deduped_includer", ["literal"], indirect=True)
@pytest.mark.parametrize("missing_hash", [None, ""], ids=["null", "empty"])
def test_deletion_releases_legacy_plaintext_claim(deduped_includer, missing_hash):
    store, watcher, source, guide, holder_id, holder_item, before = deduped_includer
    store.db.execute(
        "UPDATE folder_file_state SET text_hash = ? WHERE source_id = ? AND file_path = ?",
        (missing_hash, source["id"], str(guide)),
    )
    guide.unlink()

    result = asyncio.run(watcher.scan_source(source))

    assert result["deleted"] == 1 and result["failed"] == 0
    assert source["id"] not in store.sources_holding_item(holder_item)
    store.delete_source_cascade(holder_id)
    assert not store.db.execute(
        "SELECT id FROM items WHERE content_hash = ?", (before["text_hash"],)
    ).fetchall()


@pytest.mark.parametrize("deduped_includer", ["literal_and_copy"], indirect=True)
@pytest.mark.parametrize("missing_hash", [None, ""], ids=["null", "empty"])
def test_legacy_plaintext_copy_keeps_shared_claim(deduped_includer, monkeypatch, missing_hash):
    store, watcher, source, guide, holder_id, holder_item, before = deduped_includer
    copy_path = str(guide.parent / "other" / "copy.adoc")
    store.db.execute(
        "UPDATE folder_file_state SET text_hash = ? WHERE source_id = ? AND file_path = ?",
        (missing_hash, source["id"], copy_path),
    )
    _set_record(store, source["id"], guide, json.dumps([_canon(guide.parent / "parts/part.adoc")]))
    (guide.parent / "parts" / "part.adoc").unlink()
    monkeypatch.setattr(watcher, "_is_paused", lambda _sid: True)

    result = asyncio.run(watcher.scan_source(source))

    assert result["status"] == "paused" and result["deleted"] == 1
    assert source["id"] in store.sources_holding_item(holder_item)
    store.delete_source_cascade(holder_id)
    assert store.get_item(holder_item)["source_id"] == source["id"]
    assert watcher._load_state(source["id"])[copy_path]["text_hash"] == missing_hash
    assert store.get_item(holder_item)["content_hash"] == before["text_hash"]


def _folder_store(tmp_path, folder, props=None):
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "kb.db"))
    source_id = store.add_source("docs", "local_folder", str(folder))
    source = {
        "id": source_id,
        "uri": str(folder),
        "source_type": "local_folder",
        "properties": json.dumps(props or {}),
    }
    return store, source


def _scan(store, source):
    pipeline = _ReadingPipeline(store, source["id"])
    result = asyncio.run(FolderWatcher(store, pipeline).scan_source(source))
    return pipeline, result


def test_part_beyond_the_file_cap_is_still_tracked(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    part = folder / "parts" / "part.adoc"
    os.utime(part, (100, 100))
    os.utime(guide, (200, 200))
    store, source = _folder_store(tmp_path, folder, {"max_files": 1})
    try:
        first, result = _scan(store, source)
        assert result["capped"] == 1 and first.reads == ["guide.adoc"]
        assert "SECRET-PART" in first.texts["guide.adoc"]
        part.unlink()
        second, _ = _scan(store, source)
    finally:
        store.close()
    assert second.reads == ["guide.adoc"]
    assert "SECRET-PART" not in second.texts["guide.adoc"]


def test_includer_beyond_the_file_cap_is_reread(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    part = folder / "parts" / "part.adoc"
    newer = folder / "newer.txt"
    newer.write_text("newer", encoding="utf-8")
    os.utime(guide, (100, 100))
    os.utime(part, (100, 100))
    os.utime(newer, (300, 300))
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        source["properties"] = json.dumps({"max_files": 1})
        part.unlink()
        second, result = _scan(store, source)
    finally:
        store.close()
    assert result["capped"] == 1
    assert second.reads == ["guide.adoc"]
    assert "SECRET-PART" not in second.texts["guide.adoc"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinked_includer_drops_a_removed_part(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    (folder / "links").mkdir()
    alias = folder / "links" / "alias.adoc"
    try:
        os.symlink(guide, alias)
    except OSError:
        pytest.skip("symlink creation not permitted")
    store, source = _folder_store(tmp_path, folder)
    try:
        first, _ = _scan(store, source)
        assert first.reads.count("guide.adoc") == 2
        part = folder / "parts" / "part.adoc"
        assert _targets(store, source["id"], alias) == [_canon(part)]
        part.unlink()
        second, _ = _scan(store, source)
        # Both logical includers are read again, not only the one under the part.
        assert second.reads == ["guide.adoc", "guide.adoc"]
        assert "SECRET-PART" not in second.texts["guide.adoc"]
        assert _targets(store, source["id"], alias) is None
        assert _targets(store, source["id"], guide) is None
    finally:
        store.close()


def test_nested_part_removal_rereads_every_level(tmp_path):
    folder = tmp_path / "docs"
    (folder / "parts").mkdir(parents=True)
    (folder / "parts" / "deep.adoc").write_text("SECRET-DEEP", encoding="utf-8")
    (folder / "parts" / "mid.adoc").write_text("include::deep.adoc[]", encoding="utf-8")
    (folder / "guide.adoc").write_text("include::parts/mid.adoc[]", encoding="utf-8")
    store, source = _folder_store(tmp_path, folder)
    try:
        first, _ = _scan(store, source)
        assert first.texts["guide.adoc"] == "SECRET-DEEP"
        assert _targets(store, source["id"], folder / "guide.adoc") == sorted(
            [_canon(folder / "parts" / "mid.adoc"), _canon(folder / "parts" / "deep.adoc")]
        )
        (folder / "parts" / "deep.adoc").unlink()
        second, _ = _scan(store, source)
    finally:
        store.close()
    assert sorted(second.reads) == ["guide.adoc", "mid.adoc"]
    assert second.texts["guide.adoc"] == "include::deep.adoc[]"


def test_removing_a_file_nothing_inlined_rereads_nothing(tmp_path):
    folder = tmp_path / "docs"
    _guide_with_part(folder)
    (folder / "parts" / "unused.txt").write_text("unused", encoding="utf-8")
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        (folder / "parts" / "unused.txt").unlink()
        second, result = _scan(store, source)
    finally:
        store.close()
    assert result["deleted"] == 1
    assert second.reads == []


@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_waiting_includer_loses_its_stale_items(tmp_path, status):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        ids = json.loads(
            FolderWatcher(store, None)._load_state(source["id"])[str(guide)]["item_ids"]
        )
        assert ids and "SECRET-PART" in store.get_item(ids[0])["content"]
        store.db.execute(
            "UPDATE folder_file_state SET status = ? WHERE source_id = ? AND file_path = ?",
            (status, source["id"], str(guide)),
        )
        store.db.commit()
        (folder / "parts" / "part.adoc").unlink()
        second, _ = _scan(store, source)
        row = FolderWatcher(store, None)._load_state(source["id"])[str(guide)]
        assert second.reads == []
        assert row["status"] == status and json.loads(row["item_ids"]) == []
        assert store.get_item(ids[0]) is None
        assert _targets(store, source["id"], guide) is None
    finally:
        store.close()


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', "[1]"])
def test_unreadable_record_forces_a_reread(tmp_path, raw):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        _set_record(store, source["id"], guide, raw)
        second, _ = _scan(store, source)
        assert _targets(store, source["id"], guide) == [_canon(folder / "parts" / "part.adoc")]
    finally:
        store.close()
    assert second.reads == ["guide.adoc"]


def test_source_cascade_drops_its_records(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        assert _targets(store, source["id"], guide)
        store.delete_source_cascade(source["id"])
        assert _targets(store, source["id"], guide) is None
    finally:
        store.close()


class _FailingPipeline(_ReadingPipeline):
    async def ingest_file(self, path, **kwargs):
        self.reads.append(os.path.basename(path))
        raise RuntimeError("extraction down")


def test_stale_includer_whose_reread_fails_loses_its_items_at_once(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        ids = json.loads(
            FolderWatcher(store, None)._load_state(source["id"])[str(guide)]["item_ids"]
        )
        (folder / "parts" / "part.adoc").unlink()
        failing = _FailingPipeline(store, source["id"])
        result = asyncio.run(FolderWatcher(store, failing).scan_source(source))
        row = FolderWatcher(store, None)._load_state(source["id"])[str(guide)]
        assert failing.reads == ["guide.adoc"] and result["failed"] == 1
        assert row["status"] == "failed" and json.loads(row["item_ids"]) == []
        assert all(store.get_item(i) is None for i in ids)
        # The next sweep treats it as waiting for the user and forgets the record.
        _scan(store, source)
        assert _targets(store, source["id"], guide) is None
    finally:
        store.close()


def test_every_stale_includer_loses_its_text_in_a_budget_limited_sweep(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    twin = folder / "twin.adoc"
    twin.write_text("= Twin\ninclude::parts/part.adoc[]\n", encoding="utf-8")
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        state = FolderWatcher(store, None)._load_state(source["id"])
        ids = [i for p in (guide, twin) for i in json.loads(state[str(p)]["item_ids"])]
        assert len(ids) == 2
        (folder / "parts" / "part.adoc").unlink()
        pipeline = _ReadingPipeline(store, source["id"])
        result = asyncio.run(FolderWatcher(store, pipeline).scan_source(source, chunk_budget=1))
        assert result["budget_reached"] == 1 and len(pipeline.reads) == 1
        assert all(store.get_item(i) is None for i in ids)
        hits = store.db.execute(
            "SELECT id FROM items WHERE source_id = ? AND content LIKE '%SECRET-PART%'",
            (source["id"],),
        ).fetchall()
        assert hits == []
        later, _ = _scan(store, source)
    finally:
        store.close()
    # The deferred one is still read on the next sweep.
    assert len(later.reads) == 1 and later.reads != pipeline.reads


@pytest.mark.parametrize("deduped_includer", [True], indirect=True)
def test_retried_deduped_includer_still_releases_its_claim(deduped_includer):
    store, watcher, source, guide, holder_id, holder_item, before = deduped_includer
    # The dashboard retry turns the row 'pending' and keeps its empty group.
    store.db.execute(
        "UPDATE folder_file_state SET status = 'pending' WHERE source_id = ? AND file_path = ?",
        (source["id"], str(guide)),
    )
    store.db.commit()
    (guide.parent / "parts" / "part.adoc").unlink()

    assert asyncio.run(watcher.scan_source(source))["failed"] == 0

    assert source["id"] not in store.sources_holding_item(holder_item)
    store.delete_source_cascade(holder_id)
    assert not store.db.execute(
        "SELECT id FROM items WHERE content_hash = ?", (before["text_hash"],)
    ).fetchall()


def test_includer_retired_then_deferred_is_reread_after_its_part_returns(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    twin = folder / "twin.adoc"
    twin.write_text("= Twin\ninclude::parts/part.adoc[]\n", encoding="utf-8")
    part = folder / "parts" / "part.adoc"
    store, source = _folder_store(tmp_path, folder, {"max_files": 2})
    try:
        os.utime(part, (100, 100))
        os.utime(guide, (300, 300))
        os.utime(twin, (300, 300))
        source["properties"] = json.dumps({})
        _scan(store, source)
        part.unlink()
        pipeline = _ReadingPipeline(store, source["id"])
        asyncio.run(FolderWatcher(store, pipeline).scan_source(source, chunk_budget=1))
        assert len(pipeline.reads) == 1
        deferred = str(twin if pipeline.reads == ["guide.adoc"] else guide)
        # The part comes back before the deferred includer is reached; the cap
        # now leaves the includers out of the newest-first list.
        part.write_text("SECRET-PART", encoding="utf-8")
        os.utime(part, (500, 500))
        source["properties"] = json.dumps({"max_files": 1})
        later, _ = _scan(store, source)
        row = FolderWatcher(store, None)._load_state(source["id"])[deferred]
    finally:
        store.close()
    assert os.path.basename(deferred) in later.reads
    assert json.loads(row["item_ids"]) and row["content_hash"]


def test_stale_scanning_includer_keeps_its_retry_budget(tmp_path):
    from kiro_crew.knowledge.folder_watcher import MAX_SCAN_ATTEMPTS

    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        ids = json.loads(
            FolderWatcher(store, None)._load_state(source["id"])[str(guide)]["item_ids"]
        )
        # Every attempt so far was interrupted: the budget is spent.
        store.db.execute(
            "UPDATE folder_file_state SET status = 'scanning', attempts = ? "
            "WHERE source_id = ? AND file_path = ?",
            (MAX_SCAN_ATTEMPTS, source["id"], str(guide)),
        )
        store.db.commit()
        (folder / "parts" / "part.adoc").unlink()
        second, result = _scan(store, source)
        row = FolderWatcher(store, None)._load_state(source["id"])[str(guide)]
        assert all(store.get_item(i) is None for i in ids)
    finally:
        store.close()
    assert second.reads == [] and result["failed"] == 1
    assert row["status"] == "failed" and row["content_hash"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_markdown_name_linked_to_an_includer_is_tracked(tmp_path):
    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    alias = folder / "notes.md"
    try:
        os.symlink(guide, alias)
    except OSError:
        pytest.skip("symlink creation not permitted")
    store, source = _folder_store(tmp_path, folder)
    try:
        _scan(store, source)
        part = folder / "parts" / "part.adoc"
        # The reader goes by the resolved suffix, so the .md name inlined the part.
        assert _targets(store, source["id"], alias) == [_canon(part)]
        part.unlink()
        _scan(store, source)
        row = FolderWatcher(store, None)._load_state(source["id"])[str(alias)]
        texts = [store.get_item(i)["content"] for i in json.loads(row["item_ids"])]
    finally:
        store.close()
    assert texts and all("SECRET-PART" not in t for t in texts)


def test_fuzzy_merged_includer_leaves_no_untracked_text(tmp_path):
    from kiro_crew.knowledge.dedup import dedup_sweep
    from kiro_crew.knowledge.embedder import floats_to_bytes

    folder = tmp_path / "docs"
    guide = _guide_with_part(folder)
    other = tmp_path / "other"
    other.mkdir()
    winner_guide = other / "guide.adoc"
    winner_guide.write_text("= Guide\nnear copy\nend\n", encoding="utf-8")
    os.utime(guide, (100, 100))
    os.utime(winner_guide, (300, 300))
    store, source = _folder_store(tmp_path, folder)
    try:
        winner_id = store.add_source("other", "local_folder", str(other))
        winner = {"id": winner_id, "uri": str(other), "source_type": "local_folder"}
        _scan(store, source)
        _scan(store, winner)
        # Same name and one shared embedding, different text: a fuzzy pair.
        store.db.execute(
            "UPDATE items SET embedding = ?, embedding_sig = 'sig' WHERE source_id IN (?, ?)",
            (floats_to_bytes([1.0, 0.0, 0.0]), source["id"], winner_id),
        )
        store.db.commit()
        assert _targets(store, source["id"], guide)

        # 1. The dedup sweep runs; 2. the part goes; 3. the other source goes.
        dedup_sweep(store, apply=True)
        (folder / "parts" / "part.adoc").unlink()
        _scan(store, source)
        store.delete_source_cascade(winner_id)

        named = {
            i
            for r in store.db.execute(
                "SELECT item_ids FROM folder_file_state WHERE source_id = ?", (source["id"],)
            )
            for i in json.loads(r["item_ids"] or "[]")
        }
        owned = {
            r["id"]
            for r in store.db.execute("SELECT id FROM items WHERE source_id = ?", (source["id"],))
        }
    finally:
        store.close()
    # Every item the source owns is named by one of its rows.
    assert owned <= named
