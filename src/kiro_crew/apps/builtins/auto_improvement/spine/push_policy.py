"""F10 direct-commit push policy — the spine-owned, non-overridable safety gate.

The app's #1 safety control is the push-disabled invariant (08_safety §1.3 /
ROADMAP F10): the driver refuses to start unless the clone's push remote is
``DISABLED_NO_PUSH``, and output is draft-only — a verified change escapes the
sandbox ONLY through a draft CR a human reviews.

F10 "direct-commit mode" deliberately relaxes that into a narrow, explicitly-consented
shape: when the operator ticks "Direct commit" for a project, the app pushes the
verified commit straight to the ONE feature branch they authorized. Supply-chain security
guidance is explicit that the CR / branch-protection review is the mandated trust gate;
relaxing it is only safe when (a) the human explicitly
consented, (b) a *protected* branch is NEVER a valid target, and (c) every verification
gate still runs before the push. This module owns (b): the hard, non-overridable
protected-branch denylist.

WHY SPINE-SIDE (not just the UI): the denylist must be enforced where a hand-edited
``config.json`` cannot widen it (ROADMAP F10 guard rails). The UI also hides the toggle
for a protected branch, but THIS is the authoritative check the driver consults before
any push — a crafted config that sets ``directCommit: true`` + ``branch:
"origin/mainline"`` is refused here, in the spine.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Branches that direct-push must NEVER target, even with the operator's checkbox on.
# These are shared / release / integration branches whose protection is the whole point
# of the CR review gate (the mandated control: "code review process enforced through branch
# protection rules"). Direct-commit is for your OWN feature branch; a shared branch always falls
# back to the CR path. Matched after stripping a leading ``origin/`` and lower-cased.
#
# Two forms: exact names (the well-known shared branches in this org + the universal
# primary names) and glob-ish prefixes for release lines. Keep this list TIGHT and
# conservative — when unsure whether a branch is shared, it is treated as protected.
PROTECTED_BRANCH_NAMES = frozenset(
    {
        "mainline",  # a common primary-branch name in some orgs
        "main",
        "master",  # wokeignore:rule=master  # legacy primary: a real protected branch name
        "trunk",
        "develop",
        "development",
        "prod",
        "production",
        "release",
        "stable",
    }
)

# Release/integration line prefixes — ``release/*``, ``releases/*``, ``hotfix/*``, etc.
# These are conventionally protected integration lines, never a personal feature branch.
PROTECTED_BRANCH_PREFIXES = (
    "release/",
    "releases/",
    "hotfix/",
    "prod/",
    "production/",
    "mainline/",
)


# The remote prefixes we strip to recover the bare branch name. The app only ever uses
# ``origin``; ``upstream`` is included so a non-origin remote can't smuggle a protected
# name past the check (``upstream/mainline``). We do NOT strip arbitrary first segments —
# that would mangle a real branch path like ``release/2026.1`` → ``2026.1`` and defeat the
# release-line prefix denylist.
_REMOTE_PREFIXES = ("origin/", "upstream/")

#: Fully-qualified ref prefixes. ``refs/heads/main`` names the same branch as ``main``, so a
#: denylist that only knows the short form is trivially bypassed by spelling it out.
_REF_PREFIXES = ("refs/heads/", "refs/remotes/")


def normalize_branch(branch: str) -> str:
    """Strip a leading well-known remote prefix (``origin/`` / ``upstream/``) and whitespace.

    The configured base ref is stored as ``origin/<name>`` (tasks #19/#20); the push target
    is the bare ``<name>``. We strip ONLY a known remote prefix — never an arbitrary first
    segment — so a real branch path like ``release/2026.1`` keeps its protected ``release/``
    prefix (stripping it would let it slip past :func:`is_protected_branch`).
    """
    b = (branch or "").strip()
    # Strip known ref/remote prefixes REPEATEDLY until stable. A single ordered pass is not
    # enough: ``refs/heads/main`` and ``origin/refs/heads/main`` are the same ref as ``main``
    # and git accepts all three, but each needs a different strip order, and the first
    # version of this fix still let ``origin/refs/heads/main`` through — measured. Reachable
    # because ``branch`` is writable through ``PUT /config`` with no shape check on write.
    #
    # Found by auditing this denylist for the same evasion class review had just found in
    # the shell denylist: a normalization that runs once can always be re-nested.
    #
    # Bounded loop, not ``while True``: a crafted value like ``origin/origin/origin/...``
    # must not spin. Six passes is far more nesting than any real ref.
    for _ in range(6):
        low = b.lower()
        for pfx in _REF_PREFIXES + _REMOTE_PREFIXES:
            if low.startswith(pfx):
                b = b[len(pfx) :]
                break
        else:
            break
    return b


def is_protected_branch(branch: str) -> bool:
    """True iff ``branch`` is a protected/shared branch that direct-push must refuse.

    Conservative: an empty/blank branch is treated as protected (refuse rather than push
    to an ambiguous target). Matching is case-insensitive on the ``origin/``-stripped
    name, against the exact denylist and the release-line prefixes.
    """
    name = normalize_branch(branch).lower()
    if not name:
        return True  # ambiguous/empty target → refuse (fail closed)
    if name in PROTECTED_BRANCH_NAMES:
        return True
    return any(name.startswith(p) for p in PROTECTED_BRANCH_PREFIXES)


def authorize_direct_push(*, direct_commit: bool, branch: str) -> tuple[bool, str]:
    """The single authorization decision for an F10 direct push.

    Returns ``(allowed, reason)``. A push is authorized ONLY when the operator opted in
    (``direct_commit``) AND the target is a real, non-protected feature branch. ``reason``
    is a human-readable refusal cause when not allowed (surfaced to the ledger/UI), or a
    confirmation string when allowed — never silent.

    This is the ONE place that says "yes" to escaping the sandbox via push; the driver
    must call it and honor the result. It does not itself push (no side effects) — it is
    a pure policy decision so it is trivially testable and auditable.
    """
    if not direct_commit:
        return False, "direct-commit mode is off (default) — falling back to the CR path"
    name = normalize_branch(branch)
    if not name:
        return False, "no authorized branch configured — refusing to direct-push"
    if is_protected_branch(branch):
        return False, (
            f"branch {name!r} is protected/shared — direct-push is refused "
            "(use the CR path; the protected-branch denylist is non-overridable)"
        )
    return True, f"direct-commit authorized for non-protected branch {name!r}"


#: Refusal codes from :func:`scan_content_for_secrets`. A caller renders its own message
#: from these, so no string built inside the scanner is ever logged — which is what makes
#: "the log line carries no scanned content" true by construction rather than by review.
SCAN_OK = "ok"
SCAN_HIT = "hit"
SCAN_NO_SCANNER = "no_scanner"

#: Human-readable text per refusal code, for a caller that needs to log or record one.
#: A pure literal table: no scanned text, no exception message, no finding can enter it.
SCAN_REASON_TEXT = {
    SCAN_OK: "",
    SCAN_HIT: "content scan found credential/exfiltration finding(s)",
    SCAN_NO_SCANNER: "credential scanners unavailable",
}


def describe_scan(code: str) -> str:
    """The log-safe message for a scan refusal code. Unknown code -> a fixed literal."""
    return SCAN_REASON_TEXT.get(code, "content scan refused the push")


def scan_content_for_secrets(text: str) -> tuple[bool, str]:
    """Return ``(clean, note)`` for content that is about to leave the host.

    Every push path in this app funnels through here: the PR-draft push
    (``profiles/github_repo/pr_recipe.py``), the driver's F10 direct push, and the
    operator's one-click commit. One implementation rather than three, because a
    credential scan that only guards *some* of the exits is not a gate — and the exit
    that was missed is the one that publishes.

    The driver already redacts the commit MESSAGE, but nothing scanned the committed
    CONTENT. A pushed commit is as unwipeable as its message, and the diff is
    agent-authored, which CLAUDE.md treats as untrusted.

    DETECT, never rewrite. Redacting a code diff would corrupt the very fix the gate
    proved, so a hit refuses the push and leaves the change in the durable local queue
    for a human. That is why this returns a verdict rather than cleaned text.

    FAIL-CLOSED: if the scanners cannot be imported, the content is treated as unsafe.
    An unscannable push is indistinguishable from an unscanned one.

    The note is built from LITERALS and an integer COUNT only — never from the scanned
    text, the scanner's own findings, or an exception's message. Two reasons, and the
    second is why the count is formatted separately below:

    1. A credential-scanner warning can quote the text it matched, so interpolating a
       finding would write the secret into the very log this exists to keep it out of.
    2. Callers LOG this note (``driver`` at direct-push refusal, ``pr_recipe`` on a
       degraded draft). CodeQL's clear-text-logging query follows dataflow, and any path
       from ``text`` to the returned string makes those log calls look like they publish
       a secret — a "high severity" alert that is really about provenance, not content.
       Keeping the note demonstrably independent of ``text`` is what makes the property
       checkable by a machine instead of arguable in a comment.
    """
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
    except Exception:  # noqa: BLE001 - no scanner, no push
        # The exception message is deliberately dropped rather than interpolated: an
        # ImportError text can carry a filesystem path, and this string gets logged.
        logger.warning("credential scanners unavailable — refusing the push", exc_info=True)
        return False, SCAN_NO_SCANNER
    if not (text or "").strip():
        return True, SCAN_OK
    _, cred_hits = redact_credentials(text)
    _, exfil_hits = redact_exfiltration_urls(text)
    # Only the COUNT crosses out of this function. The findings themselves are discarded
    # here, in the one place that has them, so no caller can log them by accident.
    total = int(len(list(cred_hits)) + len(list(exfil_hits)))
    del cred_hits, exfil_hits
    if total:
        # Only a CODE leaves this function — not a message, and not the count. The count
        # was still a value derived from `text`, which is enough for a taint-tracking
        # query to follow it into a caller's log call and report a leak. A constant code
        # cannot carry anything, so the property holds by construction.
        logger.warning("content scan found %d credential/exfiltration finding(s)", total)
        return False, SCAN_HIT
    return True, SCAN_OK


#: Credential-shaped env names to drop before untrusted code runs. Matched by NAME, never
#: by value. ONE definition: this used to be copied into both the gate and the agent
#: spawn, and a duplicated security decision is how the empty-allowlist inversion survived
#: in one copy after being fixed in the other.
#:
#: Needed because the shared `kiro_crew.sandbox` scrub does not cover every family —
#: measured on the author's host, `GITHUB_TOKEN` survives `scrub_env` (its list has
#: AWS_SECRET/SLACK_*/TELEGRAM_* but not GITHUB_*).
CREDENTIAL_ENV_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "APIKEY",
    "API_KEY",
    "CREDENTIAL",
)


def strip_credential_env(env: dict[str, str]) -> dict[str, str]:
    """Drop credential-shaped names from ``env``.

    Applied at BOTH places untrusted code runs — the gate's test execution and the agent
    spawn — because the sandbox's own scrub misses families like ``GITHUB_*``.
    """
    return {
        k: v
        for k, v in env.items()
        if not any(marker in k.upper() for marker in CREDENTIAL_ENV_MARKERS)
    }
