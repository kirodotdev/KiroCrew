"""Which files a KAS agent's ``file://`` prompt may be read from.

On KAS, Crew reads a ``file://`` prompt itself and ships the text over the wire
(:func:`kiro_crew.acp.kas_agents.resolve_prompt`), so the path a spec names
decides which file's contents reach the engine and the model. This module is the
one answer to "may this resolved path be inlined", and it is the same answer for
a relative path and an absolute one: a relative ``file://`` is anchored to the
agents directory and resolved, symlinks included, before it is asked.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.security import is_sensitive_path

#: Pseudo-filesystems whose contents are process/kernel state, not documents.
#: ``/proc/<pid>/environ`` is the sharp edge: the gateway's own environment.
_PSEUDO_FS_ROOTS = ("/proc", "/sys", "/dev")

#: ``$HOME``-relative credential stores a prompt read must not reach that
#: ``is_sensitive_path`` leaves readable to the agent's own file tools. ``gh``
#: keeps its OAuth token in ``hosts.yml`` here. Fenced for this reader only: a
#: prompt is shipped off the host, a file the agent's tools read is not, so this
#: list widens nothing the sandbox or the tool gate decides.
_PROMPT_ONLY_HOME_SECRETS = (".config/gh",)

#: Data-home leaves the prompt read refuses, also outside ``is_sensitive_path``.
#: The receipt-only HMAC key is ``apps.install_receipt._SECRET_FILE``; the
#: per-token identity mappings are ``session_token_<sha256>.sig``
#: (``session_token_sig._sig_path``). Both sit directly in the data home.
_PROMPT_ONLY_DATA_HOME_FILES = ("app_receipt_secret",)
_SESSION_TOKEN_SIG_PREFIX = "session_token_"
_SESSION_TOKEN_SIG_SUFFIX = ".sig"


def _under(posix: str, root: str) -> bool:
    return posix == root or posix.startswith(root + "/")


def _anchors(paths: list[Path]) -> set[str]:
    """Each of *paths* both as written and resolved, casefolded.

    The candidate is resolved, so an anchor must be too: a symlinked
    ``~/.config/gh`` (a dotfile manager's) or data home would otherwise compare a
    canonical target against a link's own name. The
    lexical form is kept for an anchor whose resolution fails.
    """
    out: set[str] = set()
    for p in paths:
        out.add(p.as_posix().casefold())
        try:
            out.add(p.resolve().as_posix().casefold())
        except (OSError, RuntimeError):
            pass
    return out


def _data_homes() -> set[str]:
    """The data home as configured and at its default."""
    from kiro_crew.config.paths import CONFIG_DIR_LEAF, KIRO_BASE_DIR_NAME, config_dir

    return _anchors([Path.home() / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF, config_dir()])


def _is_data_home_secret(folded: str) -> bool:
    parent, _, name = folded.rpartition("/")
    if parent not in _data_homes():
        return False
    if name in _PROMPT_ONLY_DATA_HOME_FILES:
        return True
    return name.startswith(_SESSION_TOKEN_SIG_PREFIX) and name.endswith(_SESSION_TOKEN_SIG_SUFFIX)


def is_unsafe_prompt_path(path: Path) -> bool:
    """True if the RESOLVED *path* must not be read and inlined into a KAS prompt.

    *path* must already be resolved (``Path.resolve``), so a symlink is judged by
    its target. Blocks the credential and governance locations
    ``is_sensitive_path`` knows (which also re-resolves), the pseudo-filesystems,
    the prompt-only credential stores in :data:`_PROMPT_ONLY_HOME_SECRETS`, and
    the data-home receipt key and session-token mappings.
    """
    if is_sensitive_path(str(path)):
        return True
    folded = path.as_posix().casefold()
    roots = [Path(r) for r in _PSEUDO_FS_ROOTS]
    roots += [Path.home() / rel for rel in _PROMPT_ONLY_HOME_SECRETS]
    if any(_under(folded, anchor) for anchor in _anchors(roots)):
        return True
    return _is_data_home_secret(folded)
