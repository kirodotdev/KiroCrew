"""The KAS ``file://`` prompt fence: relative and absolute refs meet one check.

``resolve_prompt`` reads a ``file://`` prompt and ships the text over the wire, so
the RESOLVED path (symlinks followed) is what is judged. A relative ref may climb
out of the agents directory with ``..``, as on kiro-cli; it gains nothing an
absolute ref does not have, and both are refused at the same locations.

``HOME`` points at *tmp_path* so a relative ref can climb to a credential
location under it. The credential files are never created, except the symlink
target: the refusal is on the path, before any read.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew.acp.kas_agents import KasAgentTranslationError, resolve_prompt
from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX home layout")

_HOME_CREDENTIALS = [".ssh/id_rsa", ".aws/credentials", ".config/gh/hosts.yml", ".netrc"]


@pytest.fixture
def agents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return agents


def _resolve(ref, agents):
    return resolve_prompt({"prompt": ref}, agent_id="a", agents_dir=agents)


@pytest.mark.parametrize("rel", _HOME_CREDENTIALS)
def test_a_relative_climb_to_a_credential_is_refused(agents, rel):
    with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
        _resolve(f"file://../../{rel}", agents)


@pytest.mark.parametrize("rel", _HOME_CREDENTIALS)
def test_the_same_credential_named_absolutely_is_refused(agents, rel):
    home = agents.parent.parent
    for ref in (f"file://~/{rel}", f"file://{home / rel}"):
        with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
            _resolve(ref, agents)


def test_the_gh_token_store_is_fenced(agents):
    home = agents.parent.parent
    assert is_unsafe_prompt_path((home / ".config" / "gh" / "hosts.yml").resolve())
    assert not is_unsafe_prompt_path((home / ".config" / "ghostty" / "p.md").resolve())


def test_the_app_receipt_key_is_refused(agents):
    from kiro_crew.apps.install_receipt import _SECRET_FILE
    from kiro_crew.config.paths import config_dir

    home = agents.parent.parent
    for data_home in (config_dir(), home / ".kiro" / "crew"):
        with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
            _resolve(f"file://{data_home / _SECRET_FILE}", agents)
    with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
        _resolve(f"file://../crew/{_SECRET_FILE}", agents)


def test_a_session_token_mapping_is_refused(agents):
    from kiro_crew.config.paths import config_dir
    from kiro_crew.session_token_sig import _sig_path

    home = agents.parent.parent
    for data_home in (config_dir(), home / ".kiro" / "crew"):
        name = _sig_path("tok", data_home).name
        with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
            _resolve(f"file://{data_home / name}", agents)
    assert not is_unsafe_prompt_path((home / "notes" / name).resolve())


def test_a_symlinked_gh_config_dir_is_still_fenced(agents, tmp_path):
    # A dotfile manager links ~/.config/gh elsewhere; gh writes hosts.yml through
    # the link, so the resolved token file is outside the lexical anchor.
    home = agents.parent.parent
    dotfiles = tmp_path / "dotfiles" / "gh"
    dotfiles.mkdir(parents=True)
    (dotfiles / "hosts.yml").write_text("oauth_token: x", encoding="utf-8")
    (home / ".config").mkdir()
    (home / ".config" / "gh").symlink_to(dotfiles)
    with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
        _resolve("file://../../.config/gh/hosts.yml", agents)


def test_a_symlink_to_a_credential_is_refused(agents):
    home = agents.parent.parent
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    (agents / "prompt.md").symlink_to(home / ".ssh" / "id_rsa")
    for ref in ("file://./prompt.md", f"file://{agents / 'prompt.md'}"):
        with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
            _resolve(ref, agents)


def test_a_relative_climb_to_an_ordinary_file_loads(agents):
    shared = agents.parent / "prompts"
    shared.mkdir()
    (shared / "wizard.md").write_text("shared prompt", encoding="utf-8")
    assert _resolve("file://../prompts/wizard.md", agents) == "shared prompt"


def test_an_absolute_prompt_outside_home_still_loads(agents, tmp_path):
    p = tmp_path / "elsewhere" / "p.md"
    p.parent.mkdir()
    p.write_text("absolute", encoding="utf-8")
    assert _resolve(f"file://{p}", agents) == "absolute"
