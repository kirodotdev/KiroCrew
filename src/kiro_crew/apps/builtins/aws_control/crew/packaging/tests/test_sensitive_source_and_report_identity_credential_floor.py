"""The standalone credential floor: the local rules that stand in for the shared validator.

In a deployment venv ``kiro_crew.security`` is not importable, so the builder's own path fence,
credential-name gate and content patterns are the only floor. These tests pin each local rule
and, where the shared validator also answers, measure the two against each other: the path fence
may catch less but never refuses a path the shared validator treats as ordinary, and it still
fences every foreign credential store; the content patterns may catch more but never miss what
the shared detector catches.

Part of the sensitive-source suite whose first module is
``test_sensitive_source_and_report_identity.py``.
"""

from __future__ import annotations

import base64
import pathlib

from .test_producer import load_build, source_defining


# ---------------------------------------------------------------------------
# Round-12 GPT F1: a short encoded credential must not slip under the b64 floor
#
# The standalone decoder is the packager's REAL scan path (the canonical redactor
# is not importable in the deployment venv), and a credential shorter than an AWS
# secret access key still base64-encodes to a run under 40 chars.
# ---------------------------------------------------------------------------
def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def test_a_short_encoded_credential_is_caught_by_the_decoder() -> None:
    """A ``sk-`` vendor key encodes to a ~32-char base64 run, well under the old 40 floor.

    Drives ``_scan_decoded_runs`` directly: that is the standalone-mode scan path the
    finding is about (in a deployment venv the canonical redactor is not importable, so
    this decoder is the real scan), and it is the unit the floor governs.
    """
    mod = load_build()
    secret = "sk-" + "A" * 22  # matches _HARD_PATTERNS vendor-key (sk-[A-Za-z0-9]{20,})
    run = _b64(secret)
    assert 20 <= len(run) < 40, f"run must sit in the newly-covered band, got {len(run)}"
    leaks = mod._scan_decoded_runs(f"note: {run}", "spec.json")
    assert any("encoded-vendor-key" in leak.kind for leak in leaks), [leak.kind for leak in leaks]


def test_MUTATION_the_old_40_char_floor_would_skip_the_short_run() -> None:
    """Restore the 40-char floor and the same short run goes unscanned by the decoder."""
    mod = load_build(mutate=("[A-Za-z0-9+/]{20,}={0,2}", "[A-Za-z0-9+/]{40,}={0,2}"))
    secret = "sk-" + "A" * 22
    run = _b64(secret)
    leaks = mod._scan_decoded_runs(f"note: {run}", "spec.json")
    assert not any("encoded-vendor-key" in leak.kind for leak in leaks), (
        "floor reverted to 40: the short encoded credential should slip through, "
        "proving the lowered floor is what catches it"
    )


# ---------------------------------------------------------------------------
# Round-12 GPT F2: a credential-store filename must be refused by name
#
# A ``.git-credentials`` file carries a generic ``user:password@host`` that the
# content patterns do not reliably match, so the name gate is the real defense.
# ---------------------------------------------------------------------------
def test_a_git_credentials_file_is_refused_by_name() -> None:
    """The name alone refuses it, before any content read."""
    mod = load_build()
    assert mod.refused_by_name(pathlib.Path(".git-credentials"))
    assert mod.refused_by_name(pathlib.Path(".pypirc"))


def test_MUTATION_git_credentials_would_pass_the_name_gate_without_the_entry() -> None:
    """Drop the ``.git-credentials`` entry and the name gate lets it through."""
    mod = load_build(mutate=("      | \\.git-credentials\n", ""))
    assert not mod.refused_by_name(pathlib.Path(".git-credentials")), (
        "entry removed: the name gate should no longer refuse it, proving the entry "
        "is what closes the gap"
    )


