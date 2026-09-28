"""Where credentials live: the name, location and standalone path fences.

Each predicate judges a PATH, before anything at it is read. ``refused_by_name`` and
``refused_by_location`` are this builder's own checks; ``_looks_sensitive_standalone`` is the
coarse floor checked in ADDITION to ``kiro_crew.security.is_sensitive_path``, never instead of
it, and it must never be stricter than that shared validator.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

#: Sensitive locations, for the standalone case where ``kiro_crew.security`` is not
#: importable. Ported from ``security/paths.py:_SENSITIVE_HOME_DIRS``.
#:
#: This list exists because the alternative was worse. A fence conditional on an import is skipped
#: entirely when the import failed, on the reasoning that reading the agent spec is the
#: tool's whole purpose so refusing would make standalone mode unusable. That reasoning
#: holds for refusing, and does not hold for skipping: it made the fence conditional on
#: an import, so standalone mode was the ONE mode where a sensitive --source was read
#: and bundled. A second, coarser list is the same trade the credential scanner in ``scan``
#: makes with ``_HARD_PATTERNS``, and it is checked in ADDITION to the shared question, never
#: instead of it.
#: Note what is NOT here: ``.kiro/agents``. Upstream lists it under
#: ``_WRITE_PROTECTED_HOME_PATHS``, not the read-sensitive set, because the protection is
#: against WRITING a spec whose ``mcpServers.<name>.command`` the gateway would then exec.
#: ``is_sensitive_path("~/.kiro/agents/frontdesk.json")`` returns False, and this build only
#: reads. Including it made every run without ``--source`` refuse its own crew, since
#: ``~/.kiro`` IS the default source and the spec lives at ``~/.kiro/agents/<name>.json``.
#: The local list must never be STRICTER than the shared validator; a test pins that. That is
#: why ``.codex/auth.json`` is the token LEAF and not the ``.codex`` directory: the shared
#: validator classifies the leaf and returns False for the sibling config, so fencing the
#: whole directory here would refuse a path the rest of the tree reads.
_SENSITIVE_RELATIVE_DIRS = (
    ".aws",
    ".azure",
    ".claude/.credentials.json",
    ".codex/auth.json",
    ".config/gcloud",
    ".config/goose/secrets.yaml",
    ".docker/config.json",
    ".dsh/.credentials.yaml",
    ".git-credentials",
    ".gnupg",
    ".gpg",
    ".kiro/crew-auth-staging",
    ".kube/config",
    ".local/share/amazon-q",
    ".local/share/kiro-cli",
    ".local/share/opencode/auth.json",
    ".netrc",
    ".npmrc",
    ".pi/agent/auth.json",
    ".pypirc",
    ".ssh",
)


def _looks_sensitive_standalone(path_posix: str) -> bool:
    """Coarse fence for the standalone case: does any COMPONENT name a credential store?

    Component-wise rather than substring, so ``~/projects/sshconfig-notes`` is not caught
    by ``.ssh`` and ``~/.ssh/id_rsa`` is. Two-part entries are matched as consecutive
    components for the same reason.

    Deliberately coarser than the real predicate, which also resolves links. It
    is a floor for a mode that had NO floor, not a replacement -- when the shared
    validator is importable, both run.
    """
    # ``path_posix`` is already POSIX-form (the caller passes ``.as_posix()``), so its
    # components are parsed with ``PurePosixPath`` rather than a raw ``"/"`` split: same
    # result, and it reads as POSIX-string parsing rather than OS-path assembly (the
    # cross-platform gate flags a bare ``split("/")`` as the latter).
    parts = [part for part in PurePosixPath(path_posix).parts if part not in ("", ".", "/")]
    # ``casefold()``, not ``lower()``. Windows paths are case-insensitive, so ``~/.AWS``
    # names the same directory as ``~/.aws`` and must be caught; and casefold is what the
    # shared validator uses (``security/paths.py`` casefolds every anchored entry), so
    # ``lower()`` here would be a SECOND, weaker rule for the same question. The two
    # differ on real input: Turkish dotless i and the German sharp s both fold to forms
    # ``lower()`` leaves alone.
    folded = [part.casefold() for part in parts]
    for entry in _SENSITIVE_RELATIVE_DIRS:
        wanted = list(PurePosixPath(entry.casefold()).parts)
        span = len(wanted)
        for start in range(len(folded) - span + 1):
            if folded[start : start + span] == wanted:
                return True
    # The dir list above catches a credential STORE by directory (``~/.ssh/id_rsa``); it does
    # not catch a credential FILE by name in an ordinary directory (``~/.kiro/crew/.env``).
    # ``.env`` is the keystone leaf the security model protects, and the shared validator
    # catches it by name -- so the floor must too, or a standalone ``--allow`` of it reads
    # open whenever the shared validator is unavailable. Apply the same credential-name rule
    # the scan uses, to the final component.
    #
    # EXCEPT the crew spec leaf ``agents/<name>.json``. That basename is the operator's crew
    # name, which they choose freely -- a crew named ``credentials`` or ``client_secret`` is
    # legitimate, and its spec is not a credential file. The shared validator agrees: it
    # returns False for ``~/.kiro/agents/<name>.json`` because that is where the spec lives.
    # Applying the credential-name rule to that leaf would make the floor STRICTER than the
    # validator and refuse a legitimately named crew's own spec, so the leaf directly under an
    # ``agents`` directory ending ``.json`` is exempt from the name rule (the directory rules
    # above still run, and a non-``.json`` credential leaf under ``agents`` is still caught).
    if parts and _CREDENTIAL_NAME_RE.match(parts[-1]):
        is_crew_spec_leaf = (
            len(parts) >= 2 and folded[-2] == "agents" and parts[-1].casefold().endswith(".json")
        )
        if not is_crew_spec_leaf:
            return True
    return False


# Filenames that are credential stores by convention, matched before any read.
# Ported verbatim from ``crew_export/scan.py:_CREDENTIAL_NAME_RE``.
_CREDENTIAL_NAME_RE = re.compile(r"""(?ix)
    ^(
        \.env(\..*)?
      | .*\.pem
      | .*\.p12
      | .*\.pfx
      | .*\.key
      | id_(rsa|dsa|ecdsa|ed25519)(\.pub)?
      | \.npmrc
      | \.netrc
      | \.pgpass
      | \.pypirc
      | \.git-credentials
      | credentials(\.json)?
      | client_secret.*\.json
      | service[-_]account.*\.json
      | .*\.kdbx
      | \.htpasswd
    )$
    """)


def refused_by_name(path: Path) -> bool:
    """True when a path is a credential store by its name alone.

    A ``.pem`` that happens not to match a content regex is still a private key,
    so the name is judged before the bytes are read.
    """
    return bool(_CREDENTIAL_NAME_RE.match(path.name))


# Credential DIRECTORIES denied as a path component at any depth. Mirrored from
# ``kiro_crew.security.DENIED_ROOT_PARTS`` (security.py:8254), which denies these
# names "at any depth and covers those two dirs [``.kube``/``.docker``] whole" --
# a superset of the ``.kube/config`` and ``.docker/config.json`` leaves pinned in
# ``_SENSITIVE_HOME_DIRS``. It is MIRRORED rather than imported on purpose:
# importing ``kiro_crew.security`` here would drag in ``kiro_crew.executors``,
# ``kiro_crew.sel`` and more, none of which are importable in this app's
# deployment venv (boto3 / fastapi / pydantic / pytest only -- see the ``packaging.build``
# module docstring and the ``_HARD_PATTERNS`` note in ``scan``). So the guard would pass in a dev
# venv and fail at real packaging time, or pull the whole framework into the
# packager. This is a five-name set, not a large denylist, which is the
# narrowest-equivalent the track brief asks for.
#: Stored CASEFOLDED, because the membership test below folds each component before
#: comparing. Windows paths are case-insensitive, so ``~/.AWS/credentials`` names the same
#: file as ``~/.aws/credentials`` and a set of lowercase literals compared against raw
#: components misses it. The second such predicate in this module; the other one folds
#: too, and they must not disagree about what counts as a credential directory.
_CREDENTIAL_DIR_PARTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})


def refused_by_location(path: Path) -> bool:
    """True when a path lies inside a known credential directory.

    ``refused_by_name`` catches a store named like one (``id_rsa``, ``*.pem``); it
    does NOT catch ``~/.kube/config``, whose basename ``config`` is innocent. A
    kubeconfig's ``client-certificate-data`` is base64 and may match no credential
    pattern, so the ``scan_text`` after the read cannot be relied on to catch it --
    and reading a file the repo already fences off is the wrong shape regardless
    of what the scanner would then find. Judge the location before the read.
    """
    return any(part.casefold() in _CREDENTIAL_DIR_PARTS for part in path.parts)
