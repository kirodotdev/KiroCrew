#!/usr/bin/env python3
"""check_decisions_history.py — decision ledger entries are immutable.

``docs/decisions/`` is an append-only record of product and design decisions a
person on the team made. Each entry is one file; the directory's README is the
front page and index. The rules are in ``docs/decisions/README.md``; this gate
enforces the one that can be checked without reading:

    every ``docs/decisions/*.md`` present at the base ref, except ``README.md``,
    must exist at head byte-identical

A deleted entry, a renamed entry and an edited entry all fail. A new regular-file
entry is allowed, because appending is the only way the ledger grows. At both
base and head, the directory admits only this README and flat regular ``*.md``
files, nothing else. A closed list of admitted shapes is checkable; a growing
list of refused shapes is not. Every child is validated before the README is
exempted from byte comparison: a link resolves to whatever its target says, so
its resolved bytes are not the entry's bytes. Base entries and file modes come
from Git's immediate-child tree listing, not the checkout. The ledger directory
itself must be a tree at base and a real directory at head, not a link to another
ledger. Head entries are checked without following links before their bytes are
read. The README's bytes may change because adding an entry means adding its
index row, and docs-lint refuses an unindexed doc.

The head side judges what Git would commit, not everything the directory holds.
Its children are enumerated with ``git ls-files --cached --others
--exclude-standard``: tracked files plus untracked files Git does not ignore. A
new entry that is not yet committed is therefore still read, and a stray
untracked file that Git would commit is still refused, while editor and OS noise
the repository already ignores (a ``*.swp`` while an entry is being authored, a
``.DS_Store``) is not ledger content and cannot fail the gate. Anything Git
cannot store at all, a FIFO or an empty directory, is likewise invisible to the
verdict: the base side never sees such a thing either, so both sides judge the
same content. Each listed child is still checked without following links, and a
listed path with a directory component is refused through that directory.

Every ``git`` call runs in the repository root, resolved from this file's own
location, never in the caller's working directory. The two sides of the
comparison must come from the same repository, and the base side is a path
lookup: ``git ls-tree <ref> -- docs/decisions`` resolves that path against the
directory git runs in, so from any other directory it matches nothing, and a
ledger that matches nothing is indistinguishable from an empty one. A gate that
read the base from the caller's CWD would therefore pass silently, reporting
"0 entries intact", whenever it was invoked from a subdirectory. The self-test
proves the verdict does not depend on the CWD.

## Why this is a deterministic gate and not a review rule

An entry exists so that a later change does not quietly reverse a decision. The
easiest way to reverse one is to make the record say something else, or to make
it say nothing: an "edit" that softens a sentence, a rename that drops the file
out of a grep, a deletion buried in a hunk beside a real change. All three read
as plausible in review, because the reviewer looks at what was added. So the
judgment half of the rule (does this change reverse a recorded decision without
a superseding entry?) stays in ``AUTOSDE.yaml``, where a reviewer applies it, and
the mechanical half lives here, where no reviewer has to notice anything.

A change of mind is a NEW entry naming the one it supersedes, so the correct
shape of a ledger diff is additions only, and this gate has **no exemptions**.

## Usage

    # enforce against a base ref (exit 1 on any violation)
    DECISIONS_BASE_REF="$(git merge-base HEAD origin/main)" \\
        python3 scripts/check_decisions_history.py

    # self-test: one probe per defect class, assert each verdict
    python3 scripts/check_decisions_history.py --test

With no ``DECISIONS_BASE_REF`` the check still enforces the closed-world head
content rule, then exits 0 with a note when there is nothing to compare against.

Pass the MERGE BASE, not the base branch's moving tip. CI passes
``pull_request.base.sha`` and tests the merge ref, so the two agree there; locally
they do not. A branch that is merely BEHIND a base which has since gained an
entry reports that entry as missing — true and useless. The merge base is the
commit the branch actually departed from, so it answers "did THIS branch remove
or rewrite anything".
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DECISIONS_DIR = "docs/decisions"
INDEX = "README.md"


def _git(root: Path, *args: str) -> str | None:
    """Run ``git`` inside ``root`` and return stdout, or ``None`` when it fails.

    ``root`` is always passed explicitly: the base-side lookups are path-relative,
    so a git call that inherited the process CWD would answer for whatever
    directory the caller happened to be in.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            # A path byte that is not UTF-8 must round-trip back to the
            # filesystem unchanged; a replacement character would make the
            # head side stat a name Git never reported.
            errors="surrogateescape",
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return None


