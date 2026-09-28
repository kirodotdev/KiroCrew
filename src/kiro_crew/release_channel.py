"""Which release channel is this build on — the one Python answer.

Three surfaces need it and MUST agree, because a disagreement is silent:

* ``diagnostics.py`` tags a bug report with ``channel: <lane>`` so triage can
  filter prerelease reports as a group.
* ``dashboard/state.py`` ships it in the status payload, so the dashboard can
  show a prerelease user an obvious way to report a bug.
* ``.github/workflows/issue-triage.yml`` maps the issue form's answer to the
  same label vocabulary.

Deliberately a MODULE OF ITS OWN rather than a helper inside ``diagnostics``:
the status payload is built on the hot path and must not import the diagnostics
collector (zip, redaction pipeline, crash-report scanning) to answer a question
about a version string.

WHY THIS IS NOT A ONE-LINE SUBSTRING TEST. The same release is stamped
differently per artifact, and ``build-wheel.yml`` REWRITES ``__version__`` to
the wheel's form, so a running install reports whichever spelling its packaging
lane used:

==========  =========================  =========================
channel     desktop (SemVer)           wheel / CLI (PEP 440)
==========  =========================  =========================
nightly     ``1.2.3-nightly.<stamp>``  ``1.2.3.dev<stamp>``
insider     ``1.2.3-insider.4``        ``1.2.3rc4``
stable      ``1.2.3``                  ``1.2.3``
==========  =========================  =========================

Neither PEP 440 prerelease spelling contains a ``-``, so the hyphen-only rule
this module replaced reported **stable** for every prerelease CLI install —
silently making insider and nightly wheel users' bug reports indistinguishable
from a supported build's, which is the exact population the prerelease channels
exist to hear from.

The SemVer half mirrors ``website/electron/auto-update.js``
``channelForVersion``: a ``-nightly.`` stamp is nightly and ANY other
prerelease suffix is insider, because ``release.yml`` publishes ``-insider.N``
and ``-rc.N`` alike to the insider feed.

The same table read backwards is :func:`release_refs`: the git tags
``release.yml`` may have cut for the version a build reports, for a caller that
must install THIS release somewhere else (the cloud launcher's public-repo clone).

WHAT A BUG REPORT MAY CLAIM is a second question, answered by
:func:`provenance`, because a string classifier cannot see a marker that is no
longer in the string. A distribution that repackages a release stamps
``__version__`` with a four-part build id (``0.7.0.5``, the ``BUILD_VERSION``
rule in ``kiro_crew/__init__``), and that rule only admits a stamp over a BARE
base -- so on a repackaged insider build the ``rc`` marker survives only in the
installed distribution's metadata (``0.7.0rc7``), and ``channel("0.7.0.5")``
reads stable. Filed unchanged, that report carried a wrong ``channel: stable``
label at CREATE time, where the reporter's later dropdown pick cannot remove
it, and put the wrapper-internal stamp in the public version field. The
resolver weighs the version stamp, the distribution metadata and the
``$KIROCREW_HOME/channel`` record together and answers ``None`` -- the form's
own ``Not sure`` -- when they are silent or disagree, rather than guess. It is
NOT what the status payload ships: that field is a three-valued contract the
dashboard renders, and it keeps reading :func:`channel`.
"""

from __future__ import annotations

import importlib.metadata
import re
from typing import NamedTuple

from kiro_crew import __version__
from kiro_crew.changelog import release_of_build, running_release
from kiro_crew.config.paths import data_home

#: The lanes a build can be on. Ordered loudest-first for docs; not an enum
#: because these strings cross a JSON boundary into the dashboard and a label
#: vocabulary, where the literal IS the contract.
CHANNELS = ("nightly", "insider", "stable")

#: A PEP 440 prerelease segment (``rc4``, ``b1``, ``a2``). A base version is
#: only digits and dots, so an ``a`` / ``b`` / ``rc`` followed by digits
#: anywhere in the string can only have come from a prerelease segment. This
#: also matches ``1.2.3rc4.post1``, which is the point of not anchoring it.
_PEP440_PRERELEASE = re.compile(r"(?:a|b|rc)\d+")

#: The version shapes that name a release tag. ``release.yml`` refuses any tag
#: whose base is not exactly ``x.y.z``, so these are anchored to three numeric
#: components; a distribution build stamp (``0.7.0.5``, a ``BUILD_VERSION``
#: file beside ``kiro_crew/__init__.py``) is a build OF ``0.7.0`` and folds onto
#: its tag, exactly as ``changelog.release_of_build`` folds it onto its notes.
_STABLE_BUILD = re.compile(r"(?P<base>\d+\.\d+\.\d+)(?:\.\d+)?")
#: The wheel spelling of an insider build. ``release.yml`` derives ``rcN`` from
#: the tag's trailing number for ``-insider.N`` and ``-rc.N`` tags alike, so the
#: wheel form cannot say which one it came from; :func:`release_refs` names
#: both, ``-insider.N`` (the lane's own naming) first.
_INSIDER_WHEEL = re.compile(r"(?P<base>\d+\.\d+\.\d+)rc(?P<n>\d+)")
#: The desktop spelling of an insider build IS the tag minus its ``v``.
_INSIDER_DESKTOP = re.compile(r"\d+\.\d+\.\d+-(?:insider|rc)\.\d+")

