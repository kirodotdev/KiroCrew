#!/usr/bin/env python3
"""Vendor-API fixtures with provenance, plus shape conformance.

A wire fixture is a *claim about someone else's API*. Written as a bare Python
literal it is indistinguishable from a guess, so a shape nobody ever verified
guards the whole stack with the same authority as one captured from the live
vendor. That is how the iLink QR bug survived a green suite: the code assumed
``qrcode_img_content`` was image bytes, the fixtures agreed with the code, and
nothing recorded that the assumption had never been checked.

This module makes the claim explicit and testable:

* every fixture carries a ``_provenance`` block naming its
  :class:`Source` — ``live_probe`` (observed against the real API),
  ``vendor_doc`` (transcribed from published docs), or ``assumed`` (our
  inference, unverified);
* :func:`shape_of` reduces a payload to a key/type skeleton, and
  :func:`assert_same_shape` diffs two skeletons — so a live response can be
  compared against a stored fixture without pinning volatile values
  (tokens, ids, timestamps);
* :func:`unverified` enumerates every ``assumed`` fixture, so the gap is a
  visible inventory rather than an unknown.

The fixtures ROOT is always supplied by the caller: this module ships in the
runtime wheel, where no ``test/`` tree exists, so a default derived from
``__file__`` would point at an unpackaged path for every installed consumer.
``channel`` and ``name`` are validated as single path components before any
filesystem access, so a fixture identifier can never escape that root.

Refresh workflow (the only way a fixture should change):

1. Run the endpoint against the real API with real credentials, authorized.
2. Save the observed body via :func:`write_fixture` with
   ``source=Source.LIVE_PROBE`` and a reference (date, request id, probe
   script). ``write_fixture`` refuses to silently downgrade a ``live_probe``
   fixture to ``assumed``.
3. Re-run the suite. Every layer above is now verified against the observed
   shape; a vendor change is a one-file edit that re-verifies the stack.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple, Union

__all__ = [
    "Source",
    "Provenance",
    "Fixture",
    "ShapeMismatch",
    "fixtures_root",
    "load_fixture",
    "write_fixture",
    "iter_fixtures",
    "unverified",
    "shape_of",
    "assert_same_shape",
]

_PROVENANCE_KEY = "_provenance"


class Source(str, Enum):
    """How much authority a fixture's shape carries."""

    LIVE_PROBE = "live_probe"
    """Captured from the real vendor API. The only self-justifying source."""

    VENDOR_DOC = "vendor_doc"
    """Transcribed from published vendor documentation. Docs drift; weaker."""

    ASSUMED = "assumed"
    """Our own inference. Guards regressions but proves nothing about the
    vendor -- these are what :func:`unverified` reports."""


