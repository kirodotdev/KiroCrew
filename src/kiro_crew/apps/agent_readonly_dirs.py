"""Directories under an app's OWN data directory that agents may read and never write.

An app that keeps state an agent must not rewrite (a user's own notes, a record of
decisions already answered) declares the directories in ``app.json``::

    "agentReadOnlyDirs": ["notes", "trust"]

Each entry is a path relative to the app's data directory,
``<data home>/apps/<name>/data/``, so it names ``apps/<name>/data/<entry>``. Two layers
then fence it from every agent, the same two a core write-protected crew-home leaf has:

* **The file-edit tool gate.** ``security.paths.sensitive_write_path_refusal`` refuses an
  agent tool's write under every declared directory, at both data-home prefixes and a
  relocated home, exactly as it refuses one under a ``_WRITE_PROTECTED_HOME_PATHS`` row.
  Reads stay open, as for that row.
* **The OS sandbox, in every mode.** The sandbox seals each declared directory read-only
  as it seals a core ``READONLY`` crew-home leaf: a read-only bind on Linux (made first
  when absent, so the seal is never vacuous), a write and link deny on Seatbelt, with every
  parent guarded against rename on both. A symlink at the directory, or at a component
  between the data home and it, refuses the spawn, because a seal would land on whatever
  the link reaches while the name stays replaceable.

The app's own code keeps write access: an in-gateway backend is outside every sandbox, and
a spawned backend gets its declared directories back from
``sandbox_tiers.app_declared_readonly_targets``, from the manifest of the code it runs.

**Tightening only.** A declaration can add a fence and never remove one. It uses the
grammar, sources, name-from-location rule and retained record of
:mod:`kiro_crew.apps.credential_masks` (the same engine, under this key), so no entry can
leave its app's data directory or reach another app's. The backend's lift drops every
core leaf of any disposition and anything beneath another mask, so a declaration cannot
lift a core fence. The retained record, ``apps/.<name>-agent-readonly-dirs.json``, keeps
the fence on data a default uninstall leaves behind and on the data an update holds
aside in ``apps/.<name>-data-tmp/``.

The residual is the credential masks' one: the installed tree is sandbox-writable, so an
agent that rewrites an installed app's ``app.json`` drops its declaration from the next
spawn.

Process state only (the shared engine's parsed-manifest cache); no per-caller data.
"""

from __future__ import annotations

from kiro_crew.apps import credential_masks

#: The ``app.json`` key.
MANIFEST_KEY = "agentReadOnlyDirs"
#: The retained declaration beside the app directory is ``.<name>`` plus this.
RECORD_SUFFIX = "-agent-readonly-dirs.json"
#: This declaration, as the shared engine reads it.
KIND = credential_masks.DeclarationKind(MANIFEST_KEY, RECORD_SUFFIX)


def declaration_errors(value: object) -> list[str]:
    """Validation errors for a manifest's ``agentReadOnlyDirs`` value (``None`` is fine)."""
    return credential_masks.declaration_errors(value, kind=KIND)


def mask_conflict_errors(dirs: object, masks: object) -> list[str]:
    """Errors for ``agentReadOnlyDirs`` entries at or beneath a ``credentialMasks`` file.

    A credential mask names a FILE, and the sandbox creates an absent read-only directory
    so it can seal it, so such an entry would put a directory where the mask expects a
    file and refuse every sandboxed spawn. A directory that holds a masked file is fine:
    the seal is placed first and the mask on top of it.
    """
    files = valid_entries(masks)
    return [
        f"{MANIFEST_KEY}[{index}] must not be or sit beneath a credentialMasks file ({mask})"
        for index, entry in enumerate(valid_entries(dirs))
        for mask in files
        if entry == mask or entry.startswith(mask + credential_masks.ENTRY_SEPARATOR)
    ]


def valid_entries(value: object) -> tuple[str, ...]:
    """The admissible entries of *value*, deduplicated in declaration order."""
    return credential_masks.valid_entries(value)


def declared_dirs(*, builtin: bool = True, installed: bool = True) -> dict[str, tuple[str, ...]]:
    """Every app's declared read-only directories on this host, ``{app name: entries}``."""
    return credential_masks.declared_masks(builtin=builtin, installed=installed, kind=KIND)


def declared_crew_leaves() -> tuple[str, ...]:
    """Every declared directory as a crew-home-relative leaf, at ``data/`` and staged aside."""
    return credential_masks.declared_crew_leaves(kind=KIND)


def declared_data_leaves() -> tuple[str, ...]:
    """Every declared directory as a crew-home-relative leaf under its app's ``data/`` only."""
    return tuple(
        sorted(
            {
                credential_masks.crew_leaf(app, entry)
                for app, entries in declared_dirs().items()
                for entry in entries
            }
        )
    )


def declared_crew_leaves_for(app: str, *, builtin: bool) -> tuple[str, ...]:
    """*app*'s declared directories as crew-home-relative leaves, from ONE manifest source."""
    return credential_masks.declared_crew_leaves_for(app, builtin=builtin, kind=KIND)


def write_protected_home_paths() -> tuple[str, ...]:
    """Every declared directory as ``$HOME``-relative tool-gate rows, one per data-home prefix.

    The shape of a ``_WRITE_PROTECTED_HOME_PATHS`` row, so the gate anchors and
    re-anchors them (``KIROCREW_HOME``) exactly as it does a core row.
    """
    # Imported at call time, so loading this module never loads the security package.
    from kiro_crew.security.paths_lists import _CREW_HOME_PREFIXES

    return tuple(
        f"{prefix}/{leaf}" for leaf in declared_crew_leaves() for prefix in _CREW_HOME_PREFIXES
    )


def remember_declaration(app: str) -> None:
    """Make *app*'s retained ``agentReadOnlyDirs`` record match its installed ``app.json``.

    :func:`kiro_crew.apps.credential_masks.remember_declaration` under this key: raises
    ``OSError`` when the record cannot be written or removed.
    """
    credential_masks.remember_declaration(app, kind=KIND)
