"""The scope prompt tells the reviewer which literals refuse its whole candidate set.

``scripts/scope_redact.py --drop-changed-fields`` refuses a candidate set (exit 11)
when a field the classifier reads carries a credential shape. That refusal is
deliberate and stays: rewriting the field would measure an operation nobody
proposed, and dropping the row would remove a boundary without saying so.

The cost lands on the GENERATION side. A change whose subject is ARNs or account
ids invites the reviewer to write rows such as an ``aws sns`` call with a full ARN
in it, and one such row refuses the run on every re-roll. The prompt is read from
the BASE ref, so the change under review cannot correct it for itself.

So ``.github/review-prompts/security-scope.md`` names the refused shapes and the
spellings that carry the same operation without them. These tests keep that
section honest against the redactor it describes:

* every shape rule in the redactor is named in the prompt, so a rule added there
  reddens here until the prompt says so too;
* the example rows the prompt shows pass the scrub unchanged, load as corpus rows,
  pass ``validate``, and classify as allowed with the real deny composite, so the
  advice is a spelling the lane can actually measure.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / ".github" / "review-prompts" / "security-scope.md"
REDACT = ROOT / "scripts" / "scope_redact.py"
CANDIDATES = ROOT / "scripts" / "scope_candidates.py"
DENY_DIFF = ROOT / "scripts" / "deny_diff.py"

BEGIN = "CLEAN-ROW-EXAMPLES-BEGIN"
END = "CLEAN-ROW-EXAMPLES-END"


def _load(name: str, path: Path):
    """Import a script by path, registered in ``sys.modules`` before exec."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


redact = _load("scope_redact_for_prompt", REDACT)
deny_diff = _load("deny_diff_for_prompt", DENY_DIFF)


def _prompt() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _examples_text() -> str:
    text = _prompt()
    assert text.count(BEGIN) == 1 and text.count(END) == 1, "example block sentinels"
    return text.split(BEGIN, 1)[1].split(END, 1)[0]


def _examples() -> dict:
    return json.loads(_examples_text())


#: Each shape rule in ``scope_redact`` and the words the prompt uses for it. Keyed
#: by the module attribute so a renamed or added rule fails the table test below
#: instead of passing silently. ``_SECRET_KEY_RE`` matches a whole JSON key, which
#: a row's classified fields never are, but it names the same three tokens.
_RULE_PHRASES: dict[str, tuple[str, ...]] = {
    "_KEY_ID_RE": ("`AKIA`", "`ASIA`", "16 base32"),
    "_ARN_RE": ("`arn:aws`", "`arn:aws-cn`", "`arn:aws-us-gov`"),
    "_ACCOUNT_RE": ("exactly 12 digits",),
    "_SECRET_ASSIGN_RE": (
        "`aws_secret_access_key`",
        "`aws_session_token`",
        "`x-amz-security-token`",
        "any letter case",
    ),
    "_SECRET_KEY_RE": (
        "`aws_secret_access_key`",
        "`aws_session_token`",
        "`x-amz-security-token`",
    ),
    "_GH_TOKEN_RES": ("`ghp_`", "`gho_`", "`ghu_`", "`ghs_`", "`ghr_`", "`github_pat_`"),
}


def _shape_rules() -> set[str]:
    names = set()
    for name, value in vars(redact).items():
        if isinstance(value, re.Pattern):
            names.add(name)
        elif isinstance(value, tuple) and value and all(isinstance(v, re.Pattern) for v in value):
            names.add(name)
    return names


class TestThePromptNamesEveryRefusedShape:
    def test_the_table_covers_every_shape_rule_in_the_redactor(self) -> None:
        # A new rule in scope_redact refuses rows the prompt does not warn about.
        # This fails until the rule is added to the table AND the prompt.
        assert _shape_rules() == set(_RULE_PHRASES)

    @pytest.mark.parametrize("rule", sorted(_RULE_PHRASES))
    def test_the_prompt_names_each_rule(self, rule: str) -> None:
        text = _prompt()
        missing = [phrase for phrase in _RULE_PHRASES[rule] if phrase not in text]
        assert not missing, f"{rule}: the prompt does not name {missing}"

    def test_the_prompt_keeps_the_unadjudicated_fallback(self) -> None:
        # A boundary that only a literal can carry has no row; the prompt sends it
        # to UNADJUDICATED instead of letting the reviewer write the literal.
        text = _prompt()
        assert "NO CREDENTIAL-SHAPED LITERALS" in text
        assert "name the literal-value\nboundary under `UNADJUDICATED`" in text

    def test_the_prompt_itself_carries_no_refused_literal(self) -> None:
        # The reviewer copies what it reads, so every shape is described in words.
        # The one exception is the ARN rule's own prefixes, which the prompt has
        # to name; anything longer would be an ARN the reviewer could copy.
        text = _prompt()
        arns = set(redact._ARN_RE.findall(text))
        assert arns <= {"arn:aws", "arn:aws-cn", "arn:aws-us-gov"}, arns
        stripped = redact._ARN_RE.sub("", text)
        assert redact.redact_text(stripped) == stripped


class TestTheExampleRowsAreMeasurable:
    def test_the_examples_cover_both_platform_spellings(self) -> None:
        platforms = {row["platform"] for row in _examples()["golden_paths"]}
        assert {"posix", "windows"} <= platforms

    def test_the_examples_pass_the_classified_field_scrub_unchanged(self) -> None:
        text = json.dumps(_examples())
        # Raises ClassifiedFieldShaped (the lane's exit 11) on a shaped field.
        _, removed = redact.drop_changed_fields(text)
        assert removed == 0
        _, changed = redact.redact_json_text(text)
        assert not changed

    def test_the_examples_carry_no_placeholder(self) -> None:
        for row in _examples()["golden_paths"]:
            command = row["command_or_flow"]
            assert "[REDACTED" not in command, command
            assert not re.search(r"<[a-z][a-z-]*>", command), command

    def test_the_examples_load_as_corpus_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "examples.json"
        path.write_text(json.dumps(_examples()), encoding="utf-8")
        rows = deny_diff.load_corpus(path)
        assert len(rows) == len(_examples()["golden_paths"])

    def test_the_examples_pass_validate(self, tmp_path: Path) -> None:
        candidates = tmp_path / "candidates.json"
        candidates.write_text(json.dumps(_examples()), encoding="utf-8")
        out = tmp_path / "normalized.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(CANDIDATES),
                "validate",
                "--candidates",
                str(candidates),
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        normalized = json.loads(out.read_text(encoding="utf-8"))
        assert len(normalized["golden_paths"]) == len(_examples()["golden_paths"])

    def test_the_examples_classify_as_allowed(self, tmp_path: Path) -> None:
        # The advice is only useful if the rows are operations the composite
        # allows today: a row refused at base can never show a regression.
        commands = [row["command_or_flow"] for row in _examples()["golden_paths"]]
        verdicts, absent = deny_diff.classify(ROOT, commands, home=tmp_path)
        assert not absent
        denied = [c for c, v in zip(commands, verdicts) if v.denied]
        assert not denied, denied