def test_the_local_fence_is_never_stricter_than_the_shared_one() -> None:
    """Every entry in the local list must be one the shared validator also refuses.

    The local list exists for the mode where the shared validator is unimportable, so it may
    be COARSER -- catch less -- but never stricter. A stricter entry refuses a path the rest
    of the tree considers ordinary, and one did: ``.kiro/agents`` is upstream's
    ``_WRITE_PROTECTED_HOME_PATHS``, protecting against WRITING a spec whose
    ``mcpServers.command`` the gateway execs. ``is_sensitive_path`` returns False for it, and
    ``~/.kiro`` is the DEFAULT source, so every run without ``--source`` refused its own crew.

    No local test caught that, because every test builds its crew under ``tmp_path`` and none
    exercises the default path. This test compares the two lists instead of the behaviour.
    """
    from kiro_crew.security.paths import is_sensitive_path

    mod = load_build()
    home = str(pathlib.Path.home())
    stricter = []
    for entry in mod._SENSITIVE_RELATIVE_DIRS:
        probe = f"{home}/{entry}"
        if "." not in pathlib.PurePosixPath(entry).name:
            probe += "/probe"
        if not is_sensitive_path(probe):
            stricter.append(entry)
    assert not stricter, (
        f"these local entries are refused here but not by the shared validator: {stricter}. "
        f"A read-only build must not invent a read fence the rest of the tree does not have."
    )


def test_the_default_agent_spec_path_is_not_refused() -> None:
    """The regression stated directly: the default source must remain usable.

    Named separately from the list comparison because this is the SYMPTOM an operator hits,
    and it should be the failure a future reader sees first.
    """
    mod = load_build()
    home = str(pathlib.Path.home())
    assert not mod._looks_sensitive_standalone(f"{home}/.kiro/agents/frontdesk.json")
    assert not mod._looks_sensitive_standalone(f"{home}/.kiro/crew/skills/faq/SKILL.md")


def test_the_local_fence_casefolds_rather_than_lowercasing() -> None:
    """Windows paths are case-insensitive, so ``~/.AWS`` names the same directory.

    And casefold is what the shared validator uses, so ``lower()`` here would be a second,
    weaker rule for one question. The two differ on real input: the German sharp s folds to
    ``ss`` where ``lower()`` leaves it alone.
    """
    mod = load_build()
    for variant in (".aws", ".AWS", ".Aws", ".aWs"):
        assert mod._looks_sensitive_standalone(f"/home/someone/{variant}/credentials"), variant


def test_the_predicate_uses_casefold_in_source() -> None:
    """A source rule, because no ASCII input distinguishes the two functions.

    ``.AWS`` is caught by either, so a behaviour test cannot tell casefold from lower. The
    difference only shows on non-ASCII, which no credential directory name has -- yet the
    shared validator casefolds, and matching it is the point.
    """
    src = source_defining("_looks_sensitive_standalone").read_text(encoding="utf-8")
    fn = src[src.index("def _looks_sensitive_standalone(") :]
    body = fn[: fn.index("\ndef ")]
    assert ".casefold()" in body, "the predicate stopped casefolding"
    assert ".lower()" not in body, "the predicate went back to lower(), which folds less"


def test_both_credential_predicates_fold_case(tmp_path: pathlib.Path) -> None:
    """This module has TWO path predicates, and both must fold. One did not.

    ``_looks_sensitive_standalone`` was fixed to casefold and the membership test in
    ``_inside_credential_dir`` was left comparing raw components against lowercase literals
    -- so ``~/.AWS/credentials`` passed one fence and failed the other. Fixing one predicate
    and leaving its twin is the failure this test exists to catch: it drives BOTH.
    """
    mod = load_build()
    for variant in (".aws", ".AWS", ".Aws"):
        probe = pathlib.Path(f"/home/someone/{variant}/credentials")
        assert mod.refused_by_location(probe), f"the location test missed {variant}"
        assert mod._looks_sensitive_standalone(probe.as_posix()), f"fence missed {variant}"


def test_the_two_predicates_agree_on_every_shared_entry() -> None:
    """Where the two lists overlap they must give the same answer, in any case.

    They are separate lists on purpose -- one is a coarse standalone floor, the other a
    directory-name test -- but a name in both must not be sensitive to one and ordinary to
    the other, which is what a missed casefold produces.
    """
    mod = load_build()
    shared = {".ssh", ".aws", ".gnupg"}
    for entry in shared:
        for spelling in (entry, entry.upper(), entry.capitalize()):
            probe = pathlib.Path(f"/home/someone/{spelling}/thing")
            assert mod.refused_by_location(probe) == mod._looks_sensitive_standalone(
                probe.as_posix()
            ), f"the two predicates disagree on {spelling}"


