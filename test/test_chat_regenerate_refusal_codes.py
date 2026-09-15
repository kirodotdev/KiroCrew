"""One spelling per refusal condition, across the dashboard.

`test_error_code_contract.py` is the repo's gate for whether a refusal HAS a
code, and `error-code-baseline.json` is its per-file worklist. Neither says
anything about whether two modules refusing the SAME condition agree on the
code they use -- and a code is only worth branching on if they do, otherwise a
client needs one lookup table per endpoint and the shared vocabulary is a
fiction.

That is the gap this file covers, and the only one: the "no bare refusal body"
scan lives in the contract test, which reads the AST and so also catches the
hoisted-body and dynamic-status shapes a regex here would miss.

The scan runs over whole-file text rather than line by line, because black
wraps a long refusal so the sentence and the code land on DIFFERENT lines --
it did exactly that to three sites in this module. A line-scoped scan silently
skips every wrapped site, which is the worst failure available to a guard: it
passes while the thing it promises to pin goes unchecked. `test_the_pair_
pattern_still_matches` is what keeps that honest.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
MODULE = DASHBOARD / "chat_regenerate.py"

# An "error" sentence and the "code" beside it, tolerating the line break black
# inserts between them. `[^"]+` for the sentence and an immediate comma keep a
# match inside ONE body: two separate json_response calls always have `}` and
# `status=` between them, which \s* cannot span.
PAIR = re.compile(r'"error":\s*"(?P<sentence>[^"]+)"\s*,\s*"code":\s*"(?P<code>[a-z_]+)"', re.S)

# Sentences whose code is settled across the whole dashboard tree, so a second
# spelling is drift. Kept deliberately small: a sentence earns a place here only
# once a recursive scan shows it already has exactly one spelling, otherwise the
# guard reds on code it never asked to change.
#
# NOT here, and each for its own reason:
#
# - "not found" is a generic sentence whose SUBJECT differs per endpoint, and the
#   tree correctly pairs it with five codes (slot_not_found, not_found,
#   pin_not_found, instance_not_found, launch_job_not_found). There the code
#   carries information the sentence does not, so demanding one spelling would
#   be demanding a worse API.
#
# - "invalid JSON" is settled at 44 sites as `invalid_json` and diverges at
#   exactly one: `handlers/security.py:1535` answers an UNPARSEABLE body with
#   `invalid_value`, the code its own next guard uses for a parseable body whose
#   `value` has the wrong type -- so that endpoint currently returns one code for
#   two conditions, and its own audit line on the line above calls the branch
#   `invalid_json`. It looks like a genuine bug, but fixing it changes a security
#   endpoint's response contract and the test that pins it
#   (`test_allow_all_rejects_unparseable_body_with_code`), which belongs in that
#   endpoint's own change with its own review, not folded into this one. Pinning
#   the sentence here before then would only red on existing code.
SETTLED = {
    "slot is running": "slot_running",
}


def test_settled_conditions_use_one_spelling_across_the_dashboard() -> None:
    divergent: list[str] = []
    seen: dict[str, int] = {s: 0 for s in SETTLED}
    # rglob, not glob: `dashboard/handlers/` and `dashboard/routes/` hold most of
    # the tree's refusals (~60 `invalid_json` sites between them), so a
    # top-level-only scan would claim "across the dashboard" while never reading
    # the files where drift is most likely. It found a real one when widened:
    # `handlers/security.py` answered an unparseable body with `invalid_value`
    # while its own audit line called the same branch `invalid_json`.
    for path in sorted(DASHBOARD.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in PAIR.finditer(text):
            want = SETTLED.get(m.group("sentence"))
            if want is None:
                continue
            seen[m.group("sentence")] += 1
            if m.group("code") != want:
                line = text[: m.start()].count("\n") + 1
                rel = path.relative_to(DASHBOARD)
                divergent.append(f"{rel}:{line} -> {m.group('code')!r} (want {want!r})")
    # Reuse the settled spelling, or if the condition genuinely differs, give it
    # a sentence of its own rather than a second code for the same words.
    assert divergent == []
    # A rename that empties the scan must fail rather than pass vacuously.
    assert all(n > 0 for n in seen.values()), seen


def test_this_module_carries_the_settled_spellings() -> None:
    text = MODULE.read_text(encoding="utf-8")
    found = {m.group("sentence"): m.group("code") for m in PAIR.finditer(text)}
    assert found.get("slot is running") == "slot_running"
    # Not in SETTLED (see its note), but this module still follows the tree's
    # 44-site spelling, so a copy-paste of the outlier into here would show up.
    assert found.get("invalid JSON") == "invalid_json"


def test_the_pair_pattern_still_matches() -> None:
    """Without this, a regex edit makes the guard above pass on zero sites."""
    single = 'web.json_response({"error": "slot is running", "code": "slot_running"}, status=409)'
    assert PAIR.search(single).group("code") == "slot_running"

    # The shape black produces when the call is too long for one line: the
    # sentence and the code are still one payload, but not one line.
    wrapped = (
        "web.json_response(\n"
        '    {"error": "slot is running",\n'
        '     "code": "slot_running"},\n'
        "    status=409,\n"
        ")"
    )
    assert PAIR.search(wrapped).group("code") == "slot_running"

    # And it must not bridge two ADJACENT bodies: the sentence of the first
    # must never pair with the code of the second.
    two = (
        'return web.json_response({"error": "slot is running"}, status=409)\n'
        'return web.json_response({"error": "no variants", "code": "no_variants"}, status=400)\n'
    )
    pairs = [(m.group("sentence"), m.group("code")) for m in PAIR.finditer(two)]
    assert pairs == [("no variants", "no_variants")]
