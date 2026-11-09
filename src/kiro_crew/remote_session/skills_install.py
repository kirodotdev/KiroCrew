"""Guest-side install of the skills a remote session's agent may use.

Installed beside ``worker.py`` and run by the gateway over the same shell
(``coder/remote_worker.py``). Each copy is content-addressed and read-only:
``~/.kirocrew-worker/skills/<digest>/<skill key>/``. A copy is never changed
once complete, so a script running from it is never swapped mid-run, and two
sessions whose agents see the same skills share one copy.

    python3 skills_install.py check DIGEST     # report a complete copy, or none
    python3 skills_install.py install DIGEST   # stdin: a JSON line of
                                               # [[path, size, mode], ...], then bytes

Both print one JSON line: ``present``, ``root``, the interpreter, and the copied
``.py`` files that do not compile on it, so a script written for a newer Python
is reported as that rather than failing unexplained.

Stdlib only and Python 3.6 compatible, so it runs, and reports, on an old guest.
"""

import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
import time

BASE = os.path.join(os.path.expanduser("~"), ".kirocrew-worker", "skills")
MARKER = ".complete"
_DIGEST = re.compile(r"^[0-9a-f]{16,64}$")
#: A partial install untouched this long was abandoned (a killed install) and is removed.
STALE_SECONDS = 24 * 3600
#: Bounds that match the gateway's (``skills_copy.MAX_*``), checked again here.
MAX_FILES = 5000
MAX_TOTAL_BYTES = 32 << 20
MAX_REPORTED = 200


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(2)


def safe_rel(rel):
    """Whether ``rel`` is a plain relative path with no hidden or parent part."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        return False
    # An absolute path has an empty first part, and ``..`` is hidden.
    return all(part and not part.startswith(".") for part in rel.split(posixpath.sep))


def make_writable(path):
    """Give the owner write access back throughout ``path``, so it can be removed."""
    for root, dirs, _files in os.walk(path):
        os.chmod(root, stat.S_IRWXU)
        for name in dirs:
            child = os.path.join(root, name)
            if not os.path.islink(child):
                os.chmod(child, stat.S_IRWXU)


def remove_tree(path):
    try:
        make_writable(path)
        shutil.rmtree(path)
    except OSError:
        pass


def incompatible(root):
    """The copied ``.py`` files this interpreter cannot compile, with the reason:
    the first :data:`MAX_REPORTED` as examples, and how many there are in all."""
    found = []  # type: list
    total = 0
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, "rb") as handle:
                    compile(handle.read(), path, "exec", dont_inherit=True)
            except SyntaxError as exc:
                reason = "line %s: %s" % (exc.lineno, exc.msg)
            except Exception as exc:  # MemoryError/RecursionError on a deeply nested file
                reason = str(exc)[:200]
            else:
                continue
            total += 1
            if len(found) < MAX_REPORTED:
                found.append([os.path.relpath(path, root), reason])
    return found, total


def prune():
    """Remove partial installs abandoned for :data:`STALE_SECONDS`.

    A complete copy is never removed: a runtime that started from it may still
    run its scripts, and nothing here can tell. shortcut: complete copies stay
    until the workspace is deleted (each at most :data:`MAX_TOTAL_BYTES`); prune
    them once the gateway reports which digests its live runtimes use.
    """
    now = time.time()
    try:
        names = os.listdir(BASE)
    except OSError:
        return
    for name in names:
        path = os.path.join(BASE, name)
        if not name.startswith(".tmp-") or os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if age > STALE_SECONDS:
            remove_tree(path)


def report(digest, present):
    root = os.path.join(BASE, digest)
    result = {
        "present": present,
        "root": root,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "executable": sys.executable,
    }
    found, total = incompatible(root) if present else (None, 0)
    result["incompatible"] = found or []
    result["incompatible_total"] = total
    sys.stdout.write(json.dumps(result) + "\n")


def complete(root):
    marker = os.path.join(root, MARKER)
    if os.path.islink(root) or not os.path.isfile(marker):
        return False
    try:
        os.utime(marker, None)
    except OSError:
        pass
    return True


def install(digest):
    stream = sys.stdin.buffer
    try:
        entries = json.loads(stream.readline().decode("utf-8"))
    except ValueError:
        fail("skills header is not JSON")
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        fail("skills header is not a bounded list")
    total = 0
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 3 and safe_rel(entry[0])):
            fail("unsafe skill file path")
        if not all(isinstance(v, int) and v >= 0 for v in entry[1:]):
            fail("bad skill file size or mode")
        total += entry[1]
    if total > MAX_TOTAL_BYTES:
        fail("skills payload over its size bound")
    root = os.path.join(BASE, digest)
    os.makedirs(BASE, mode=0o700, exist_ok=True)
    # A unique name, so a folder left by a killed install never blocks this one.
    tmp = tempfile.mkdtemp(prefix=".tmp-%s-" % digest, dir=BASE)
    try:
        for rel, size, mode in entries:
            data = stream.read(size)
            if len(data) != size:
                fail("truncated skill file")
            path = os.path.join(tmp, *rel.split(posixpath.sep))
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(path, 0o555 if mode & 0o111 else 0o444)
        with open(os.path.join(tmp, MARKER), "w") as handle:
            handle.write(digest + "\n")
        # Read-only from the bottom up: a script can read and run its siblings
        # but cannot change the copy another session may be running from. The
        # top folder last, after the rename: macOS refuses to rename a folder
        # its owner cannot write.
        for folder, _dirs, _files in sorted(os.walk(tmp), key=lambda w: -len(w[0])):
            if folder != tmp:
                os.chmod(folder, 0o555)
        try:
            os.rename(tmp, root)
        except OSError:
            # Another session installed the same digest first: that copy is it.
            if not complete(root):
                raise
            remove_tree(tmp)
        else:
            os.chmod(root, 0o555)
    except BaseException:
        remove_tree(tmp)
        raise


def main(argv):
    if len(argv) != 2 or argv[0] not in ("check", "install") or not _DIGEST.match(argv[1]):
        fail("usage: skills_install.py check|install DIGEST")
    mode, digest = argv
    os.makedirs(BASE, mode=0o700, exist_ok=True)
    present = complete(os.path.join(BASE, digest))
    if mode == "install" and not present:
        install(digest)
        present = True
    prune()
    report(digest, present)


if __name__ == "__main__":
    main(sys.argv[1:])