#: Release channel -> the repository label that carries it. Prerelease reports
#: are what this mapping exists for: an insider bug is a candidate release
#: blocker and a nightly bug is usually a PR that merged hours ago, and neither
#: is triaged like a report against a supported build. ``stable`` is included so
#: the dimension is COMPLETE — a half-populated dimension cannot be used as a
#: saved filter, because "no channel label" would mean both "stable" and "filed
#: before this shipped".
CHANNEL_LABELS = {
    "nightly": "channel: nightly",
    "insider": "channel: insider",
    "stable": "channel: stable",
}

#: Release channel -> the exact option text of ``bug_report.yml``'s "Release
#: channel" dropdown. Prefilling a dropdown matches the option string VERBATIM
#: and silently leaves the field EMPTY on a miss, so ``test_diagnostics.py``
#: pins this map against the template's real option list.
CHANNEL_FORM_OPTIONS = {
    "nightly": "Nightly",
    "insider": "Insider (prerelease)",
    "stable": "Stable",
}

#: The dropdown's own answer for a lane the build cannot prove (see
#: :func:`provenance`). ``issue-triage.yml`` deliberately maps it to NO label,
#: so a report filed with it is labelled by a human who read it, never by a
#: guess. Same verbatim-match pin as the map above.
UNKNOWN_CHANNEL_FORM_OPTION = "Not sure"

#: The installed distribution whose metadata version is the second copy of the
#: release pipeline's spelling. ``pyproject.toml`` names it, and
#: ``build-wheel.yml`` stamps its ``version`` with the same string it writes
#: into ``__version__``, so the two agree on a build nobody re-stamped.
_DISTRIBUTION_NAME = "kirocrew"


def channel(version: str | None = None) -> str:
    """Classify ``version`` (default: this build's) into a release channel.

    Takes an argument so tests and callers holding some *other* version string
    can use the same rule instead of reimplementing it.
    """
    v = version if version is not None else __version__
    # `.dev` is checked first: a nightly wheel is `<base>.dev<stamp>` and
    # carries no rc segment, while an rc wheel never carries `.dev`.
    if "-nightly." in v or ".dev" in v:
        return "nightly"
    if "-" in v or _PEP440_PRERELEASE.search(v):
        return "insider"
    return "stable"


def is_prerelease(version: str | None = None) -> bool:
    """Whether this build is NOT a supported stable release."""
    return channel(version) != "stable"


def release_refs(version: str | None = None) -> tuple[str, ...]:
    """The git tags ``release.yml`` may have cut for ``version``, likeliest first.

    ``0.7.0`` and a stamped ``0.7.0.5`` both name ``v0.7.0``; an insider build
    spelled the desktop way (``0.7.0-insider.5``, ``0.7.0-rc.1``) IS its tag.
    The wheel spelling ``0.7.0rc5`` is what ``release.yml`` writes for BOTH a
    ``v0.7.0-insider.5`` and a ``v0.7.0-rc.5`` tag, so it names both, insider
    first. Empty when no tag can exist for the version: nightly builds come off
    ``main`` HEAD and cut none, the ``a`` / ``b`` prerelease segments belong to
    no lane, and an unparseable string names nothing. The answers are tag
    NAMES, not a promise that one exists — a caller that will clone one must
    still probe the remote, because a build can be stamped before its tag is
    pushed and a fork may never push one.
    """
    v = (version if version is not None else __version__).strip()
    if channel(v) == "nightly":
        return ()
    m = _STABLE_BUILD.fullmatch(v)
    if m:
        return (f"v{m.group('base')}",)
    m = _INSIDER_WHEEL.fullmatch(v)
    if m:
        base, n = m.group("base"), m.group("n")
        return (f"v{base}-insider.{n}", f"v{base}-rc.{n}")
    if _INSIDER_DESKTOP.fullmatch(v):
        return (f"v{v}",)
    return ()


# ── Report provenance ─────────────────────────────────────────────────────────


