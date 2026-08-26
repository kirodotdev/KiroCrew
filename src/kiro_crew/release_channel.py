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
"""

from __future__ import annotations

import re

from kiro_crew import __version__

#: The lanes a build can be on. Ordered loudest-first for docs; not an enum
#: because these strings cross a JSON boundary into the dashboard and a label
#: vocabulary, where the literal IS the contract.
CHANNELS = ("nightly", "insider", "stable")

#: A PEP 440 prerelease segment (``rc4``, ``b1``, ``a2``). A base version is
#: only digits and dots, so an ``a`` / ``b`` / ``rc`` followed by digits
#: anywhere in the string can only have come from a prerelease segment. This
#: also matches ``1.2.3rc4.post1``, which is the point of not anchoring it.
_PEP440_PRERELEASE = re.compile(r"(?:a|b|rc)\d+")

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