def entries_at(ref: str, root: Path, directory: str = DECISIONS_DIR) -> dict[str, bytes]:
    """Read flat regular-file entries from Git's tree, rejecting every other child.

    An absent ledger is empty. A failed enumeration or blob read is a violation,
    not evidence that the base has nothing to protect. Check the directory's
    exact record because listings of a link contain no child entries.
    """
    exact = _git(root, "ls-tree", "-z", ref, "--", directory)
    if exact is None:
        raise ValueError(f"Cannot enumerate {directory} at {ref} with git ls-tree.")
    if not exact:
        return {}
    for record in exact.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode, kind, _oid = metadata.split()
        if path != directory or mode != "040000" or kind != "tree":
            reason = "a symlink" if mode == "120000" else f"Git mode {mode} ({kind})"
            raise ValueError(
                f"{directory} at {ref} is {reason}. The ledger directory itself "
                "must be a tree; a non-tree record is not an absent ledger."
            )
    listing = _git(root, "ls-tree", "-z", f"{ref}:{directory}")
    if listing is None:
        raise ValueError(f"Cannot enumerate {directory} at {ref} with git ls-tree.")
    found: dict[str, bytes] = {}
    for record in listing.split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, oid = metadata.split()
        path = f"{directory}/{name}"
        if mode not in {"100644", "100755"} or kind != "blob":
            reason = "a symlink" if mode == "120000" else f"Git mode {mode} ({kind})"
            raise ValueError(
                f"{path} at {ref} is {reason}. Every ledger child must be a flat "
                "regular file named README.md or ending in .md; nothing else is allowed."
            )
        if not name.endswith(".md") or "/" in name:
            raise ValueError(
                f"{path} at {ref} has an unadmitted name. Every ledger child must be "
                "a flat regular file named README.md or ending in .md; nothing else is allowed."
            )
        if name == INDEX:
            continue
        blob = subprocess.run(
            ["git", "cat-file", "blob", oid],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            raise ValueError(f"Cannot read {path} at {ref} byte-for-byte.")
        found[name] = blob.stdout
    return found


def _committable_children(root: Path, directory: str = DECISIONS_DIR) -> list[str]:
    """Names of the ledger's immediate children that Git would commit, sorted.

    Tracked files and untracked files Git does not ignore, as ``git ls-files``
    reports them relative to ``root``. A nested path contributes its first
    component, so the directory it lives in is what gets judged. Ignored files
    are absent, and so is anything Git cannot store.
    """
    listing = _git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", directory
    )
    if listing is None:
        raise ValueError(f"Cannot enumerate {directory} in the checkout with git ls-files.")
    prefix = f"{directory}/"
    children: set[str] = set()
    for record in listing.split("\0"):
        if not record.startswith(prefix):
            continue
        children.add(record[len(prefix) :].split("/", 1)[0])
    return sorted(children)


def entries_in_tree(root: Path) -> dict[str, bytes]:
    """Read flat head entries, rejecting every child outside the admitted shapes.

    A link's resolved bytes belong to its target, not to the ledger entry.
    The ledger directory and every directory between the repository root and
    it are checked before enumerating children: a link anywhere on that path
    makes the gate compare whatever the link points at. The children judged
    are the ones Git would commit; a tracked child that is missing from the
    checkout is left for the base comparison to report as gone.
    """
    directory = root / DECISIONS_DIR
    walked = root
    for part in Path(DECISIONS_DIR).parts:
        walked = walked / part
        try:
            walked_mode = os.lstat(walked).st_mode
        except FileNotFoundError:
            return {}
        if not stat.S_ISDIR(walked_mode):
            reason = "a symlink" if stat.S_ISLNK(walked_mode) else "not a directory"
            raise ValueError(
                f"{walked} is {reason}. The ledger directory and every directory "
                "above it must be a real directory; a link makes the gate compare "
                "whatever the link points at."
            )
    found: dict[str, bytes] = {}
    for name in _committable_children(root):
        path = directory / name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            reason = "a symlink" if stat.S_ISLNK(mode) else "not a plain regular file"
            raise ValueError(
                f"{path} is {reason}. Every ledger child must be a flat regular file "
                "named README.md or ending in .md; nothing else is allowed."
            )
        if not name.endswith(".md"):
            raise ValueError(
                f"{path} has an unadmitted name. Every ledger child must be a flat "
                "regular file named README.md or ending in .md; nothing else is allowed."
            )
        if name == INDEX:
            continue
        found[name] = path.read_bytes()
    return found