@dataclass(frozen=True)
class Provenance:
    source: Source
    reference: str = ""
    """Where it came from: probe script + date, doc URL + section, or why assumed."""
    captured_at: str = ""
    """ISO-8601 date of capture, so staleness is visible."""
    note: str = ""

    @property
    def is_verified(self) -> bool:
        return self.source in (Source.LIVE_PROBE, Source.VENDOR_DOC)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.value,
            "reference": self.reference,
            "captured_at": self.captured_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Provenance":
        try:
            source = Source(raw["source"])
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"fixture provenance must name a known source "
                f"{[s.value for s in Source]}, got {raw.get('source')!r}"
            ) from exc
        return cls(
            source=source,
            reference=str(raw.get("reference", "")),
            captured_at=str(raw.get("captured_at", "")),
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class Fixture:
    channel: str
    name: str
    payload: Any
    provenance: Provenance
    path: Optional[Path] = None

    @property
    def is_verified(self) -> bool:
        return self.provenance.is_verified


class ShapeMismatch(AssertionError):
    """A live response no longer matches the recorded fixture's shape."""


def _safe_component(value: str, kind: str) -> str:
    """Validate a single path component before it touches the filesystem.

    ``channel`` and ``name`` are interpolated into a path, so an unvalidated
    ``"../outside"`` (or any separator-bearing name) would let a caller read or
    OVERWRITE files outside the fixture root. Everything here must be one plain
    component: no separators, no ``.``/``..``, no absolute paths, no NUL.
    """
    if not value or not isinstance(value, str):
        raise ValueError(f"fixture {kind} must be a non-empty string, got {value!r}")
    # Windows silently STRIPS trailing dots and spaces, so ".. " and "..." are
    # both ".." by the time the filesystem sees them, and "foo." is "foo".
    # Compare against the stripped form so the traversal check cannot be evaded
    # by padding, and reject the padding outright since it never means anything
    # useful in a fixture identifier.
    if value != value.strip(". ") and value.strip(". ") in ("", ".", ".."):
        raise ValueError(f"fixture {kind} must not be a padded dot segment, got {value!r}")
    if value != value.rstrip(". "):
        raise ValueError(
            f"fixture {kind} must not end in a dot or space (Windows strips them), "
            f"got {value!r}"
        )
    if value in (".", ".."):
        raise ValueError(f"fixture {kind} must not be {value!r}")
    if "\x00" in value:
        raise ValueError(f"fixture {kind} must not contain a NUL byte")
    # Reject both separators on every OS, not just the local one, so a fixture
    # name authored on POSIX cannot escape when the suite runs on Windows.
    if "/" in value or "\\" in value or os.sep in value or (os.altsep and os.altsep in value):
        raise ValueError(f"fixture {kind} must be a single path component, got {value!r}")
    # A BARE DRIVE ("C:") survives every check above -- Path("C:").parts is
    # ("C:",) and is_absolute() is False -- but ``root / "C:"`` discards the root
    # entirely on Windows and yields a drive-relative path. Checked via
    # PureWindowsPath so the rejection also holds when authoring on POSIX,
    # matching the cross-OS intent of the separator check above.
    if PureWindowsPath(value).drive:
        raise ValueError(f"fixture {kind} must not carry a drive, got {value!r}")
    if Path(value).is_absolute() or len(Path(value).parts) != 1:
        raise ValueError(f"fixture {kind} must be a single relative component, got {value!r}")
    return value


def _resolve_within(root: Union[Path, str], *parts: str) -> Path:
    """Join ``parts`` under ``root`` and prove the result stays inside it.

    Component validation alone is not containment: a pre-existing SYMLINK at
    ``<root>/<channel>`` points wherever it likes while every component looks
    innocent. So the nearest on-disk ancestor is canonicalized and checked
    against the canonical root before any read, mkdir or write happens.

    Two traps this deliberately avoids:

    * ``Path.exists()`` FOLLOWS symlinks, so it reports False for a DANGLING
      link -- which ``open()`` would still happily follow. The walk therefore
      tests ``os.path.lexists`` (the link itself), not ``exists``.
    * The walk stops at ``base`` rather than climbing past it. A root that does
      not exist yet is normal (``write_fixture`` creates it), and climbing above
      it would compare some unrelated ancestor and reject a legitimate write.
      If nothing between ``base`` and the target exists, nothing can redirect
      us, so the lexical join is already contained.
    """
    base = Path(root).resolve()
    target = base.joinpath(*parts)
    probe = target
    while probe != base and not os.path.lexists(probe):
        probe = probe.parent
    if probe == base:
        # Nothing exists between the root and the target -- no symlink can be
        # in the way. (A missing root is the caller's own trusted directory.)
        return target
    real = probe.resolve()
    if real != base and base not in real.parents:
        raise ValueError(
            f"fixture path escapes the root: {'/'.join(parts)!r} resolves outside "
            f"{base} (via {real}) -- a symlinked fixture path is not honoured"
        )
    return target


def fixtures_root(root: Union[Path, str]) -> Path:
    """Normalize an explicitly supplied fixtures root.

    There is deliberately NO default. ``kiro_crew.testing`` ships inside the
    runtime wheel, where no ``test/`` tree exists, so any default computed from
    ``__file__`` would resolve to a path that is not packaged and fail for every
    installed consumer. The caller owns the layout: repo suites pass their own
    ``test/fixtures/channels``, downstream consumers pass theirs.
    """
    return Path(root)


def load_fixture(channel: str, name: str, *, root: Union[Path, str]) -> Fixture:
    _safe_component(channel, "channel")
    _safe_component(name, "name")
    path = _resolve_within(root, channel, f"{name}.json")
    if not path.is_file():
        raise FileNotFoundError(f"no wire fixture at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or _PROVENANCE_KEY not in raw:
        raise ValueError(
            f"{path} has no {_PROVENANCE_KEY!r} block -- an unattributed fixture "
            f"is an unverifiable claim about the vendor's API"
        )
        # (fail closed: no provenance means we cannot tell verified from guessed)
    body = {k: v for k, v in raw.items() if k != _PROVENANCE_KEY}
    return Fixture(
        channel=channel,
        name=name,
        payload=body.get("payload", body),
        provenance=Provenance.from_dict(raw[_PROVENANCE_KEY]),
        path=path,
    )


def write_fixture(
    channel: str,
    name: str,
    payload: Any,
    provenance: Provenance,
    *,
    root: Union[Path, str],
    allow_downgrade: bool = False,
) -> Path:
    """Persist a fixture. Refuses to weaken an existing fixture's provenance.

    Downgrading ``live_probe`` -> ``assumed`` would silently convert observed
    truth into a guess, so it requires ``allow_downgrade=True``.
    """
    _safe_component(channel, "channel")
    _safe_component(name, "name")
    # Containment is proven BEFORE mkdir: a symlinked <root>/<channel>
    # would otherwise be created/followed and the write would land outside.
    path = _resolve_within(root, channel, f"{name}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not allow_downgrade:
        existing = load_fixture(channel, name, root=root)
        if existing.provenance.is_verified and not provenance.is_verified:
            raise ValueError(
                f"refusing to downgrade {path.name} from "
                f"{existing.provenance.source.value} to {provenance.source.value}; "
                f"pass allow_downgrade=True if this is deliberate"
            )
    doc = {_PROVENANCE_KEY: provenance.to_dict(), "payload": payload}
    _write_no_follow(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return path


def _write_no_follow(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, refusing to follow a symlink at the leaf.

    ``_resolve_within`` proves containment at CHECK time; a symlink planted
    between that check and this write would still be followed by an ordinary
    ``write_text``. ``O_NOFOLLOW`` closes that race at the syscall: if the leaf
    is a symlink, ``open`` fails instead of writing through it.

    ``O_NOFOLLOW`` is POSIX-only, so it degrades to 0 on Windows -- where the
    containment check remains the guard. Kept narrow deliberately: this is the
    one place the module writes.

    Known residual (accepted, not exploitable here): the guard covers the LEAF
    only. A concurrent actor could still swap an intermediate directory for a
    symlink between the containment check and ``mkdir``/``open``. Closing that
    would need descriptor-relative traversal (``openat`` with
    ``O_DIRECTORY|O_NOFOLLOW`` per component) and has no portable Windows
    equivalent. It buys nothing against this module's actual threat model: the
    root comes from test code, never from a model or remote input, the fixture
    is read back by the same test process, and an actor who can plant a symlink
    inside the fixture tree mid-run can simply edit the test instead.

    Ownership of the descriptor transfers exactly ONCE: if ``fdopen`` raises we
    still own the raw fd and must close it; once it succeeds the file object owns
    it exclusively. Closing in both places would double-close the same NUMBER,
    and another thread could have reused it in between -- shutting an unrelated
    file or socket.

    The write is ATOMIC: bytes land in a temporary sibling that is fsynced and
    then ``os.replace``d over ``path``. Opening ``path`` directly with
    ``O_TRUNC`` would destroy the existing fixture at OPEN time, so any later
    failure -- a full disk, a broken pipe -- would leave a truncated file and
    lose a payload that may have cost a credentialled live probe to capture.
    Since the module already refuses to weaken a fixture's provenance, letting a
    failed write erase its CONTENT would protect the claim and discard the
    evidence. Either the old bytes survive intact or the new ones replace them
    whole; there is no in-between state a reader can observe.

    ``os.replace`` also cannot write THROUGH a symlink at ``path`` -- it swaps
    the directory entry itself -- so the containment guarantee holds regardless
    of the pre-check below. The explicit ``is_symlink`` check is kept so a
    symlinked leaf is REPORTED rather than silently replaced.
    """
    if path.is_symlink():
        raise ValueError(
            f"refusing to write fixture through a symlink at {path.name}"
        )
    # Same directory as the target, so the containment already proven for
    # ``path`` covers the temporary too. O_EXCL means a pre-planted file (or
    # symlink) at the temporary name is refused rather than reused.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)  # fdopen never took ownership
        _unlink_quietly(tmp)
        raise
    try:
        with handle:  # the file object now owns the fd exclusively
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _unlink_quietly(tmp)  # never leave a partial sibling behind
        raise
    os.replace(tmp, path)


def _unlink_quietly(path: Path) -> None:
    """Best-effort cleanup of a temporary that is already being abandoned.

    Called only from an ``except`` path, so it must never mask the original
    exception with a secondary failure of its own.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


def iter_fixtures(*, root: Union[Path, str]) -> List[Fixture]:
    base = fixtures_root(root)
    if not base.is_dir():
        return []
    out: List[Fixture] = []
    for path in sorted(base.glob("*/*.json")):
        out.append(load_fixture(path.parent.name, path.stem, root=root))
    return out


def unverified(*, root: Union[Path, str]) -> List[Fixture]:
    """Every fixture whose shape nobody has confirmed against the vendor."""
    return [f for f in iter_fixtures(root=root) if not f.is_verified]


# -- shape comparison ---------------------------------------------------------
def shape_of(value: Any) -> Any:
    """Reduce a payload to a key/type skeleton.

    Values are discarded so volatile data (tokens, ids, timestamps) never makes
    a conformance check flaky; only structure and types are compared. Lists
    collapse to the union of their element shapes, so a 1-element sample and a
    50-element response compare equal when the elements agree.
    """
    if isinstance(value, dict):
        return {k: shape_of(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        seen: List[Any] = []
        for item in value:
            s = shape_of(item)
            if s not in seen:
                seen.append(s)
        return ["|".join(_render(s) for s in seen)] if seen else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "str"


def _render(shape: Any) -> str:
    return json.dumps(shape, sort_keys=True) if isinstance(shape, (dict, list)) else str(shape)


def _flatten(shape: Any, prefix: str = "") -> Dict[str, str]:
    flat: Dict[str, str] = {}
    if isinstance(shape, dict):
        for k, v in shape.items():
            flat.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    else:
        flat[prefix or "<root>"] = _render(shape)
    return flat


def assert_same_shape(expected: Any, actual: Any, *, context: str = "") -> None:
    """Assert two payloads share a key/type skeleton, reporting a precise diff.

    Intended for fixture-vs-live conformance: run the real endpoint, pass the
    stored fixture as ``expected``, and any vendor drift surfaces as named
    missing / extra / retyped fields instead of an opaque inequality.
    """
    exp_flat = _flatten(shape_of(expected))
    act_flat = _flatten(shape_of(actual))
    missing = sorted(set(exp_flat) - set(act_flat))
    extra = sorted(set(act_flat) - set(exp_flat))
    retyped: List[Tuple[str, str, str]] = [
        (k, exp_flat[k], act_flat[k])
        for k in sorted(set(exp_flat) & set(act_flat))
        if exp_flat[k] != act_flat[k]
    ]
    if not (missing or extra or retyped):
        return
    lines = [f"wire shape drifted{f' [{context}]' if context else ''}:"]
    if missing:
        lines.append(f"  fields the fixture expects but the response lacks: {missing}")
    if extra:
        lines.append(f"  fields the response added (fixture is stale): {extra}")
    for key, want, got in retyped:
        lines.append(f"  {key}: fixture says {want}, response has {got}")
    raise ShapeMismatch("\n".join(lines))
