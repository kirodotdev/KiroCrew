"""argv[0] resolution and the batch-launcher guard shared by the scripts' command runners.

Stdlib only, and loaded by path (the scripts directory is not a package).

Two problems a bare ``subprocess.run(["gh", ...])`` has on Windows:

* CreateProcess appends only ``.exe`` when it searches PATH, so a ``gh.cmd`` /
  ``git.cmd`` launcher (scoop, npm, corporate shims) is invisible to it although
  every shell finds it. ``shutil.which`` honors PATHEXT.
* A bare name is searched in the CURRENT DIRECTORY first, and these scripts run
  inside a checked-out repository, so a checkout carrying its own ``gh.exe``
  would win the lookup. ``which`` is therefore asked about each ABSOLUTE PATH
  entry by name, never about the bare name, which keeps both the current
  directory and relative PATH entries out of the search.

Launching a ``.cmd``/``.bat`` file makes cmd.exe the real parser of the argument
list, and cmd.exe expands metacharacters inside arguments (CVE-2024-24576,
"BatBadBut"). Arguments here carry values read from GitHub and the checkout
(branch names, jq programs, PR bodies), so when the resolved target is a batch
launcher, any argument holding a cmd.exe metacharacter is refused rather than
passed through.
"""

import os
import shutil

# What cmd.exe expands inside an argument when it runs a .cmd/.bat file.
CMD_META = '^&|<>%!"\r\n'

# Shell conventions for "could not run": 127 not found, 126 refused to execute.
RC_NOT_FOUND = 127
RC_REFUSED = 126


def which(name):
    """``name`` from PATH's absolute entries only (PATHEXT applied), or None.

    PATHEXT is applied here: before Python 3.12, ``shutil.which`` adds no
    PATHEXT suffix to a name with a directory part.
    """
    names = [name]
    if os.name == "nt":
        exts = [e for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
        if not name.lower().endswith(tuple(e.lower() for e in exts)):
            names = [name + e for e in exts]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.isabs(entry):
            for candidate in names:
                found = shutil.which(os.path.join(entry, candidate))
                if found:
                    return found
    return None


def resolve(args):
    """Vet a command before it is spawned.

    Returns ``(argv, None)`` with argv[0] replaced by the resolved executable, or
    ``(None, (rc, "", message))`` -- the ``(returncode, stdout, stderr)`` shape
    every runner returns -- when it must not be spawned: rc 127 when argv[0] is
    not found on PATH's absolute entries, rc 126 when it resolves to a batch
    launcher and an argument carries a cmd.exe metacharacter. An argv[0] that
    already names a path (it contains a directory part) is used as given; only
    a bare name is looked up.
    """
    name = str(args[0])
    exe = name if os.path.dirname(name) else which(name)
    if exe is None:
        return None, (RC_NOT_FOUND, "", "{}: not found on PATH".format(name))
    rest = [str(a) for a in args[1:]]
    if exe.lower().endswith((".cmd", ".bat")) and any(ch in a for a in rest for ch in CMD_META):
        return None, (
            RC_REFUSED,
            "",
            "{}: refusing batch-file launch with cmd.exe metacharacters in arguments".format(name),
        )
    return [exe] + rest, None
