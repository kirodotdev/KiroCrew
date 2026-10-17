"""Where the ``imsg`` bridge binary is allowed to come from.

The channel spawns a child process, so *who chooses that executable* is a
security boundary rather than a preference, and the invariant is absolute:
**nothing an agent can write may influence which binary runs.**

That rules out two sources, not one:

* **Configuration.** ``config.json`` is agent-writable -- ``security.py`` says so
  in as many words, and records that this is why the computer-use enable and the
  denied-command opt-out live on the KEYSTONE floor instead. A settable
  ``cli_path`` would let any auto-approved agent shell write a payload path and
  have the gateway execute it, outside the agent sandbox and with the gateway's
  privileges, on the next restart.
* **``PATH``.** Same outcome by a different route: an agent that can write to any
  directory on the gateway's ``PATH`` (``~/.local/bin`` is writable and on it for
  a normal install) can drop an ``imsg`` there and have it win the lookup. The
  tempting argument -- that ``PATH`` is already trusted for ``git``, ``gh`` and
  ``npm`` -- does not carry here: those tools have no fixed install location, so
  ``PATH`` is unavoidable for them. This binary has exactly two known locations,
  so a ``PATH`` lookup buys nothing and adds a writable surface.

Resolution is therefore a fixed, source-level list. Adding an entry is a decision
a reviewer makes in a diff, never something the runtime can be talked into.

The list is also what makes the launch-agent case work, which is the deployment
that matters most: under a launch agent the gateway inherits a minimal ``PATH``
with no Homebrew prefix, so a ``PATH`` lookup would have failed there anyway.
Both prefixes are covered -- ``/opt/homebrew`` on Apple Silicon, ``/usr/local``
on Intel -- which is where ``brew install steipete/tap/imsg`` puts it.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_BINARY = "imsg"

# Standard install locations, tried in order. Absolute and fixed at source level:
# an entry here is a decision made by a reviewer, not by whatever happens to be
# in a config file or on PATH at runtime.
TRUSTED_BRIDGE_PATHS: tuple[str, ...] = (
    "/opt/homebrew/bin/imsg",
    "/usr/local/bin/imsg",
)


def resolve_bridge_path() -> str:
    """Return the bridge executable to spawn, or ``""`` when none is installed.

    An empty return is the "not installed" signal the readiness surface renders
    as *needs setup*. There is deliberately no fallback beyond this list: no
    caller-supplied string, no config value, and no ``PATH`` search.
    """
    for candidate in TRUSTED_BRIDGE_PATHS:
        path = Path(candidate)
        try:
            if path.is_file():
                return str(path)
        except OSError:
            # An unreadable or unstattable candidate is simply not a hit; the
            # next one, or the "not installed" answer, is still correct.
            continue
    return ""
