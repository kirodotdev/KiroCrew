"""A sandboxed child cannot rename a parent of a hidden crew-home leaf.

A mask is a mount on the leaf. Renaming a parent moves the mount with it inside the
child's namespace, but on disk the leaf now sits at a name no mask covers, so the NEXT
spawn reads it there. Each test spawns a child under the real namespace sandbox and
judges by what is on disk afterwards. Seatbelt's half is a plan check: its masks are
path rules, so a renamed parent would expose the leaf in the same session.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox, sandbox_plan
from kiro_crew.apps import credential_masks
from kiro_crew.sandbox import userns_available
from kiro_crew.sandbox_leaves import _CREW_HOME_PREFIXES, MD_NOTEBOOK_APP_NAME
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_NAMESPACE = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]

# The child's side: try each rename and report what happened, inside its own namespace.
_CHILD = r"""
import errno, json, os, sys

def attempt(src, dst):
    try:
        os.rename(src, dst)
        return "renamed"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "renames": [attempt(src, src + "-moved") for src in sys.argv[2:]],
    "inside": attempt(sys.argv[1], sys.argv[1] + "-renamed"),
}
print("REPORT " + json.dumps(report), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    # The plan masks from the resolved data home, so every compared path is resolved too,
    # and ``$HOME`` is pinned so no step touches the developer's real data home.
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    monkeypatch.setattr(credential_masks, "config_dir", lambda: home)
    credential_masks.clear_cache()
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()
    credential_masks.clear_cache()


def _install(home: Path, name: str, masks: list[str]) -> Path:
    app = home / "apps" / name
    (app / "data").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "1.0.0", "credentialMasks": masks}
    (app / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app / "data"


def _run(home: Path, inside: Path, parents: list[Path], **kwargs) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    argv = [sys.executable, "-c", _CHILD, str(inside), *(str(p) for p in parents)]
    proc = subprocess.run(
        sandbox.namespace_argv(argv, "standard", **kwargs),
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    return report


class TestTheNamespaceChildCannotRenameAParent:
    pytestmark = _NAMESPACE

    def test_a_core_hidden_leafs_parents_stay_in_place(self, home):
        edits = home / "apps" / "meetings" / "data" / "edits"
        edits.mkdir(parents=True)
        (edits / "note.json").write_text("private edits", encoding="utf-8")
        sibling = home / "apps" / "meetings" / "data" / "other.txt"
        sibling.write_text("x", encoding="utf-8")
        parents = [edits.parent, edits.parent.parent, home / "apps"]

        report = _run(home, sibling, parents)

        assert report["renames"] == ["EBUSY"] * len(parents), report
        # Only the directory entries are pinned: what is inside them stays writable.
        assert report["inside"] == "renamed", report
        assert (edits / "note.json").read_text(encoding="utf-8") == "private edits"

    def test_a_declared_leafs_parents_stay_in_place(self, home):
        data = _install(home, "demo", ["secrets.json"])
        (data / "secrets.json").write_text("token", encoding="utf-8")
        scratch = data / "scratch.txt"
        scratch.write_text("x", encoding="utf-8")
        parents = [data, data.parent]

        report = _run(home, scratch, parents)

        assert report["renames"] == ["EBUSY"] * len(parents), report
        assert report["inside"] == "renamed", report
        assert (data / "secrets.json").read_text(encoding="utf-8") == "token"

    def test_the_leafs_own_backend_may_rename_its_folder(self, home):
        data = _install(home, "demo", ["tokens/oauth.json"])
        tokens = data / "tokens"
        tokens.mkdir()
        (tokens / "oauth.json").write_text("token", encoding="utf-8")
        lifted = sandbox.app_declared_mask_targets("demo", builtin=False)
        assert lifted, "the declaration lifted nothing, so this pins nothing"
        scratch = data / "scratch.txt"
        scratch.write_text("x", encoding="utf-8")

        report = _run(home, scratch, [tokens], extra_visible_dirs=lifted)

        assert report["renames"] == ["renamed"], report


class TestSeatbeltGuardsTheParentsOfACoreHiddenLeaf:
    pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")

    def test_every_parent_of_a_nested_core_leaf_is_a_literal_write_deny(self, home):
        profile = sandbox._build_seatbelt_profile("standard")
        for parent in ("apps/meetings/data", "apps/meetings", "apps/aws-control", "apps"):
            rule = f'(deny file-write* (literal "{home / parent}"))'
            assert rule in profile, f"{parent} can be renamed on Seatbelt"

    def test_the_namespace_plan_pins_the_same_parents(self, home):
        for parent in ("apps/meetings/data", "apps/meetings", "apps/aws-control"):
            (home / parent).mkdir(parents=True, exist_ok=True)
        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        pins = set(plan.runtime_ancestor_guards)
        for parent in ("apps/meetings/data", "apps/meetings", "apps/aws-control", "apps"):
            assert str(home / parent) in pins, f"an agent can rename {parent}"


class TestARetiredLeafGuardsOnlyTheFoldersThatExist:
    """Nothing writes a retired leaf, so an absent folder above it holds nothing to move.

    md-notebook's backend runs sandboxed and creates its own home, ``workspace/md-notebook``,
    on first use; a Seatbelt deny on that absent name would stop it cloning any vault.
    """

    pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")

    def test_an_absent_folder_stays_creatable_on_seatbelt(self, home):
        notebook = home / "workspace" / "md-notebook"
        profile = sandbox._build_seatbelt_profile("standard")
        assert f'(deny file-write* (literal "{notebook}"))' not in profile

    def test_the_notes_backends_own_spawn_can_create_its_home_on_seatbelt(self, home):
        """Each folder from ``workspace`` down to the clone root stays creatable while absent.

        Rendered for the backend's own spawn, the one that clones vaults there. Once a folder
        exists it is guarded like any parent, so no spawn can rename it.
        """
        lifted = sandbox.app_backend_visible_targets(MD_NOTEBOOK_APP_NAME)
        assert lifted, "the Notes backend lifted nothing, so this pins nothing"
        chain = ("workspace", f"workspace/{MD_NOTEBOOK_APP_NAME}")
        roots = (home, *(Path.home() / prefix for prefix in _CREW_HOME_PREFIXES))
        for created in ((), chain[:1], chain):
            for rel in created:
                (home / rel).mkdir(exist_ok=True)
            profile = sandbox._build_seatbelt_profile("standard", extra_visible_dirs=lifted)
            for root in roots:
                for rel in (*chain, f"{chain[-1]}/vaults"):
                    folder = root / rel
                    rule = f'(deny file-write* (literal "{folder}"))'
                    if folder.exists() and rel in chain and (root / chain[-1]).exists():
                        assert rule in profile, f"{folder} can be renamed"
                    else:
                        assert rule not in profile, f"the Notes backend cannot create {folder}"

    def test_a_present_folder_is_guarded_on_both_backends(self, home):
        notebook = home / "workspace" / "md-notebook"
        notebook.mkdir(parents=True)
        (notebook / "pat").write_text("an older build's token", encoding="utf-8")
        profile = sandbox._build_seatbelt_profile("standard")
        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        pins = set(plan.runtime_ancestor_guards)
        for folder in (notebook, notebook.parent):
            assert f'(deny file-write* (literal "{folder}"))' in profile, folder
            assert str(folder) in pins, f"an agent can rename {folder}"
