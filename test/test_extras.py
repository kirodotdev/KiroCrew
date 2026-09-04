"""The optional-extra install advice must never name an unresolvable project.

``kirocrew`` is not published on any index, so ``pip install "kirocrew[feishu]"``
fails with *No matching distribution found* for every user. These tests pin the
properties that keep the advice runnable: it names the extra's real
distributions, and every copy of a pin -- the Python map and the specifiers
embedded in the locale catalogs -- stays equal to what ``setup.cfg`` declares.
"""

from __future__ import annotations

import configparser
import json
import re
from pathlib import Path

import pytest

from kiro_crew import extras

_REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_CFG = _REPO_ROOT / "setup.cfg"
LOCALES = _REPO_ROOT / "website" / "src" / "i18n" / "locales"

#: Extras a running gateway advises installing. ``dev`` is test tooling, which
#: no user-facing surface ever offers.
USER_FACING = ("otlp", "perf", "teams", "whatsapp", "feishu", "voice-aws", "voice")


def _declared_extras() -> dict[str, list[str]]:
    """``setup.cfg``'s ``[options.extras_require]``, one list per extra."""
    parser = configparser.ConfigParser()
    parser.read(SETUP_CFG, encoding="utf-8")
    return {
        name: [ln.strip() for ln in raw.splitlines() if ln.strip()]
        for name, raw in parser["options.extras_require"].items()
    }


def _expand(
    name: str, declared: dict[str, list[str]], seen: frozenset[str] = frozenset()
) -> list[str]:
    """Resolve *name* to distributions, expanding self-referential extras."""
    if name in seen:
        return []
    seen = seen | {name}
    out: list[str] = []
    for req in declared[name]:
        nested = re.match(r"^kirocrew\s*\[([^\]]+)\]$", req, re.IGNORECASE)
        if nested is not None:
            for inner in nested.group(1).split(","):
                for spec in _expand(inner.strip(), declared, seen):
                    if spec not in out:
                        out.append(spec)
            continue
        if req not in out:
            out.append(req)
    return out


# ── The property that matters ──


@pytest.mark.parametrize("extra", USER_FACING)
def test_no_surface_ever_names_this_project(extra: str) -> None:
    """Nothing the user is asked to run may mention ``kirocrew[...]``.

    Covers both renderings, because each reaches a different surface: the short
    hint used in logs and CLI output, and the copy-to-clipboard command in the
    dashboard. A regression in either hands the user a command that cannot
    resolve. The interpreter path itself may legitimately contain "kirocrew" (a
    venv inside a checkout), so the assertion targets the extras form -- what
    pip would try, and fail, to resolve.
    """
    reqs = extras.requirements_for_extra(extra)
    assert reqs, f"{extra} resolved to nothing"
    rendered = " ".join(reqs) + extras.install_hint(extra) + extras.pip_install_command(extra)
    assert "kirocrew[" not in rendered.lower()


@pytest.mark.parametrize("extra", USER_FACING)
def test_requirements_match_setup_cfg(extra: str) -> None:
    """The map cannot drift from the declared extras.

    It is the single source the gateway reads, so a stale entry ships a wrong
    pin to real users -- the failure mode this whole module exists to remove.
    """
    assert extras.REQUIREMENTS[extra] == tuple(_expand(extra, _declared_extras()))


def test_every_declared_extra_is_covered() -> None:
    """A new extra in setup.cfg must be added to the map.

    Without this, adding one yields an empty requirement tuple, which renders as
    "no install command" -- a silently unhelpful panel rather than a loud error.
    """
    missing = set(_declared_extras()) - set(extras.REQUIREMENTS) - {"dev"}
    assert (
        not missing
    ), f"extras declared in setup.cfg but absent from REQUIREMENTS: {sorted(missing)}"


# ── The pins embedded in the UI catalogs ──
#
# The Feishu guide and the Teams notice spell a command out inside their
# translated prose, so those specifiers are a SECOND copy of the pin that no
# Python caller reads. Without this test, bumping setup.cfg leaves the same
# panel showing two different commands -- the generated one and a stale
# hand-written one -- across 13 locale files.


def _embedded_specs(text: str, package: str) -> set[str]:
    """Every pinned specifier for *package* in *text*.

    Anchored on a version operator or an extras bracket immediately after the
    name, so a bare prose mention of the package is not read as an unpinned
    spec. Stops at a JSON string escape or a closing tag.
    """
    # The tail is spelled as an allow-list of specifier characters rather than
    # "anything up to a quote": a pin legitimately CONTAINS `<` (as in
    # `<2`), so excluding `<` truncates the spec, while allowing everything up
    # to the next quote runs past a `</mono>` tag into the prose.
    pattern = re.escape(package) + r"(?=[<>=\[])" + r"([A-Za-z0-9_.,\[\]<>=!~^*+-]*)"
    return {package + m.group(1).rstrip("<>=,") for m in re.finditer(pattern, text)}


