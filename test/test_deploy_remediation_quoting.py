"""The finite-TTL 409's copyable remediation must be safe to paste.

The deploy handler answers a finite-TTL request with a 409 whose
``remediation`` is a command the dashboard shows behind a copy button. Its
``--profile`` and ``--region`` words come from the deploy profile registry,
an agent-writable file, so a stored value such as ``us-west-2;touch /tmp/x``
must reach the pasted shell as ONE literal argument instead of a second
command. Two layers hold that line: the renderers quote every word, and the
registry loader drops an entry whose name or region fails the request-path
field spec.
"""

from __future__ import annotations

import json
import shlex

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.deploy import handlers_guards as guards
from kiro_crew.deploy import profiles as profiles_mod

# Each value breaks a different shell rule: a command separator, a parameter
# expansion, whitespace, both quote styles and a command substitution.
HOSTILE_REGION = "us-west-2;touch${IFS}/tmp/pwned"
HOSTILE_NAME = "p q'\"$(id)"


def _shell_words(cmd: str) -> list[str]:
    """Split ``cmd`` the way ``sh`` would, with ``;``/``&&`` as their own words.

    ``shlex.split`` alone treats ``;`` as an ordinary character, so an
    unquoted ``a;b`` would still come back as one word and hide the bug; the
    punctuation-aware lexer shows the break the shell would make.
    """
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


@pytest.fixture
def _skills(monkeypatch, tmp_path):
    fake = tmp_path / "skills"
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: fake)
    return fake


@pytest.mark.parametrize(
    "render", [guards._reaper_remediation, guards._base_remediation], ids=["reaper", "base"]
)
def test_hostile_profile_and_region_stay_single_arguments(_skills, render):
    cmd = render(HOSTILE_NAME, HOSTILE_REGION)
    words = _shell_words(cmd)
    # The literal values survive as one word each, right after their flag.
    assert words[words.index("--region") + 1] == HOSTILE_REGION
    assert words[words.index("--profile") + 1] == HOSTILE_NAME
    # Nothing in the hostile region escaped as a separate command.
    assert ";" not in words
    assert not any(w.startswith("touch") for w in words)


def test_base_remediation_quotes_both_steps(_skills):
    cmd = guards._base_remediation(HOSTILE_NAME, HOSTILE_REGION)
    words = _shell_words(cmd)
    # The only chain operator is the one joining the base step to the reaper.
    assert words.count("&&") == 1
    assert words.count(HOSTILE_REGION) == 2
    assert words.count(HOSTILE_NAME) == 2


def test_template_and_script_paths_are_quoted_words(monkeypatch, tmp_path):
    """A skills root containing a space is one word for the shell, too."""
    fake = tmp_path / "my skills"
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: fake)
    cmd = guards._base_remediation("p", "us-west-2")
    words = _shell_words(cmd)
    assert (
        words[words.index("--template-file") + 1]
        == (fake / "artifact-deploy" / "templates" / "base-stack.yaml").as_posix()
    )
    assert (
        words[words.index("&&") + 1]
        == (fake / "artifact-deploy" / "scripts" / "install-reaper.sh").as_posix()
    )


def test_safe_values_render_without_quotes(_skills):
    """The common case reads exactly as before: no quotes around plain words."""
    cmd = guards._reaper_remediation("my-sso+dev", "us-gov-west-1")
    assert cmd.endswith("install-reaper.sh --profile my-sso+dev --region us-gov-west-1")
    assert "'" not in cmd