def compare(base: dict[str, bytes], head: dict[str, bytes]) -> list[str]:
    """Every base entry that is missing or changed at head, as problem lines.

    An entry that exists at head under a different name is reported as
    renamed, so the message points at the fix (restore the file) rather than at
    two unrelated symptoms.
    """
    problems: list[str] = []
    head_by_content: dict[bytes, list[str]] = {}
    for name, content in head.items():
        head_by_content.setdefault(content, []).append(name)
    for name in sorted(base):
        content = base[name]
        if name not in head:
            renamed = [n for n in head_by_content.get(content, []) if n not in base]
            if renamed:
                problems.append(
                    f"{DECISIONS_DIR}/{name} was RENAMED to {DECISIONS_DIR}/{renamed[0]}. "
                    f"An entry keeps its name for as long as the ledger exists; a rename "
                    f"drops it out of every grep and link that found it."
                )
            else:
                problems.append(
                    f"{DECISIONS_DIR}/{name} is GONE. An entry is never deleted; a "
                    f"change of mind is a new entry with a 'Supersedes:' line."
                )
        elif head[name] != content:
            problems.append(
                f"{DECISIONS_DIR}/{name} was EDITED. An entry is never edited after it "
                f"is written; a change of mind is a new entry with a 'Supersedes:' line."
            )
    return problems


def enforce(root: Path, base_ref: str) -> int:
    """Compare the ledger at ``root``'s head against ``base_ref``; return the exit code.

    ``root`` is the repository the gate protects. Both sides are read from it,
    so the verdict is the same whichever directory the process runs in.
    """
    try:
        head = entries_in_tree(root)
    except (ValueError, OSError) as exc:
        print(f"::error::decision-ledger-history: {exc}", file=sys.stderr)
        return 1

    if not base_ref:
        print(
            "decision-ledger-history: no DECISIONS_BASE_REF, so nothing to compare "
            "the ledger against (set it to enforce, e.g. DECISIONS_BASE_REF=origin/main)"
        )
        return 0

    try:
        base = entries_at(base_ref, root)
    except (ValueError, OSError) as exc:
        print(f"::error::decision-ledger-history: {exc}", file=sys.stderr)
        return 1

    problems = compare(base, head)
    if problems:
        for problem in problems:
            print(f"::error::decision-ledger-history: {problem}", file=sys.stderr)
        print(
            "\nA decision record is append-only. To change what the team decided, "
            "add a new entry that names the one it supersedes and quotes the "
            "maintainer who decided; leave the original file exactly as it is. "
            "See docs/decisions/README.md.",
            file=sys.stderr,
        )
        return 1

    print(
        f"decision-ledger-history: {len(base)} entr{'y' if len(base) == 1 else 'ies'} "
        f"at {base_ref} intact at head OK"
    )
    return 0


def _probe_repo(root: Path) -> bool:
    """Make ``root`` a throwaway repository so the head side has a Git to ask."""
    return _git(root, "init", "--quiet") is not None


def _probe_tree(root: Path, stage: bool) -> str | None:
    """Build a base tree in a throwaway repository at ``root``; return its oid.

    Git trees expose a directory's own mode independently of its children, and
    an index needs no commit or identity to write one. ``root`` is passed to
    every git call so the probe never depends on the process CWD.
    """
    if not _probe_repo(root):
        return None
    if stage and _git(root, "add", "--", DECISIONS_DIR) is None:
        return None
    tree = _git(root, "write-tree")
    return tree.strip() if tree is not None else None