@pytest.mark.parametrize(
    ("extra", "package"),
    [("feishu", "lark-oapi"), ("teams", "PyJWT")],
)
def test_locale_catalogs_carry_the_declared_pin(extra: str, package: str) -> None:
    expected = extras.REQUIREMENTS[extra][0]
    seen_anywhere = False
    for path in sorted(LOCALES.glob("*.json")):
        found = _embedded_specs(path.read_text(encoding="utf-8"), package)
        assert found <= {
            expected
        }, f"{path.name} embeds {sorted(found - {expected})}, expected {expected}"
        seen_anywhere = seen_anywhere or bool(found)
    assert seen_anywhere, f"no locale embeds a {package} pin; did the key move?"


def test_locale_catalogs_are_valid_json() -> None:
    """The pin substitution above edits raw catalog text, so parse them all."""
    for path in sorted(LOCALES.glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))


# ── Rendering ──


def test_an_unknown_extra_yields_no_command() -> None:
    """Better to render no command than one that installs nothing."""
    assert extras.requirements_for_extra("not-an-extra") == ()
    assert extras.install_hint("not-an-extra") == ""
    assert extras.pip_install_command("not-an-extra") == ""


def test_the_command_names_this_interpreter() -> None:
    """Installing into a different environment than the importer is the bug
    this string exists to prevent."""
    import sys

    assert sys.executable in extras.pip_install_command("feishu")


def test_posix_quotes_specifiers_with_single_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``<`` and ``>`` in a pin are redirection operators unless quoted.

    ``os.name`` is pinned rather than inherited, because the quoting is
    platform-dependent and CI runs this suite on Windows too -- read off the
    host, this test asserted the POSIX form on a runner that correctly emits
    the Windows one. The Windows form has its own test; between them both
    branches are pinned on every runner.
    """
    monkeypatch.setattr(extras.os, "name", "posix")
    assert "'lark-oapi>=1.4,<2'" in extras.pip_install_command("feishu")
    assert "'lark-oapi>=1.4,<2'" in extras.install_hint("feishu")


def test_windows_quotes_specifiers_for_cmd_as_well_as_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint carries no interpreter prefix, so it is read as an ordinary
    command in whatever shell the user has -- and on Windows that may be cmd,
    which does NOT treat ``'`` as a quote. POSIX quoting there leaves the ``<``
    of a bounded pin live, so cmd reads from a file instead of passing the bound
    to pip. Double quotes are the one form cmd and PowerShell both strip."""
    monkeypatch.setattr(extras.os, "name", "nt")
    hint = extras.install_hint("feishu")
    assert hint == 'pip install "lark-oapi>=1.4,<2"'
    assert "'" not in hint


def test_windows_quotes_every_specifier_in_a_multi_package_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unquoted bound is enough to break the command, so the guard has to
    hold for each specifier rather than just the first."""
    monkeypatch.setattr(extras.os, "name", "nt")
    hint = extras.install_hint("voice")
    for req in extras.REQUIREMENTS["voice"]:
        assert f'"{req}"' in hint
    assert "'" not in hint


@pytest.mark.parametrize("extra", USER_FACING)
def test_specifiers_hold_no_powershell_metacharacters(extra: str) -> None:
    """The precondition that makes double quotes safe on Windows.

    PowerShell expands ``$name`` and honours backticks INSIDE double quotes, so
    the Windows form is only correct while no specifier contains one. Today none
    can -- a PEP 508 requirement is a name, extras, and version operators -- but
    a URL or marker in this map would change that silently, and the command
    would corrupt rather than fail. Fail here instead."""
    for req in extras.REQUIREMENTS[extra]:
        assert "$" not in req
        assert "`" not in req
        assert '"' not in req


def test_windows_uses_powershell_literal_quoting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single quotes are PowerShell's literal form; a bare or double-quoted
    path silently expands ``$name`` and backticks, both legal path characters."""
    monkeypatch.setattr(extras.os, "name", "nt")
    monkeypatch.setattr(extras.sys, "executable", r"C:\tools\$py\python.exe")
    command = extras.pip_install_command("feishu")
    # The INTERPRETER keeps PowerShell-literal single quotes (its path can hold
    # `$` and spaces); the SPECIFIER uses the cross-shell double-quoted form.
    expected = "& 'C:\\tools\\$py\\python.exe' -m pip install \"lark-oapi>=1.4,<2\""
    assert command == expected


def test_windows_doubles_a_quote_in_the_interpreter_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A literal single quote is a legal Windows path character; doubling is
    PowerShell's own escape, so the path reaches pip byte-for-byte."""
    monkeypatch.setattr(extras.os, "name", "nt")
    monkeypatch.setattr(extras.sys, "executable", r"C:\tools\o'brien.exe")
    assert extras.pip_install_command("feishu").startswith(
        r"& 'C:\tools\o''brien.exe' -m pip install "
    )


def test_a_multi_package_extra_quotes_every_specifier() -> None:
    """`voice` installs three distributions; each carries its own bounds."""
    command = extras.pip_install_command("voice")
    for req in extras.REQUIREMENTS["voice"]:
        assert extras.quote_spec(req) in command
