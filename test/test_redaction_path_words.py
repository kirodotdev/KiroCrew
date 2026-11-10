"""Pass 3 leaves a CamelCase workspace path alone and still finds a key beside it.

A Brazil-style workspace path (``<ws>/src/<Package>/...``) whose package name carries
digits and an acronym is one base64-alphabet run, and windows straddling the package
name and the next component cleared every gate of the bare-secret pass. The path came
back as ``[REDACTED: credential]``, in chat and in the ``tool_input`` a hook reads.
``_is_spelled_like_a_path`` declines a fragment window that crosses a ``/`` and holds
five capitalised words, or four on three separators. These tests pin both sides: the
paths survive, and keys -- standalone, slash-bearing, beside a path, glued into one --
are still redacted.
"""

from __future__ import annotations

import json
import random
import string

import pytest

from kiro_crew.security import redaction_entropy
from kiro_crew.security.redaction import REDACTED_CREDENTIAL_TAG, redact_credentials

#: The canonical AWS documentation example key. It holds two ``/``.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
#: A second slash-bearing key, so no claim rests on one fixture's letters.
ALT_KEY = "Kx3Q51tPusV/D0URlGfM" "mNbVc7Z8yJhLpQrStUwZ"
#: A random key-shaped token whose own 40 characters cross one ``/`` and hold five
#: capitalised words, found by seeded search; the accepted residual below is about it.
WORDY_KEY = "xZQDvniU6/UbcEbt1vtJmyrZhmiZAqufDCcvSMDH"
#: A key carrying the ceiling's three separators of its own and four words: the
#: 597th 40-char draw from Random(11). Its own separators must not make a path run.
THREE_SLASH_WORDY_KEY = "Iv1f60/zj7c2/HxlUfiJNizK/ObjDh2XVXie3pX7"
#: A stand-in with the reported package name's shape: digits, an acronym, CamelCase.
PKG = "Corp126DSTRobotWorksApp"
ROOT = "/home/someone/workspace/RoboWorks/worktrees/rw-ui-changes-T2.4"
WS = "/home/someone/ws/RoboWorks/src"

REPORTED_PATHS = (
    # The reported write target: the run after `T2.` is 49 characters.
    f"{ROOT}/src/{PKG}/ui/src/FolderBrowser.tsx",
    # The same workspace without a worktree segment to break the run.
    f"{WS}/{PKG}/BrazilBuildTools/README.md",
    # The spec documents a spec-builder app reads back.
    f"{WS}/{PKG}/specs/FolderBrowser/requirements.md",
    f"{WS}/{PKG}/specs/FolderBrowser/design.md",
    f"{WS}/{PKG}/specs/FolderBrowser/tasks.md",
)

NEARBY_PATHS = (
    # Acronym- and digit-heavy components, whose capitals count as no word.
    f"{WS}/{PKG}/ECRUpload2/src/s3Put.ts",
    f"{WS}/{PKG}/ECRUpload2/src/AWS4SigV4Test.ts",
    f"{WS}/{PKG}/ECRUpload2/lib/S3DAOTest.ts",
    "/home/someone/ws/src/MyService/IAMRoleUtil/KMSKey3/DSTStack2.ts",
    # Digits in several components.
    f"/home/someone/ws/Build2024/src/{PKG}9/ui/src/Panel3Widget2/FolderBrowser9.tsx",
    # One very long component after the package.
    f"/home/someone/src/{PKG}/VeryLongGeneratedFolderBrowserPanelComponentNameForTests.tsx",
    # A Windows path written with forward slashes (backslashes already split the run).
    f"C:/Users/someone/ws/RoboWorks/src/{PKG}/ui/src/FolderBrowser.tsx",
    "C:\\Users\\someone\\ws\\RoboWorks\\src\\" + PKG + "\\ui\\src\\FolderBrowser.tsx",
    # Shorter runs that never fired, kept so the fix cannot regress them.
    f"{ROOT}/src/{PKG}/ui/src/BoardPanel.tsx",
    f"{ROOT}/src/{PKG}/ui/src/runForm.ts",
    f"{ROOT}/src/{PKG}/ui/src/__tests__/x.tsx",
)


def _rule_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable both forms of the rule: no window holds 41 words."""
    off = redaction_entropy._SECRET_KEY_LEN + 1
    monkeypatch.setattr(redaction_entropy, "_PATH_WORDS_MIN", off)
    monkeypatch.setattr(redaction_entropy, "_PATH_WORDS_MIN_AT_CEILING", off)


@pytest.mark.parametrize("path", REPORTED_PATHS + NEARBY_PATHS)
def test_a_camelcase_workspace_path_survives(path: str) -> None:
    assert redact_credentials(path) == (path, [])


@pytest.mark.parametrize("path", REPORTED_PATHS)
def test_the_path_survives_inside_json(path: str) -> None:
    """The shape a hook reads: the path as a string value in tool input JSON."""
    payload = json.dumps({"tool_name": "fs_write", "tool_input": {"path": path, "text": "x"}})
    assert redact_credentials(payload) == (payload, [])


@pytest.mark.parametrize("path", REPORTED_PATHS + NEARBY_PATHS[:3])
def test_the_word_rule_is_what_saves_the_path(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch the rule off and every reported and acronym-heavy path is redacted again."""
    _rule_off(monkeypatch)
    result, warnings = redact_credentials(path)
    assert REDACTED_CREDENTIAL_TAG in result
    assert warnings