class Provenance(NamedTuple):
    """What the running build can prove about its release identity.

    ``channel`` is ``None`` when the build cannot prove its lane; the other
    fields are the evidence, kept so the PRIVATE diagnostics bundle can record
    what was weighed while the PUBLIC report says only what was proven.
    """

    version: str
    """``__version__`` as the running build reports it -- possibly a repackager's
    four-part build stamp. Private: never a public report field."""
    release: str
    """The public release version: ``version`` with a build stamp folded off
    (``0.7.0.5`` -> ``0.7.0``). The release pipeline's own spellings are kept,
    because ``0.7.0-insider.4`` IS its public tag and a nightly's stamp is the
    only thing that says WHICH nightly."""
    distribution: str | None
    """The installed distribution's metadata version, ``None`` when there is
    no dist-info to read (a bare source tree)."""
    recorded: str | None
    """The ``$KIROCREW_HOME/channel`` record, ``None`` when absent or naming
    no lane."""
    channel: str | None
    """The lane the report may claim, or ``None`` for the form's ``Not sure``."""


def _metadata_version(name: str) -> str:
    """Seam over ``importlib.metadata.version`` so tests can answer it in-process."""
    return importlib.metadata.version(name)


def distribution_version() -> str | None:
    """The installed ``kirocrew`` distribution's metadata version, or ``None``.

    This is the second copy of the release pipeline's version spelling, and the
    one a repackager's ``BUILD_VERSION`` stamp cannot reach: the stamp rewrites
    ``__version__``, not the wheel's ``METADATA``. Any failure to read it --
    no dist-info, a broken environment -- is ``None`` (no evidence), never an
    exception: this runs inside the diagnostics collector, whose job is to work
    on a broken install.
    """
    try:
        raw = _metadata_version(_DISTRIBUTION_NAME)
    except Exception:
        return None
    value = str(raw or "").strip()
    return value or None


def recorded_channel() -> str | None:
    """The lane ``$KIROCREW_HOME/channel`` records, or ``None``.

    Absent-aware, which is the one way it differs from
    ``platform.update_layout.release_channel``: the updater must always have a
    lane to FOLLOW and so defaults to stable, but as EVIDENCE an absent or
    unreadable record is silence, and silence must not read as stable -- that
    default is exactly how a packaged insider build was reported as Stable.
    Same ``data_home()`` (never ``config_dir()``) for the same reason the
    updater gives: no start-of-process maintenance as a side effect of a read.
    """
    try:
        raw = (data_home() / "channel").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name = raw.strip().lower()
    return name if name in CHANNELS else None


def _is_build_stamp(version: str) -> bool:
    """A repackager's four-part build id, the one shape that says nothing about
    its lane: the stamp rule admits it only over a bare base, so whatever
    prerelease marker the release pipeline wrote is gone from this string."""
    return release_of_build(version) != version


def report_channel(version: str, distribution: str | None, recorded: str | None) -> str | None:
    """The lane a bug report may claim for ``version``, or ``None``.

    Three sources can each make ONE claim, and the answer is the claim they
    agree on:

    * the ``channel`` record, when it names a lane;
    * the installed distribution's metadata version, when it carries a
      prerelease marker AND describes the same release as ``version``.
      Metadata for another release is stale dist-info (a checkout ahead of its
      editable install) and says nothing about these bytes. PLAIN metadata
      says nothing either: the desktop lanes pip-install the checkout and stamp
      only ``__version__``, so a desktop insider build's dist-info reads the
      unstamped ``pyproject.toml`` base on every lane -- silence, not a stable
      claim. Only the wheel lane stamps ``pyproject.toml``, which is why a
      marker there IS evidence (the reported case), while its absence proves
      nothing;
    * ``version`` itself, unless it is a build stamp. The release pipeline
      writes it for every lane, so its plain spelling is a positive stable,
      while a four-part build stamp claims nothing: the stamp rule strips the
      marker.

    No claim at all is ``None``, not stable: an unknown lane guessed as stable
    is the failure this function exists to end. Two claims that disagree are
    ``None`` too -- a stable record over an ``rc`` build is a promoted-stable
    install that was never re-stamped, or a lane switch the user has not
    updated onto yet, and either way the report must not pick a side that the
    reporter cannot take back once it is a label.
    """
    claims: list[str] = []
    if recorded is not None and recorded in CHANNELS:
        claims.append(recorded)
    release = running_release(version)[0]
    if distribution and running_release(distribution)[0] == release:
        marker = channel(distribution)
        if marker != "stable":
            claims.append(marker)
    if not _is_build_stamp(version):
        claims.append(channel(version))
    if not claims:
        return None
    agreed = claims[0]
    return agreed if all(claim == agreed for claim in claims) else None


def provenance(version: str | None = None) -> Provenance:
    """Resolve what this build (default) or ``version`` can prove.

    The one resolver behind every public report field: the diagnostics bundle's
    issue link reads its version, its dropdown answer and its create-time label
    from here, so the three cannot contradict each other the way a classifier
    over the raw stamp made them.
    """
    v = (version if version is not None else __version__).strip()
    distribution = distribution_version()
    recorded = recorded_channel()
    return Provenance(
        version=v,
        release=release_of_build(v),
        distribution=distribution,
        recorded=recorded,
        channel=report_channel(v, distribution, recorded),
    )