def _self_test() -> int:
    """Probe immutability, regular-file enforcement and allowed ledger shapes."""
    entry = b"# Buttons say New\n\nDecided by: A Person\n"
    other = b"# Panels are blue\n\nDecided by: A Person\n"
    base = {"2026-07-20-buttons-say-new.md": entry, "2026-08-01-panels-are-blue.md": other}
    cases: list[tuple[str, dict[str, bytes], bool]] = [
        (
            "a deleted entry is caught",
            {"2026-08-01-panels-are-blue.md": other},
            True,
        ),
        (
            "an edited entry is caught",
            {
                "2026-07-20-buttons-say-new.md": entry + b"\nEDITED\n",
                "2026-08-01-panels-are-blue.md": other,
            },
            True,
        ),
        (
            "a renamed entry is caught",
            {"2026-07-20-buttons-say-new-chat.md": entry, "2026-08-01-panels-are-blue.md": other},
            True,
        ),
        (
            "a new entry is allowed",
            {**base, "2026-09-23-a-third-decision.md": b"# Third\n"},
            False,
        ),
        (
            "a superseding entry beside the untouched original is allowed",
            {
                **base,
                "2026-09-23-buttons-say-create.md": b"# Create\n\nSupersedes: 2026-07-20-buttons-say-new.md\n",
            },
            False,
        ),
        ("an unchanged ledger is clean", dict(base), False),
    ]
    failures: list[str] = []
    for name, head, expect in cases:
        got = bool(compare(base, head))
        if got != expect:
            failures.append(f"{name}: expected violation={expect}, got {got}")

    # The rename probe must name the rename, not report an unrelated deletion,
    # or the message sends the author looking for a file that is still there.
    renamed = compare(
        base, {"2026-07-20-buttons-say-new-chat.md": entry, "2026-08-01-panels-are-blue.md": other}
    )
    if not any("RENAMED" in line for line in renamed):
        failures.append("a rename is reported as something other than a rename")

    # The README is the index, not an entry: editing it is how a row is added,
    # so the tree walk must validate its shape without comparing its bytes. An
    # uncommitted entry is untracked but not ignored, so it is read.
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / DECISIONS_DIR
        ledger.mkdir(parents=True)
        (ledger / INDEX).write_bytes(b"# Decisions\n")
        (ledger / "2026-07-20-buttons-say-new.md").write_bytes(entry)
        if not _probe_repo(Path(tmp)):
            failures.append("index probe: cannot initialize probe repository")
        else:
            seen = entries_in_tree(Path(tmp))
            if set(seen) != {"2026-07-20-buttons-say-new.md"}:
                failures.append(f"entries_in_tree surfaced the wrong files: {sorted(seen)}")
        # ...and a tree with no ledger at all is an empty set, not an error.
        if entries_in_tree(Path(tmp) / "elsewhere") != {}:
            failures.append("a tree without docs/decisions/ is not an empty ledger")

    probes = len(cases) + 4
    skipped = 0
    # Exercise both readers through enforce, including its path-bearing error.
    # Base probes clear the invalid checkout child so only the recorded tree
    # can cause the violation, even when the child is absent at head.
    for side in ("head", "base"):
        for shape in ("nested", "nested.md", "notes.txt", "README.md", "link", "link.md"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ledger = root / DECISIONS_DIR
                ledger.mkdir(parents=True)
                if not _probe_repo(root):
                    failures.append(f"{side} {shape}: cannot initialize probe repository")
                    continue
                path = ledger / shape
                if shape in {"nested", "nested.md"}:
                    path.mkdir()
                    (path / "entry.md").write_bytes(entry)
                elif shape in {"README.md", "link", "link.md"}:
                    target = root / "target.txt"
                    target.write_bytes(entry)
                    try:
                        os.symlink(target, path)
                    except (OSError, NotImplementedError) as exc:
                        print(f"self-test SKIP: {side} {shape}: symlink unavailable ({exc})")
                        skipped += 1
                        continue
                else:
                    path.write_bytes(b"not markdown")
                ref = ""
                if side == "base":
                    tree = _probe_tree(root, stage=True)
                    if tree is None:
                        failures.append(f"{side} {shape}: cannot build probe repository tree")
                        continue
                    ref = tree
                    if shape in {"nested", "nested.md"}:
                        (path / "entry.md").unlink()
                        path.rmdir()
                    else:
                        path.unlink()
                probes += 1
                captured = io.StringIO()
                with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                    code = enforce(root, ref)
                report = captured.getvalue()
                if (
                    code != 1
                    or "::error::" not in report
                    or f"{DECISIONS_DIR}/{shape}" not in report
                ):
                    failures.append(f"{side} {shape}: missing rejection and path: {report!r}")

    # The head side judges what Git would commit. An ignored swap file and a
    # FIFO Git cannot store are not ledger content beside intact entries; an
    # untracked file Git would commit still is, and is refused by name.
    for shape in ("ignored", "fifo", "stray"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / DECISIONS_DIR
            ledger.mkdir(parents=True)
            for name, content in base.items():
                (ledger / name).write_bytes(content)
            (root / ".gitignore").write_bytes(b"*.swp\n")
            tree = _probe_tree(root, stage=True)
            if tree is None:
                failures.append(f"head {shape}: cannot build probe repository tree")
                continue
            if shape == "ignored":
                (ledger / "x.swp").write_bytes(b"editor state")
            elif shape == "fifo":
                try:
                    os.mkfifo(ledger / "fifo")
                except (AttributeError, OSError, NotImplementedError) as exc:
                    print(f"self-test SKIP: head {shape}: FIFO unavailable ({exc})")
                    skipped += 1
                    continue
            else:
                (ledger / "notes.txt").write_bytes(b"not markdown")
            probes += 1
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                code = enforce(root, tree)
            report = captured.getvalue()
            if shape == "stray":
                if code != 1 or f"{DECISIONS_DIR}/notes.txt" not in report:
                    failures.append(f"head {shape}: untracked stray file accepted: {report!r}")
            elif code != 0 or "2 entries" not in report or "intact at head OK" not in report:
                failures.append(f"head {shape}: content Git would not commit failed: {report!r}")

    # An entry whose name is not valid UTF-8 is still one entry: its bytes must
    # reach the filesystem intact on both sides, or an unchanged ledger reads
    # as GONE. Filesystems that refuse such a name skip the probe.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ledger = root / DECISIONS_DIR
        ledger.mkdir(parents=True)
        for name, content in base.items():
            (ledger / name).write_bytes(content)
        try:
            odd = os.fsdecode(b"2026-07-21-caf\xe9.md")
            (ledger / odd).write_bytes(entry)
        except (UnicodeDecodeError, OSError, ValueError) as exc:
            print(f"self-test SKIP: non-UTF-8 entry name unavailable ({exc})")
            skipped += 1
        else:
            tree = _probe_tree(root, stage=True)
            if tree is None:
                failures.append("non-UTF-8 entry name: cannot build probe repository tree")
            else:
                probes += 1
                captured = io.StringIO()
                with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                    code = enforce(root, tree)
                report = captured.getvalue()
                if code != 0 or "3 entries" not in report or "intact at head OK" not in report:
                    failures.append(
                        f"non-UTF-8 entry name: unchanged ledger not intact: {report!r}"
                    )

    for replace in (False, True):
        label = "replacement symlink" if replace else "new identical-content symlink"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / DECISIONS_DIR
            ledger.mkdir(parents=True)
            if not _probe_repo(root):
                failures.append(f"{label}: cannot initialize probe repository")
                continue
            target = root / "original.txt"
            path = ledger / "2026-07-20-buttons-say-new.md"
            if replace:
                path.write_bytes(entry)
                path.rename(target)
            else:
                target.write_bytes(entry)
            try:
                os.symlink(target, path)
            except (OSError, NotImplementedError) as exc:
                print(f"self-test SKIP: {label}: symlink creation unavailable ({exc})")
                skipped += 1
                continue
            probes += 1
            try:
                entries_in_tree(root)
            except ValueError as exc:
                if str(path) not in str(exc) or "symlink" not in str(exc):
                    failures.append(f"{label}: violation does not name the link and reason")
            else:
                failures.append(f"{label}: accepted a link to identical bytes")

    # "ancestor" links the parent directory instead of the ledger itself: a link
    # one level up resolves to the same bytes and must be refused just the same.
    for shape in ("symlink", "blob", "ancestor"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / DECISIONS_DIR
            ledger.mkdir(parents=True)
            for name, content in base.items():
                (ledger / name).write_bytes(content)
            linked = ledger.parent if shape == "ancestor" else ledger
            target = root / "original-ledger"
            linked.rename(target)
            if shape in ("symlink", "ancestor"):
                try:
                    os.symlink(target, linked, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    print(f"self-test SKIP: ledger {shape}: creation unavailable ({exc})")
                    skipped += 1
                    continue
            else:
                ledger.write_bytes(entry)
            probes += 1
            try:
                entries_in_tree(root)
            except ValueError as exc:
                if str(linked) not in str(exc) or "real directory" not in str(exc):
                    failures.append(f"head ledger {shape}: violation lacks path and reason")
            else:
                failures.append(f"head ledger {shape}: accepted a non-directory ledger")

    # Base-side shapes are read from Git's tree, so each probe repository is
    # addressed by its own root; nothing here changes the process CWD.
    for shape in ("absent", "tree", "blob", "symlink"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / DECISIONS_DIR
            ledger.parent.mkdir(parents=True)
            if shape == "tree":
                ledger.mkdir()
                for name, content in base.items():
                    (ledger / name).write_bytes(content)
            elif shape == "blob":
                ledger.write_bytes(entry)
            elif shape == "symlink":
                target = root / "original-ledger"
                target.mkdir()
                for name, content in base.items():
                    (target / name).write_bytes(content)
                try:
                    os.symlink(target, ledger, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    print(f"self-test SKIP: base ledger symlink: creation unavailable ({exc})")
                    skipped += 1
                    continue
            tree = _probe_tree(root, stage=shape != "absent")
            if tree is None:
                failures.append(f"base ledger {shape}: cannot build probe repository tree")
                continue
            probes += 1
            try:
                seen = entries_at(tree, root)
            except ValueError as exc:
                if shape not in {"blob", "symlink"}:
                    failures.append(f"base ledger {shape}: unexpected violation: {exc}")
                elif DECISIONS_DIR not in str(exc) or "must be a tree" not in str(exc):
                    failures.append(f"base ledger {shape}: violation lacks path and reason")
            else:
                if shape in {"blob", "symlink"}:
                    failures.append(f"base ledger {shape}: accepted a non-tree ledger")
                elif seen != (base if shape == "tree" else {}):
                    failures.append(f"base ledger {shape}: read incorrect entries")

    # The verdict must not depend on the caller's CWD. Run the whole enforcement
    # from a subdirectory of a probe repository whose head has lost an entry: the
    # base must still be read from the repository root and the deletion caught.
    # A base read relative to the CWD would find no ledger there and pass as
    # "0 entries intact".
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ledger = root / DECISIONS_DIR
        ledger.mkdir(parents=True)
        for name, content in base.items():
            (ledger / name).write_bytes(content)
        tree = _probe_tree(root, stage=True)
        if tree is None:
            failures.append("subdirectory probe: cannot build probe repository tree")
        else:
            (ledger / "2026-07-20-buttons-say-new.md").unlink()
            subdirectory = root / "website"
            subdirectory.mkdir()
            probes += 1
            captured = io.StringIO()
            original_cwd = Path.cwd()
            try:
                os.chdir(subdirectory)
                with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                    code = enforce(root, tree)
            finally:
                os.chdir(original_cwd)
            report = captured.getvalue()
            if code != 1 or "2026-07-20-buttons-say-new.md is GONE" not in report:
                failures.append(
                    "subdirectory probe: enforcement run from a subdirectory did not "
                    f"read the base entries (exit {code}): {report.strip()!r}"
                )

    # A directory named like an entry is judged through the file Git sees in it.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / DECISIONS_DIR / "2026-07-20-buttons-say-new.md"
        path.mkdir(parents=True)
        (path / "inner.md").write_bytes(entry)
        if not _probe_repo(root):
            failures.append("directory entry: cannot initialize probe repository")
        else:
            probes += 1
            try:
                entries_in_tree(root)
            except ValueError as exc:
                if str(path) not in str(exc) or "regular file" not in str(exc):
                    failures.append("directory entry: violation does not name the path and reason")
            else:
                failures.append("a directory named as an entry is accepted")

    # Nothing at base means nothing to protect: the very first entry lands
    # through this gate, so an empty base must not refuse it.
    if compare({}, {"2026-09-23-first.md": b"# First\n"}):
        failures.append("a ledger with nothing at base refuses its first entry")

    if failures:
        for line in failures:
            print(f"self-test FAILED: {line}", file=sys.stderr)
        return 1
    print(f"decision-ledger-history self-test: {probes} probes, all correct; {skipped} skipped")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return _self_test()

    root = Path(__file__).resolve().parents[1]
    base_ref = os.environ.get("DECISIONS_BASE_REF", "").strip()
    return enforce(root, base_ref)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