def _seed_registry(tmp_path, monkeypatch, entries):
    reg = tmp_path / "profiles.json"
    reg.write_text(
        json.dumps({"version": 2, "profiles": entries, "default": entries[0]["name"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: reg)
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: tmp_path / "nope2.json")


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "p", "region": HOSTILE_REGION},
        {"name": HOSTILE_NAME, "region": "us-west-2"},
        {"name": "-starts-like-an-option", "region": "us-west-2"},
        {"name": "p", "region": 7},
        {"name": "p\x00", "region": "us-west-2"},
        {"name": "p", "region": " us-west-2"},
        {"name": 123456789012, "region": "us-west-2"},
    ],
    ids=[
        "hostile-region",
        "hostile-name",
        "option-shaped-name",
        "non-string-region",
        "nul-in-name",
        "padded-region",
        "numeric-name",
    ],
)
def test_registry_drops_an_entry_the_request_path_would_refuse(tmp_path, monkeypatch, entry):
    """The stored default is served without a request parameter to validate,
    so the loader applies the request path's field specs itself.

    The NUL and padding cases pin that the RAW stored value must pass: the
    validator's sanitized copy would pass while the raw string still reaches
    ``aws`` argv (an embedded NUL raises out of ``subprocess``). The numeric
    case pins that a bare-number name is still recognised as THE dropped
    default (JSON keeps it an int, ``default`` is a str), so it clears rather
    than falling back to another account's profile."""
    _seed_registry(tmp_path, monkeypatch, [entry, {"name": "ok", "region": "eu-west-1"}])
    reg = profiles_mod.load_registry()
    assert [p["name"] for p in reg["profiles"]] == ["ok"]
    # The dropped entry was the default: it clears rather than silently
    # pointing at another account.
    assert reg["default"] == ""
    assert profiles_mod.resolve_profile("") is None
    assert profiles_mod.resolve_profile("ok") == ("ok", "eu-west-1")


def test_registry_keeps_an_entry_with_an_empty_region(tmp_path, monkeypatch):
    """An empty region is not malformed: make_entry fills in the default."""
    _seed_registry(tmp_path, monkeypatch, [{"name": "p", "region": ""}])
    reg = profiles_mod.load_registry()
    assert reg["profiles"][0]["region"] == profiles_mod.DEFAULT_REGION


def test_registry_keeps_names_the_aws_control_app_registers(tmp_path, monkeypatch):
    """The AWS control app registers names with ``@`` and ``=``; the loader
    must accept the union of every writer's charset, and a write must keep
    the entry on disk. Both characters are inert once ``shlex.join`` quotes
    them."""
    _seed_registry(
        tmp_path,
        monkeypatch,
        [
            {"name": "dev@corp", "region": "eu-central-1"},
            {"name": "role=admin", "region": "us-east-1"},
        ],
    )
    reg = profiles_mod.load_registry()
    assert [p["name"] for p in reg["profiles"]] == ["dev@corp", "role=admin"]
    assert reg["default"] == "dev@corp"
    assert profiles_mod.resolve_profile("") == ("dev@corp", "eu-central-1")
    cmd = guards._reaper_remediation("dev@corp", "eu-central-1")
    assert _shell_words(cmd)[-3:] == ["dev@corp", "--region", "eu-central-1"]
    # A no-op locked write keeps the entry on disk.
    monkeypatch.setattr(profiles_mod, "_data_dir", lambda: tmp_path)
    with profiles_mod.locked_registry():
        pass
    on_disk = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    assert [p["name"] for p in on_disk["profiles"]] == ["dev@corp", "role=admin"]
    assert on_disk["default"] == "dev@corp"


def test_a_dropped_default_clears_instead_of_switching_accounts(tmp_path, monkeypatch):
    """A default whose own entry was dropped must not fall back to another
    profile: that would send a deploy naming no profile to a different
    account. A default that was never present still falls back, as before."""
    _seed_registry(
        tmp_path,
        monkeypatch,
        [{"name": "p", "region": HOSTILE_REGION}, {"name": "other", "region": "us-east-1"}],
    )
    reg = profiles_mod.load_registry()
    assert [p["name"] for p in reg["profiles"]] == ["other"]
    assert reg["default"] == ""
    assert profiles_mod.resolve_profile("") is None
    reg_path = tmp_path / "profiles.json"
    reg_path.write_text(
        json.dumps(
            {
                "version": 2,
                "default": "gone",
                "profiles": [{"name": "other", "region": "us-east-1"}],
            }
        ),
        encoding="utf-8",
    )
    assert profiles_mod.load_registry()["default"] == "other"


def test_legacy_single_profile_config_is_validated_too(tmp_path, monkeypatch):
    legacy = tmp_path / "config.json"
    legacy.write_text(json.dumps({"profile": "p", "region": HOSTILE_REGION}), encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: tmp_path / "nope2.json")
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: legacy)
    assert profiles_mod.load_registry() == {"version": 2, "profiles": [], "default": ""}
