"""Contract tests for the nightly desktop version stamp.

The stamp feeds three consumers with incompatible constraints, and a
plausible-looking "simplification" of the shell that computes it breaks one
of them in a way no PR-level check exercises (the probe builds that gate
``build-desktop.yml`` use ``0.0.0-probe.N``, whose prerelease identifier is a
single digit -- so a malformed *real* stamp only fails on the nightly run):

* **Int32-safe numeric identifiers** (HISTORICAL, still enforced) -- the Windows
  target was Squirrel.Windows, whose bundled NuGet compared NUMERIC prerelease
  identifiers with ``Int32.Parse``
  (``NuGet.SemanticVersion.CompareTo`` -> ``ParseInt32``). An identifier above
  ``2147483647`` threw ``System.OverflowException`` inside
  ``ReleaseEntry.WriteReleaseFile``, killing ``Update.com --releasify``: a single
  ``YYYYMMDDHHMMSS`` identifier (~2.0e13) always overflowed, while ``YYYYMMDD``
  (~2.0e7) and ``HHMMSS`` (<= 235959) as SEPARATE identifiers always fit. The
  target is now NSIS, which imposes neither this bound nor NuGet's 20-character
  prerelease cap, so the constraint is no longer live -- but the assertion is
  kept as a regression guard: shortening the stamp buys nothing, and any future
  NuGet-based target (msi, appx) would re-impose it silently.
* **Channel routing** -- the literal ``-nightly.`` substring is what
  ``auto-update.js`` ``channelForVersion``, ``instance-guard.js``
  ``identityFamily``, and ``packaging/build-desktop.sh``'s ``*-nightly.*``
  glob all match on. Lose it and a nightly build silently tracks the
  insider feed and ships under the production app name.
* **Uniqueness** -- seconds precision must survive: a date-only stamp let two
  nightlies on one UTC date overwrite the same immutable
  ``signed/<channel>/<version>/`` keys (the republish class #62 fixed for
  ``cli/*``), which becomes CloudFront edge divergence once mac artifacts are
  public.

Asserted against the rendered stamp format rather than by running the
workflow, so the guarantees hold without a nightly dispatch.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "nightly.yml"

INT32_MAX = 2_147_483_647

# Worst-case stamp components. The TIME is deliberately the 06:00 cron's value:
# HHMMSS=060000 is the case that makes a bare numeric identifier invalid SemVer
# (leading zero), which electron-builder rejects outright.
_WORST_CASE_DATE = "29991231"
_WORST_CASE_TIME = "060000"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _rendered_nightly_version() -> str:
    """Render the workflow's `version=` output with worst-case stamp parts.

    Extracts the literal format from the workflow instead of duplicating it,
    so the test tracks the shell it is pinning.
    """
    text = _workflow_text()
    match = re.search(r'echo "version=\$\{BASE\}([^"]+)" >> "\$GITHUB_OUTPUT"', text)
    assert match, "nightly.yml no longer emits a version= output in the expected shape"
    suffix = match.group(1)
    rendered = (
        suffix.replace("${STAMP_DATE}", _WORST_CASE_DATE)
        .replace("${STAMP_TIME}", _WORST_CASE_TIME)
        .replace("${STAMP_FULL}", _WORST_CASE_DATE + _WORST_CASE_TIME)
        .replace("${STAMP}", _WORST_CASE_DATE + _WORST_CASE_TIME)
    )
    assert "$" not in rendered, f"unresolved shell variable in rendered stamp: {rendered!r}"
    return "0.1.0" + rendered


def test_digit_runs_fit_int32_after_identifier_concatenation() -> None:
    """Squirrel Int32-parses digit runs in the nupkg filename's version.

    electron-builder CONCATENATES dot-separated prerelease identifiers into the
    nupkg filename, so the constraint applies to digit runs in the joined
    string -- dot-splitting a long number does not help. Proven live on
    windows-latest: 20260727152449 (2.0e13) fails, 20260727.183000 fails
    (concatenated back to 14 digits), 1785000002 (1.78e9) passes, and
    20260727t184500 passes. The bound is magnitude, not digit count.
    """
    version = _rendered_nightly_version()
    _, _, prerelease = version.partition("-")
    assert prerelease, f"nightly version carries no prerelease part: {version!r}"
    joined = prerelease.replace(".", "")
    runs = re.findall(r"\d+", joined)
    assert runs, f"nightly stamp has no digits to order by: {version!r}"
    for run in runs:
        assert int(run) <= INT32_MAX, (
            f"digit run {run!r} in concatenated prerelease {joined!r} exceeds "
            f"Int32 ({INT32_MAX}); `Update.com --releasify` throws "
            "System.OverflowException and the Windows desktop leg fails. "
            "Separate the digit runs with a LETTER (e.g. <date>t<time>) -- a "
            "dot does not survive electron-builder's filename concatenation."
        )


def test_numeric_identifiers_have_no_leading_zero() -> None:
    """SemVer forbids leading zeros in purely numeric prerelease identifiers.

    The 06:00 cron produces HHMMSS=060000, so a bare `.060000` identifier is
    invalid and electron-builder rejects the version before Squirrel ever runs.
    An alphanumeric identifier (one containing a letter) may carry leading zeros.
    """
    version = _rendered_nightly_version()
    _, _, prerelease = version.partition("-")
    for ident in prerelease.split("."):
        if ident.isdigit() and len(ident) > 1:
            assert not ident.startswith("0"), (
                f"numeric prerelease identifier {ident!r} in {version!r} has a "
                "leading zero -- invalid SemVer, rejected by electron-builder. "
                "Prefix it with a letter so it becomes alphanumeric."
            )


def test_nightly_channel_prefix_is_preserved() -> None:
    """`-nightly.` is what every channel-routing consumer matches on."""
    version = _rendered_nightly_version()
    assert "-nightly." in version, (
        f"{version!r} lost the '-nightly.' marker; auto-update.js "
        "channelForVersion would classify it as insider and the app would "
        "track the wrong feed"
    )


def test_stamp_keeps_seconds_precision() -> None:
    """Same-minute re-dispatch must not collide, and the date stays 8 digits.

    Two separate properties, deliberately not conflated:

    * UNIQUENESS is carried by the SECONDS component alone. sign-and-notarize.yml
      keys pre-signed/signed/notarized/desktop objects on ${VERSION} with an
      If-None-Match never-republish check, so what stops a same-minute
      re-dispatch reusing a key is HHMMSS being full seconds -- and that held
      for the 11-digit YYDDD form too.
    * The 14-digit total pins the DATE WIDTH: the desktop stamp keeps the full
      8-digit YYYYMMDD so it stays legible and matches the digits the wheel's
      .dev<YYYYMMDDHHMMSS> carries for the same run. That is a shape pin, not a
      uniqueness guard -- a future target that re-imposed a length cap could
      shorten the date without breaking uniqueness."""
    version = _rendered_nightly_version()
    _, _, prerelease = version.partition("-")
    digits = "".join(re.findall(r"\d", prerelease))
    assert len(digits) >= 14, (
        f"{version!r} carries {len(digits)} stamp digits; the desktop stamp is "
        "pinned to the full 8-digit YYYYMMDD + HHMMSS (14) so it stays legible "
        "and shares its digits with the wheel's .dev<N>. Shortening the DATE "
        "does not break uniqueness (the seconds assertion below owns that), so "
        "if a Windows target re-imposes a length cap, change this pin knowingly"
    )
    # The time is the last digit run after the "t" separator.
    time_part = prerelease.rsplit("t", 1)[-1]
    time_digits = "".join(re.findall(r"\d", time_part))
    assert len(time_digits) >= 6, (
        f"{version!r} time component {time_part!r} carries {len(time_digits)} "
        "digits; full seconds precision (HHMMSS = 6) is required so a same-minute "
        "re-dispatch cannot republish an immutable signed/notarized version key"
    )


def test_stamp_orders_chronologically() -> None:
    """Fixed-width zero-padded stamps must sort lexically as they sort in time."""
    text = _workflow_text()
    match = re.search(r'echo "version=\$\{BASE\}([^"]+)" >> "\$GITHUB_OUTPUT"', text)
    assert match
    suffix = match.group(1)

    def render(date: str, time: str) -> str:
        return (
            suffix.replace("${STAMP_DATE}", date)
            .replace("${STAMP_TIME}", time)
            .replace("${STAMP_FULL}", date + time)
            .replace("${STAMP}", date + time)
        )

    earlier = render("20260727", "060000")
    later_same_day = render("20260727", "184500")
    next_day = render("20260728", "010000")
    year_rollover = render("20270101", "000000")
    assert earlier < later_same_day < next_day < year_rollover, (
        "stamp does not sort chronologically as a string "
        f"({earlier!r} < {later_same_day!r} < {next_day!r} < {year_rollover!r} "
        "is false); semver compares alphanumeric identifiers lexically, so a "
        "non-fixed-width or unpadded stamp would order releases wrongly"
    )


def test_wheel_version_stays_pep440_and_separate() -> None:
    """The desktop semver change must not leak into the pip wheel stamp."""
    text = _workflow_text()
    match = re.search(r'echo "wheel_version=\$\{BASE\}([^"]+)" >> "\$GITHUB_OUTPUT"', text)
    assert match, "nightly.yml no longer emits a wheel_version output"
    suffix = match.group(1)
    assert suffix.startswith(".dev"), (
        f"wheel stamp {suffix!r} must stay a PEP 440 '.dev<N>' suffix -- a semver "
        "prerelease is normalized away by setuptools, collapsing every nightly "
        "wheel onto the base version"
    )
    assert "-" not in suffix, f"wheel stamp {suffix!r} must not carry a semver prerelease dash"


DESKTOP_WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop.yml"
# Windows builds separately: it Authenticode-signs during the build, so it needs
# OIDC that the shared desktop workflow deliberately does not hold.
WINDOWS_WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"


def test_stamp_components_come_from_a_single_clock_read() -> None:
    """Date and time must be sliced from ONE stamp, not read from `date` twice.

    Separate `date -u` calls can straddle UTC midnight, pairing an old-day date
    with a new-day time. The version then goes BACKWARD relative to the run
    before it, and the feed -- whose client gate engages on any version
    difference -- would offer installed clients a downgrade. It can also make
    the desktop stamp and the wheel stamp disagree for the same run.
    """
    text = _workflow_text()
    block = re.search(r"(STAMP=\$\(date.*?)echo \"version=", text, re.DOTALL)
    assert block, "nightly.yml no longer computes STAMP before emitting version"
    body = block.group(1)
    date_calls = re.findall(r"\$\(date\b", body)
    assert len(date_calls) == 1, (
        f"found {len(date_calls)} `date` calls in the stamp block; exactly one "
        "is allowed. Slice the single STAMP (${STAMP:0:8} / ${STAMP:8:6}) so the "
        "components cannot straddle UTC midnight."
    )


def test_documented_probe_example_satisfies_the_same_rules() -> None:
    """Any version example in the dispatch description must actually build.

    The description is operator-facing copy: an example that reproduces the
    overflow teaches the exact failure this PR fixes. Every semver-looking
    example is held to the same digit-run rule as the real stamp.
    """
    # Both workflows document a probe stamp, and the version-format hazards
    # these examples guard were Squirrel's -- so it is the WINDOWS workflow
    # whose example historically had to satisfy them. Checking only
    # build-desktop.yml would leave the example that matters unguarded.
    for workflow in (DESKTOP_WORKFLOW, WINDOWS_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        examples = re.findall(r"\b\d+\.\d+\.\d+-[0-9A-Za-z.\-]+", text)
        assert examples, f"no version examples found in {workflow.name}"
        for example in examples:
            _, _, prerelease = example.partition("-")
            joined = prerelease.replace(".", "")
            for run in re.findall(r"\d+", joined):
                if int(run) > INT32_MAX:
                    # Only fail when the example is being RECOMMENDED, not when
                    # it is cited as a known-bad counterexample.
                    context = text[max(0, text.index(example) - 200) : text.index(example) + 60]
                    recommended = not re.search(
                        r"(?i)(dotted|reproduces|FAIL|counterexample|overflow)", context
                    )
                    assert not recommended, (
                        f"documented example {example!r} in {workflow.name} has digit "
                        f"run {run!r} above Int32 ({INT32_MAX}) and is presented as a "
                        "recommendation; it would fail releasify. Use a "
                        "letter-separated stamp."
                    )


def test_job_level_inputs_are_declared_on_every_trigger() -> None:
    """A job-level expression reading `inputs.X` must find X on BOTH triggers.

    ``continue-on-error`` is evaluated before any job starts. When it reads an
    input that a trigger does not declare, the value is empty, the key is not a
    boolean, and GitHub rejects the whole workflow at startup: the run ends in
    seconds with ZERO jobs and no log to explain it. That is exactly how the
    ``workflow_dispatch`` probe path -- the only way to validate a packaging
    change against a realistic version before a nightly -- died silently after
    ``windows_soft_fail`` was added under ``workflow_call`` alone.

    YAML linters and actionlint both pass such a file, so pin it here.
    """
    # Both reusable build workflows are exposed on workflow_call AND
    # workflow_dispatch, and both carry job-level expressions, so both can be
    # killed at startup this way.
    for workflow in (DESKTOP_WORKFLOW, WINDOWS_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        referenced = set(re.findall(r"inputs\.([A-Za-z_][A-Za-z0-9_]*)", text))
        assert referenced, f"{workflow.name} no longer references any inputs"

        # Split the trigger block per trigger and collect each one's inputs.
        for trigger in ("workflow_call", "workflow_dispatch"):
            block = re.search(
                rf"^  {trigger}:\n(.*?)(?=^  [a-z_]+:\n|^permissions:|^jobs:)",
                text,
                re.MULTILINE | re.DOTALL,
            )
            assert block, f"{workflow.name} no longer declares a {trigger} trigger"
            declared = set(
                re.findall(r"^      ([A-Za-z_][A-Za-z0-9_]*):$", block.group(1), re.MULTILINE)
            )
            missing = referenced - declared
            assert not missing, (
                f"{workflow.name}: {trigger} does not declare input(s) "
                f"{sorted(missing)} that the workflow references. If a job-level "
                "key (continue-on-error, if, timeout-minutes, environment) reads "
                "one of them, GitHub rejects the workflow at startup on this "
                "trigger and the run produces zero jobs."
            )


def test_windows_soft_fail_expression_is_boolean_safe() -> None:
    """continue-on-error must coerce to a boolean even for an absent input.

    The soft-fail switch moved to build-windows.yml when Windows split out of
    the shared desktop matrix (it signs during its build and so needs OIDC,
    which build-desktop.yml must not hold). The hazard travelled with it, so
    the assertion follows the expression rather than the filename. Duplicated in
    test_windows_signing_contract.py, which owns that workflow's contract; kept
    here too because this file is where the zero-jobs startup rejection was
    diagnosed and documented.
    """
    text = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^    continue-on-error: (.+)$", text, re.MULTILINE)
    assert match, "build-windows.yml no longer sets continue-on-error on the build job"
    expr = match.group(1)
    assert "soft_fail == true" in expr or "fromJSON" in expr, (
        f"continue-on-error expression {expr!r} yields the input's raw value; an "
        "absent input then makes it non-boolean and GitHub rejects the workflow "
        "at startup. Compare explicitly (`== true`) or coerce with fromJSON."
    )