def test_the_local_patterns_catch_everything_the_shared_detector_does() -> None:
    """The local subset is a documented NARROWING, so the narrowing must be measured.

    ``_HARD_PATTERNS`` exists for the standalone case where ``kiro_crew.security`` cannot be
    imported. Calling it a subset is only honest if someone checks: three gaps were found by
    running this comparison rather than reading the two lists side by side --
    the two SSH public-key line forms, and the URL-encoded PEM header. The shared detector
    spells its separator ``[\\s+%]`` precisely for the encoded form, and the local copy had a
    literal space, so the encoded header passed unmatched.

    Executable rather than a source rule, because the shared patterns can change under this
    module: a new form added upstream should fail here, which is the whole point.
    """
    from kiro_crew.security import _HARD_CREDENTIAL_RE

    mod = load_build()
    # Every credential-shaped sample is ASSEMBLED, never written as one literal. The repo's
    # secret scanners read this file too, and a test that proves a scanner works must not
    # itself trip one -- ``test_producer.py`` already does this (``"AKIA" +
    # "IOSFODNN7EXAMPLE"[4:] + "ABCD"``), so this follows that convention rather than
    # inventing an exemption.
    _akia = "AKIA" + "IOSFODNN7EXAMPLE"
    _asia = "ASIA" + "IOSFODNN7EXAMPLE"
    _secret_label = "Secret" + "AccessKey"
    _secret_body = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY"
    inputs = {
        "aws-key-akia": _akia,
        "aws-key-asia": _asia,
        "labelled-secret": f'{_secret_label}="{_secret_body}"',
        "labelled-session": "aws_session" + "_token=FQoGZXIvYXdzEBYaDF",
        "labelled-access-id": "aws_access" + f"_key_id={_akia}",
        "access-key-id-label": "Access" + f'KeyId: "{_akia}"',
        "ssh-rsa-line": "ssh-" + "rsa AAAAB3NzaC1yc2EA user@host",
        "ssh-ed25519-line": "ssh-" + "ed25519 AAAAC3NzaC1lZDI1 user@host",
        "pem-header": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "pem-header-encoded": "BEGIN+" + "RSA+PRIVATE+KEY",
        "slack-token": "xox" + "b-123456789012-abcdefghijkl",
    }
    gaps = []
    for name, text in inputs.items():
        if not _HARD_CREDENTIAL_RE.search(text):
            continue  # not a shared-detector case; nothing is claimed about it
        if not any(pattern.search(text) for _, pattern in mod._HARD_PATTERNS):
            gaps.append(name)
    assert not gaps, (
        f"the standalone scan misses what the shared detector catches: {gaps}. The local set "
        f"may be COARSER in what it adds, never narrower in what the shared one refuses."
    )


def test_the_local_set_may_add_forms_the_shared_one_omits() -> None:
    """The relationship is one-directional, and that is deliberate.

    The local set catches GitHub and vendor tokens the shared detector does not, and that is
    fine: refusing more in a mode with no other floor is the safe direction. Asserting
    equality instead would delete those on the next run of the test above.
    """
    mod = load_build()
    extra = ("ghp" + "_" + "a" * 36, "sk" + "-" + "b" * 24)
    for text in extra:
        assert any(pattern.search(text) for _, pattern in mod._HARD_PATTERNS), text


def test_a_github_fine_grained_pat_is_caught_by_the_scan() -> None:
    """github_pat_ ... is a credential the classic gh[pousr]_ pattern does not match."""
    mod = load_build()
    pat = "github_pat_" + "A" * 22 + "_" + "b" * 59
    leaks = mod.scan_text(f"token = {pat}", "prompt")
    assert any("github-fine-grained-pat" in leak.kind for leak in leaks), [
        leak.kind for leak in leaks
    ]


