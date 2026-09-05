"""The shipped md-notebook backend can see its own state files (#8762).

``_CREW_HIDDEN_LEAVES`` bind-masks ``workspace/md-notebook/{pat,vaults.json,
settings.json}`` in every sandbox mode so the AGENT's spawned subprocesses
cannot read the GitHub token or rewrite the vault list. PR #7439 applied that
mask unconditionally — including to the md-notebook BACKEND, which is itself
spawned under the same sandbox (``apps/backend.py`` ``wrap_argv``) and OWNS
those files, so every attach/clone failed with EPERM.

These tests pin the carve-out: :func:`md_notebook_backend_state_paths` names
every masked spelling of exactly those three leaves, and passing them as
``extra_visible_dirs`` un-hides them in BOTH platform builders (Linux launcher
+ macOS seatbelt) without re-sealing them read-only and without weakening any
sibling mask entry. The spawn-site condition (shipped-builtin provenance only)
is pinned in ``test_app_backend.py``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import kiro_crew.sandbox as sb

_LEAF_NAMES = {"pat", "vaults.json", "settings.json"}
_MARKER = f"{os.sep}md-notebook{os.sep}"


def _launcher_list(script: str, name: str) -> list[str]:
    match = re.search(rf"^{name} = (\[.*\])$", script, re.MULTILINE)
    assert match, f"{name} not found in the generated launcher script"
    return json.loads(match.group(1))


def test_the_carveout_names_every_live_spelling_of_exactly_the_backend_leaves():
    paths = sb.md_notebook_backend_state_paths()

    # Exactly the three state files plus the staging dir, never more: the
    # carve-out is the ONE hole in the crew-home mask, so it must not be able
    # to widen past them.
    assert {os.path.basename(p) for p in paths} == _LEAF_NAMES | {".staging"}
    for path in paths:
        assert (
            _MARKER in path or "/md-notebook/" in path
        ), f"carve-out path escapes the md-notebook state dir: {path!r}"

    # Every LIVE spelling is covered: the default home prefix plus the
    # relocated data home. The deprecated ~/.kirocrew prefix stays MASKED and
    # uncarved — no resolver returns it, so unmasking it would expose a stale
    # legacy PAT copy for zero functional gain (First Principles review).
    home = str(Path.home())
    live = [
        os.path.join(home, sb._CREW_HOME_DEFAULT, leaf)
        for leaf in sb._MD_NOTEBOOK_BACKEND_CARVEOUT_LEAVES
    ]
    live += sb._relocated_crew_targets(sb._MD_NOTEBOOK_BACKEND_CARVEOUT_LEAVES)
    assert set(live) <= set(paths)
    assert not [
        p for p in paths if f"{os.sep}.kirocrew{os.sep}" in p
    ], "the carve-out must not unmask the deprecated home spelling"


def test_a_data_home_beneath_a_foreign_mask_is_refused_not_carved(monkeypatch, tmp_path, caplog):
    """``extra_visible_dirs`` cancels any hidden entry that CONTAINS a visible
    path, so a data home placed beneath an independently masked directory
    (``KIROCREW_HOME`` under a credential tree) would take that WHOLE foreign
    mask down with the carve-out. The helper must drop such a spelling — the
    backend keeps its EPERM there, which is strictly safer than unmasking a
    credential directory — and both platform builders must keep the ancestor's
    rules."""
    import logging

    # A fake home under tmp_path so nothing touches the operator's real home;
    # ``.kiro/crew-auth-staging`` is a real _STANDARD_DIRS entry, so a data
    # home beneath it exercises the exact ancestor-unhide shape without naming
    # a third-party credential path.
    fake_home = tmp_path / "home"
    ancestor = fake_home / ".kiro" / "crew-auth-staging"
    data_home = ancestor / "crew-home"
    data_home.mkdir(parents=True)
    monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(sb, "config_dir", lambda: data_home)
    # Force a re-prime against the fake data home; monkeypatch restores the
    # original cache afterward so later tests see the real one.
    monkeypatch.setattr(sb, "_voice_runtime_paths_cache", None)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        paths = sb.md_notebook_backend_state_paths()

    shadowed = [p for p in paths if p.startswith(str(ancestor) + os.sep)]
    assert not shadowed, f"a carve-out spelling beneath the foreign mask survived: {shadowed!r}"
    assert any(
        "crew-auth-staging" in r.getMessage() for r in caplog.records
    ), "the refusal must name the offending ancestor so the misconfiguration is actionable"

    # And the builders keep the foreign mask even when handed the carve-out.
    script = sb._build_launcher_script("standard", extra_visible_dirs=paths)
    assert str(ancestor) in _launcher_list(
        script, "SENSITIVE_DIRS"
    ), "the foreign ancestor mask was dropped from the Linux launcher"
    profile = sb._build_seatbelt_profile("standard", extra_visible_dirs=paths)
    assert (
        f'(deny file-read* (subpath "{ancestor}"))' in profile
    ), "the foreign ancestor read deny was dropped from the seatbelt profile"


def test_the_leaves_stay_masked_without_the_carveout():
    """The control: the fix is a per-spawn carve-out, not a mask removal."""
    script = sb._build_launcher_script("standard")
    hidden = _launcher_list(script, "SENSITIVE_DIRS")
    assert [p for p in hidden if os.path.basename(p) in _LEAF_NAMES and "md-notebook" in p], (
        "the md-notebook state mask disappeared from the launcher entirely — "
        "these tests pin a carve-out for one spawn, never an unmasking for all"
    )
    assert [p for p in hidden if p.endswith(f"md-notebook{os.sep}.staging")], (
        "the staging-directory mask disappeared from the launcher — during a "
        "write (and after a crash) it holds the same PAT bytes as the leaves"
    )

    profile = sb._build_seatbelt_profile("standard")
    assert (
        "md-notebook" in profile
    ), "the md-notebook state mask disappeared from the seatbelt profile entirely"


def test_the_launcher_unhides_the_leaves_with_the_carveout():
    paths = sb.md_notebook_backend_state_paths()
    script = sb._build_launcher_script("standard", extra_visible_dirs=paths)
    hidden = _launcher_list(script, "SENSITIVE_DIRS")
    files = _launcher_list(script, "SENSITIVE_FILES")
    # Every LIVE spelling the backend reaches is unhidden…
    leaked = [p for p in hidden + files if p in paths]
    assert (
        not leaked
    ), f"the carve-out left md-notebook state masked, the backend still EPERMs: {leaked!r}"

    # …while the deprecated ~/.kirocrew spellings STAY masked: the backend
    # never reads there, so they are pure mask, no carve (First Principles).
    legacy = [p for p in hidden if "/.kirocrew/" in p and "md-notebook" in p]
    assert legacy, "the legacy-home md-notebook mask entries disappeared"

    # And NOT re-sealed read-only: the backend WRITES vaults.json/settings.json
    # through an atomic temp+rename staged under .staging, and writes the PAT
    # file itself — a read-only seal (the policy-cache treatment) would trade
    # the EPERM on open() for an EPERM on rename().
    readonly = _launcher_list(script, "READONLY_DIRS")
    assert not [p for p in readonly if p in paths]

    # The carve-out must not weaken any sibling mask entry.
    assert [
        p for p in hidden if os.path.basename(p) == ".env"
    ], "the carve-out took the channel-credential mask down with it"


def test_the_seatbelt_profile_drops_every_live_md_notebook_rule_with_the_carveout():
    paths = sb.md_notebook_backend_state_paths()
    profile = sb._build_seatbelt_profile("standard", extra_visible_dirs=paths)
    # No read deny and no write deny on any LIVE spelling — every seatbelt rule
    # names its target path, so one containment check covers both directions.
    leaked = [p for p in paths if f'"{p}"' in profile]
    assert not leaked, (
        "the carve-out left a seatbelt rule over live md-notebook state; on "
        f"macOS the backend would still EPERM: {leaked!r}"
    )
    # The deprecated ~/.kirocrew spellings keep their rules (pure mask, no carve).
    assert (
        ".kirocrew/workspace/md-notebook" in profile
    ), "the legacy-home md-notebook seatbelt rules disappeared"
    # Sibling masks stay intact.
    assert "crew/.env" in profile, "the carve-out took the channel-credential deny down with it"


class TestAbsentStateFilesAreMaterializedSoTheMaskCanMount:
    """``mount(2)`` cannot target an absent path and the launcher's hiding loops
    guard on existence, so an absent leaf gets NO mask in the spawned namespace.
    The carve-out makes these three files creatable on a sandboxed host for the
    first time, so an agent namespace spawned before the first vault attach
    would read the PAT saved after it — unless the absent-equivalent documents
    are materialised before launch (server-side GPT review finding on #8778)."""

    @pytest.fixture()
    def crew_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".kiro" / "crew"
        home.mkdir(parents=True)
        monkeypatch.setattr(sb, "config_dir", lambda: home)
        return home

    def test_every_leaf_is_created_with_its_absent_equivalent_document(self, crew_home):
        created = sb._materialize_md_notebook_mask_targets()
        state = crew_home / "workspace" / "md-notebook"
        assert set(created) == {
            str(state / n) for n in ("pat", "vaults.json", "settings.json", ".staging")
        }
        assert (state / "vaults.json").read_bytes() == b"[]\n"
        assert (state / "settings.json").read_bytes() == b"{}\n"
        assert (state / "pat").read_bytes() == b""
        # The PAT mount target is a credential path: owner-only from birth.
        assert os.stat(state / "pat").st_mode & 0o077 == 0
        # The staging DIRECTORY gets a mount target too: the backend creates it
        # on first write, inside a namespace that may already run maskless.
        assert (state / ".staging").is_dir()
        assert os.stat(state / ".staging").st_mode & 0o077 == 0

    def test_the_documents_read_as_absent_to_the_backend(self, crew_home, monkeypatch):
        """The whole materialisation argument: an empty document must mean what
        an absent file means TO THE READER. Pin it against the backend's own
        read functions rather than restating their behavior here."""
        from kiro_crew.apps.builtins.md_notebook import server

        sb._materialize_md_notebook_mask_targets()
        monkeypatch.setattr(server, "_HOME", crew_home / "workspace" / "md-notebook")
        assert server._read_vaults_sync() == []
        assert server._read_settings_sync() == server._default_settings()
        assert server._read_pat_sync() is None

    def test_existing_state_is_left_byte_for_byte_alone(self, crew_home):
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "vaults.json").write_text('[{"id": "real"}]')
        assert sb._materialize_md_notebook_mask_targets()  # creates only the other two
        assert (state / "vaults.json").read_text() == '[{"id": "real"}]'

    def test_an_absent_data_home_is_not_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb, "config_dir", lambda: tmp_path / "never-made")
        assert sb._materialize_md_notebook_mask_targets() == []
        assert not (tmp_path / "never-made").exists()

    def test_a_failed_publish_refuses_the_spawn(self, crew_home, monkeypatch):
        """Fail-closed: launching anyway would run the agent with a mask the
        launcher silently skips — the exact hole this materialiser closes."""
        monkeypatch.setattr(sb, "_publish_empty_ceiling", lambda *a, **k: False)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_planted_intermediate_symlink_refuses_the_spawn(self, crew_home, tmp_path):
        """#4381, materialiser edition: os.makedirs follows a RESOLVING symlink
        planted at ``workspace/md-notebook`` (an agent-writable tree a spawned
        subprocess can symlink at OS level), so the materialised files would
        land at the link's target while the launcher masks the lexical path —
        a mask mounted over an attacker-chosen redirection (server-side GPT
        review, round 7). Refuse the spawn instead."""
        elsewhere = tmp_path / "elsewhere-mask"
        elsewhere.mkdir()
        (crew_home / "workspace").mkdir(parents=True)
        (crew_home / "workspace" / "md-notebook").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()
        assert list(elsewhere.iterdir()) == [], (
            "the materialiser followed the planted link and created state at "
            f"its target: {list(elsewhere.iterdir())!r}"
        )

    def test_a_resolving_leaf_symlink_refuses_the_spawn(self, crew_home, tmp_path):
        """A RESOLVING link at the leaf is refused too, not only a dangling one:
        mount(2) resolves its target, so a resolving ``pat`` link would put the
        mask on the referent while the lexical name stays an agent-replaceable
        link — swap it after launch and a later PAT write publishes to an
        unmasked name inside the live namespace (server-side GPT review,
        round 9)."""
        real = tmp_path / "somewhere-else-pat"
        real.write_bytes(b"")
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "pat").symlink_to(real)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_special_file_at_a_leaf_refuses_the_spawn(self, crew_home):
        """An EXISTING target is acceptable only as a regular file: the
        launcher's hiding loops classify with isdir/isfile, and a FIFO at
        ``pat`` matches neither — the mask is silently skipped for the whole
        sandbox (server-side GPT review, round 10)."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        os.mkfifo(state / "pat")
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_raced_special_file_refuses_the_spawn(self, crew_home, monkeypatch):
        """A LOST publish race is benign only when the winner clears the same
        regular-file bar the pre-check enforces: an agent racing mkfifo between
        validation and publish must not have its non-file accepted on bare
        existence (server-side GPT review, round 11)."""

        real_publish = sb._publish_empty_ceiling

        def _raced(target, parent, content=b"{}\n"):
            if target.endswith("pat") and not os.path.exists(target):
                os.mkfifo(target)  # the racing writer wins with a FIFO
                return False  # our publish loses (os.link EEXIST -> False)
            return real_publish(target, parent, content=content)

        monkeypatch.setattr(sb, "_publish_empty_ceiling", _raced)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_namespace_argv_materializes_before_the_launcher_runs(self, crew_home):
        """The call site: every Linux spawn gets mount targets before its child
        mounts, so no agent namespace can predate the mask."""
        sb.namespace_argv(["/bin/true"])
        state = crew_home / "workspace" / "md-notebook"
        for name in ("pat", "vaults.json", "settings.json"):
            assert (state / name).is_file(), f"{name} absent after namespace_argv"


class TestStateWritersStageInsideTheMask:
    """The three leaf masks cover exactly three names — a temp staged BESIDE the
    target holds the same bytes (PAT included) at a name no mask covers, and a
    SIGKILL between write and rename leaves it there forever (server-side GPT
    review, F1 on #8778). Every state writer must stage under the whole-directory
    ``.staging`` mask instead."""

    @pytest.fixture()
    def server(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.md_notebook import server

        monkeypatch.setattr(server, "_HOME", tmp_path / "state")
        return server

    def test_a_crashed_publish_leaves_the_temp_inside_the_mask(self, server, monkeypatch):
        def _boom(tmp, target):
            raise AssertionError("simulated crash at publish time")

        monkeypatch.setattr(server, "replace_with_retry", _boom)
        # Suppress the failure-path unlink so the orphan the crash WOULD leave
        # is observable — this models SIGKILL, which runs no cleanup at all.
        monkeypatch.setattr(server.Path, "unlink", lambda self, *a, **k: None)
        with pytest.raises(AssertionError):
            server._write_pat_sync("ghp_secret")

        state = server._HOME
        orphans_beside_target = [p for p in state.iterdir() if p.name != ".staging"]
        assert (
            not orphans_beside_target
        ), f"a PAT temp was staged beside the target, outside the mask: {orphans_beside_target!r}"
        staged = list((state / ".staging").iterdir())
        assert staged, "the temp was not staged under the masked .staging dir at all"
        assert staged[0].read_text() == "ghp_secret"
        if os.name == "posix":
            assert os.stat(staged[0]).st_mode & 0o077 == 0

    def test_each_writer_publishes_to_its_target_with_no_residue(self, server):
        server._write_pat_sync("ghp_token")
        server._write_vaults_sync([{"id": "v1"}])
        server._write_settings_sync({"autoSync": True})
        state = server._HOME
        assert (state / "pat").read_text() == "ghp_token"
        assert "v1" in (state / "vaults.json").read_text()
        assert "autoSync" in (state / "settings.json").read_text()
        assert list((state / ".staging").iterdir()) == [], "a successful write left residue"
        assert {p.name for p in state.iterdir()} == {
            "pat",
            "vaults.json",
            "settings.json",
            ".staging",
        }, "a writer left a temp beside its target"

    def test_a_planted_parent_link_refuses_the_write(self, server, tmp_path):
        """The #4381 guard, carried over from atomic_write: the old PAT write
        (atomic_write with restrict_to_owner=True) refused a secret write whose
        parent chain passes through a pre-planted link, because mkdir/mkstemp/
        rename all follow it and the token lands outside the sensitive-path
        fence. Moving the staging off atomic_write must not shed that guard
        (server-side GPT review, round 3 on #8778)."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # server fixture sets _HOME to tmp_path/"state" without creating it;
        # plant the state dir itself as a link to a foreign directory.
        server._HOME.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(OSError):
            server._write_pat_sync("ghp_secret")
        assert list(elsewhere.iterdir()) == [], (
            "the PAT write followed a pre-planted parent link and published "
            f"the token outside the fence: {list(elsewhere.iterdir())!r}"
        )

    def test_a_planted_staging_link_refuses_the_write(self, server, tmp_path):
        """Same guard, one component deeper: `.staging` itself must be a real
        directory, not a link redirecting every temp (PAT bytes included)."""
        elsewhere = tmp_path / "elsewhere-staging"
        elsewhere.mkdir()
        server._HOME.mkdir(parents=True)
        (server._HOME / ".staging").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(OSError):
            server._write_pat_sync("ghp_secret")
        assert (
            list(elsewhere.iterdir()) == []
        ), "the staged temp followed a pre-planted .staging link outside the mask"

    def test_clearing_the_pat_keeps_the_mask_mount_target(self, server):
        """Clearing must atomically empty the file, never unlink it: the inode
        is the sandbox mask's mount target, and a clear landing between the
        launcher's materialize and mount steps would leave that namespace
        maskless for a later PAT save (server-side GPT review, round 5)."""
        server._write_pat_sync("ghp_token")
        assert server._read_pat_sync() == "ghp_token"
        server._write_pat_sync("")  # what api_pat's clear branch calls
        pat_file = server._HOME / "pat"
        assert pat_file.is_file(), "the PAT clear removed the mask's mount target"
        assert pat_file.read_bytes() == b""
        assert server._read_pat_sync() is None, "empty must read as absent"