@pytest.mark.parametrize(
    "label,text,key",
    [
        ("standalone", KEY, KEY),
        ("second standalone", ALT_KEY, ALT_KEY),
        (
            "in JSON beside a path",
            json.dumps({"path": f"/home/s/src/{PKG}/ui/a.ts", "k": KEY}),
            KEY,
        ),
        ("as the component after the package", f"/home/someone/src/{PKG}/{KEY}", KEY),
        (
            "second as the component after the package",
            f"/home/someone/src/{PKG}/{ALT_KEY}",
            ALT_KEY,
        ),
        ("glued to the package name", f"/home/someone/src/{PKG}{KEY}", KEY),
        ("glued on both sides after the package", f"/home/someone/src/{PKG}/X{KEY}X", KEY),
        (
            "after a path and a space",
            f"/home/someone/src/{PKG}/ui/src/FolderBrowser.tsx {KEY}",
            KEY,
        ),
        ("before a path component", f"/home/someone/src/{PKG}/{KEY}/ui/src/FolderBrowser.tsx", KEY),
    ],
)
def test_a_key_beside_or_inside_a_workspace_path_is_redacted(
    label: str, text: str, key: str
) -> None:
    result, warnings = redact_credentials(text)
    assert key not in result, f"{label}: key leaked"
    assert REDACTED_CREDENTIAL_TAG in result, label
    assert warnings, label


def test_a_standalone_key_spelled_like_a_path_is_still_redacted() -> None:
    """The rule reads only a fragment: a 40-char run is the token somebody wrote."""
    assert redaction_entropy._looks_like_secret_key(WORDY_KEY)
    assert redaction_entropy._is_spelled_like_a_path(WORDY_KEY)
    for text in (WORDY_KEY, f"secret: {WORDY_KEY}\n", json.dumps({"k": WORDY_KEY})):
        assert WORDY_KEY not in redact_credentials(text)[0]


@pytest.mark.parametrize(
    "fmt", ["X{}X", "the value was X{}X, rotate it", '{{"k": "pre{}post"}}', "{}+abc"]
)
def test_a_key_glued_into_prose_is_redacted_even_when_spelled_like_a_path(fmt: str) -> None:
    """Outside a path run the words are not read: the key brings its only separators."""
    for key in (WORDY_KEY, THREE_SLASH_WORDY_KEY):
        text = fmt.format(key)
        result, warnings = redact_credentials(text)
        assert key not in result
        assert warnings


def test_a_key_with_three_separators_of_its_own_spells_a_path() -> None:
    """Pin the premise of the glued-prose case above for the three-separator key."""
    assert THREE_SLASH_WORDY_KEY.count("/") == 3
    assert redaction_entropy._looks_like_secret_key(THREE_SLASH_WORDY_KEY)
    assert redaction_entropy._is_spelled_like_a_path(THREE_SLASH_WORDY_KEY)


def test_the_accepted_residual_is_a_glued_key_spelled_like_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DOCUMENTS AN ACCEPTED LOSS. This asserts what is NOT redacted.

    A key whose own 40 characters are spelled like a path, glued into a path run so
    every window over it is a fragment with separators beyond its own, is declined
    like a path window. At this layer the two are the same bytes. The loss needs a
    key that spells the words (0.14% of random key-shaped tokens,
    ``test_few_random_keys_are_spelled_like_a_path``) and is concatenated into a path
    with no delimiter; measured on 100,000 random keys, this rule adds 53 leaks at
    ``/srv/ci/keys/X<key>X`` and 1 glued into prose. With the rule off it is found, which shows this rule and no other
    gate lets it through.
    """
    glued = f"/srv/ci/keys/X{WORDY_KEY}X"
    assert WORDY_KEY in redact_credentials(glued)[0]
    _rule_off(monkeypatch)
    assert WORDY_KEY not in redact_credentials(glued)[0]


def test_few_random_keys_are_spelled_like_a_path() -> None:
    """Bound the rule's cost on uniformly random key-shaped tokens (seeded, so exact)."""
    rng = random.Random(11)
    alphabet = string.ascii_letters + string.digits + "+/"
    shaped = 0
    declined = 0
    while shaped < 20_000:
        token = "".join(rng.choice(alphabet) for _ in range(redaction_entropy._SECRET_KEY_LEN))
        if not redaction_entropy._looks_like_secret_key(token):
            continue
        shaped += 1
        declined += redaction_entropy._is_spelled_like_a_path(token)
    assert declined / shaped < 0.0025, declined


@pytest.mark.parametrize(
    "window,expected",
    [
        ("Lab126DSTDroidWorksApp/ui/src/FolderBrow", True),
        # Four words on exactly the ceiling's three separators.
        ("tWorksApp/ECRUpload2/src/AWS4SigV4Test", True),
        # Four words on two separators is not enough.
        ("RobotWorksApp/ECRUpload2/s3Px4X", False),
        # Four words across one separator is not enough.
        ("Corp126DSTRobotWorksApp/CONTRIBUTINGXYZ1", False),
        # No separator, however wordy: not a path window.
        ("FolderBrowserPanelWidgetRobotWorksApp126", False),
        # A word needs a vowel and two lowercase letters after its capital.
        ("Xyz/Bcd/Fgh/Jkl/Mnp/Qrs/Tvw/X1/Y2/Z3/W4A", False),
    ],
)
def test_what_counts_as_spelled_like_a_path(window: str, expected: bool) -> None:
    assert redaction_entropy._is_spelled_like_a_path(window) is expected