def test_MUTATION_a_fine_grained_pat_slips_without_its_pattern() -> None:
    """Drop the fine-grained PAT from the vendor set and the token slips through unflagged.

    The vendor/token spellings are sourced from the shared ``credential_patterns`` module
    and spliced in as ``_VENDOR_TOKEN_COMPILED``; the mutation filters that one format out of
    the compiled set, which is the construct that now carries the catch.
    """
    mod = load_build(
        mutate=(
            "    *_VENDOR_TOKEN_COMPILED,\n",
            "    *(p for p in _VENDOR_TOKEN_COMPILED " 'if p[0] != "github-fine-grained-pat"),\n',
        )
    )
    pat = "github_pat_" + "A" * 22 + "_" + "b" * 59
    leaks = mod.scan_text(f"token = {pat}", "prompt")
    assert not any(
        "github-fine-grained-pat" in leak.kind for leak in leaks
    ), "pattern removed: the fine-grained PAT should slip, proving the pattern catches it"


def test_a_jwt_is_caught_by_the_scan() -> None:
    """A three-segment eyJ... JWT is a bearer/session credential the local set had missed."""
    mod = load_build()
    jwt = "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20
    leaks = mod.scan_text(f"authorization: Bearer {jwt}", "prompt")
    assert any(leak.kind == "jwt" for leak in leaks), [leak.kind for leak in leaks]


def test_a_dotenv_plan_path_is_refused_by_the_standalone_floor() -> None:
    """`.env` is a credential leaf the standalone floor must catch even without the validator."""
    mod = load_build()
    assert mod._looks_sensitive_standalone("home/user/.kiro/crew/.env") is True
    assert mod._looks_sensitive_standalone("home/user/project/app.env") is False
    assert mod._looks_sensitive_standalone("home/user/secret.pem") is True


def test_MUTATION_without_the_credential_name_rule_the_floor_misses_dotenv() -> None:
    """Remove the credential-name check and the standalone floor lets `.env` through."""
    mod = load_build(
        mutate=(
            "    if parts and _CREDENTIAL_NAME_RE.match(parts[-1]):",
            "    if parts and False:",
        )
    )
    assert mod._looks_sensitive_standalone("home/user/.kiro/crew/.env") is False, (
        "credential-name rule removed: the floor no longer catches .env, proving that rule is "
        "what closes the leaf gap when the shared validator is unavailable"
    )


def test_the_local_subset_fences_every_foreign_credential_store() -> None:
    """A credential store belonging to ANOTHER tool must not be ordinary to the local fence.

    Scoped to the stores that sit outside the crew's own data home, which is the class this
    list already covers: ``.aws``, ``.docker/config.json``, ``.kube/config``, ``.ssh`` and the
    rest are all somebody else's credentials under ``$HOME``. The crew's own home is excluded
    deliberately -- the validator classifies well over a hundred leaves under it, this build
    READS that tree by design, and the fence is documented as allowed to be coarser there.

    Measured before this test existed: ``.codex/auth.json``, ``.claude/.credentials.json``,
    ``.local/share/amazon-q`` and ``.local/share/kiro-cli`` were all classified by the shared
    validator and all ordinary to this fence. In the standalone mode this fence is the only
    one, so each omission was a readable credential belonging to another agent.
    """
    from kiro_crew.security import paths as sec_paths
    from kiro_crew.security.paths import is_sensitive_path

    own_home = (".kiro", ".kirocrew")
    leaves = set()
    for name in dir(sec_paths):
        value = getattr(sec_paths, name)
        if isinstance(value, (tuple, list, frozenset, set)):
            for item in value:
                if isinstance(item, str) and "/" in item and item.startswith("."):
                    leaves.add(item)

    mod = load_build()
    home = pathlib.Path.home()
    missed = []
    for entry in sorted(leaves):
        if pathlib.PurePosixPath(entry).parts[0] in own_home:
            continue
        if not is_sensitive_path(f"{home}/{entry}"):
            continue
        if not mod._looks_sensitive_standalone(f"/srv/crew/{entry}"):
            missed.append(entry)
    assert not missed, (
        f"the shared validator classifies these as credential stores belonging to another "
        f"tool, and the standalone fence treats them as ordinary: {missed}. That fence is the "
        f"only one when kiro_crew.security cannot be imported."
    )
