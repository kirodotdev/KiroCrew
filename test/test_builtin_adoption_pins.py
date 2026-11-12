"""The adoption pin table: validation (matrix row 21), the shipped file, no app names in core (row 20)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from builtin_adoption_helpers import NAME, REPOSITORY, install_pins, pin_row, requires_git

import kiro_crew
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps.builtin_adoption_pins import load_adoption_pins, parse_adoption_pins

_SRC = Path(kiro_crew.__file__).resolve().parent
COMMIT = "a" * 40
TREE = "b" * 64
ARCHIVE = "c" * 64


@pytest.fixture(autouse=True)
def _fresh_cache():
    load_adoption_pins.cache_clear()
    yield
    load_adoption_pins.cache_clear()


def _row(**overrides):
    return pin_row(COMMIT, TREE, ARCHIVE, **overrides)


def _parse(rows):
    return parse_adoption_pins({"schemaVersion": 1, "pins": rows})


def test_a_valid_row_loads_with_a_derived_archive_url():
    pins = _parse([_row()])
    pin = pins[NAME]
    assert pin.repository == REPOSITORY
    assert pin.archive_url == (
        f"https://codeload.github.com/FixtureOrg/kirocrew-app-fixture-former/tar.gz/{COMMIT}"
    )
    assert pin.archive_root == f"kirocrew-app-fixture-former-{COMMIT}"
    assert (pin.legacy_route, pin.legacy_icon) == (f"/{NAME}", "Box")


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": REPOSITORY + ".git"},
        {"repository": "https://user:pw@github.com/FixtureOrg/repo"},
        {"repository": "https://github.com/FixtureOrg/repo?x=1"},
        {"repository": "https://gitlab.com/FixtureOrg/repo"},
        {"commit": "A" * 40},
        {"commit": "a" * 39},
        {"treeSha256": "z" * 64},
        {"archiveSha256": "c" * 63},
        {"version": "1.0"},
        {"minAppSdkLevel": 0},
        {"minAppSdkLevel": True},
        {"name": "Not_Kebab"},
        {"name": "system"},
        {"legacyNav": {"route": "personal", "icon": "Box"}},
        {"legacyNav": {"route": "/x", "icon": "lowercase"}},
        {"legacyNav": {"route": "/x"}},
        {"archiveUrl": "https://evil.example/archive.tar.gz"},
    ],
)
def test_a_malformed_row_is_dropped(overrides):
    assert _parse([_row(**overrides)]) == {}


def test_a_dropped_row_does_not_drop_its_neighbours():
    other = _row(name="other-app")
    assert set(_parse([_row(commit="bad"), other])) == {"other-app"}


def test_a_duplicated_name_drops_every_row_with_it():
    assert _parse([_row(), _row(commit="d" * 40), _row(name="other-app")]).keys() == {"other-app"}


def test_more_rows_than_the_cap_drop_the_whole_table():
    rows = [_row(name=f"app-{i}") for i in range(pins_mod.MAX_PINS + 1)]
    assert _parse(rows) == {}
    assert len(_parse(rows[:-1])) == pins_mod.MAX_PINS


@pytest.mark.parametrize("document", [[], {"schemaVersion": 2, "pins": []}, {"schemaVersion": 1}])
def test_an_unknown_document_shape_is_an_empty_table(document):
    assert parse_adoption_pins(document) == {}


def test_a_missing_or_unparseable_file_is_an_empty_table(tmp_path, monkeypatch):
    monkeypatch.setattr(pins_mod, "_PINS_PATH", tmp_path / "absent.json")
    assert load_adoption_pins() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(pins_mod, "_PINS_PATH", bad)
    load_adoption_pins.cache_clear()
    assert load_adoption_pins() == {}


def test_the_table_is_read_once_per_process(tmp_path, monkeypatch):
    path = tmp_path / "pins.json"
    install_pins(monkeypatch, path, [_row()])
    assert pins_mod.adoption_pin_names() == {NAME}
    path.write_text(json.dumps({"schemaVersion": 1, "pins": []}), encoding="utf-8")
    assert pins_mod.adoption_pin(NAME) is not None
    load_adoption_pins.cache_clear()
    assert pins_mod.adoption_pin(NAME) is None


def test_the_harness_table_replaces_the_shipped_one(tmp_path, monkeypatch):
    table = tmp_path / "harness-pins.json"
    monkeypatch.setenv(pins_mod.HARNESS_PINS_ENV, str(table))
    assert load_adoption_pins() == {}, "an absent harness table is empty"
    table.write_text(json.dumps({"schemaVersion": 1, "pins": [_row()]}), encoding="utf-8")
    load_adoption_pins.cache_clear()
    assert set(load_adoption_pins()) == {NAME}
    assert load_adoption_pins()[NAME].harness_archive == ""


def test_only_a_harness_table_row_may_name_a_local_archive(tmp_path, monkeypatch):
    archive = str(tmp_path / "fixture.tar.gz")
    assert _parse([_row(harnessArchive=archive)]) == {}
    harness = parse_adoption_pins(
        {"schemaVersion": 1, "pins": [_row(harnessArchive=archive)]}, harness=True
    )
    assert harness[NAME].harness_archive == archive
    relative = parse_adoption_pins(
        {"schemaVersion": 1, "pins": [_row(harnessArchive="fixture.tar.gz")]}, harness=True
    )
    assert relative == {}


# --------------------------------------------------------------------------- #
# The shipped file and the no-app-names rule (row 20)
# --------------------------------------------------------------------------- #


_SHIPPED_PINS = [
    {
        "name": "personal-shopper",
        "repository": "https://github.com/KiroCrewCaptain/kirocrew-app-personal-shopper",
        "commit": "2de66b467755be0104ec6b23884815c5213db409",
        "version": "0.1.2",
        "treeSha256": "b77353e2e427c43191de6d42f45308b6b88b0a97a877395deead6e2e1c38e63b",
        "archiveSha256": "aaa144450e1cf6b4b3f882717aabfb1b7de6ce717d9de9636f5bc57a62884498",
        "minAppSdkLevel": 1,
        "legacyNav": {"route": "/personal-shopper", "icon": "ShoppingBag"},
    },
    {
        "name": "papyrus",
        "repository": "https://github.com/KiroCrewCaptain/kirocrew-app-papyrus",
        "commit": "36e0bc725796e37240fc854eaf905948ab1c4c7a",
        "version": "0.2.2",
        "treeSha256": "86a7e60af8e5b6947fe9d4cc062d35a61908feac17a0cebbbc9cea48d79917ba",
        "archiveSha256": "022184b4062cf639f0380aa339322fdd7be90a4dfc7228bb6701c5958dd533a6",
        "minAppSdkLevel": 1,
        "legacyNav": {"route": "/papyrus", "icon": "ScrollText"},
    },
    {
        "name": "file-explorer",
        "repository": "https://github.com/KiroCrewCaptain/kirocrew-app-file-explorer",
        "commit": "9ccc81e91d2b86b5d5cde214c0bfdf3a71a570aa",
        "version": "1.0.2",
        "treeSha256": "93e6e4b74fecccc7a6eb57f769b186ca866f86dee5af85b55c75a7b8b4535396",
        "archiveSha256": "f1cee8aacf8b35ca806182fb5b7d31b2824feb942d4eb397cb9b93dc4c454111",
        "minAppSdkLevel": 1,
        "legacyNav": {"route": "/file-explorer", "icon": "FolderTree"},
    },
    {
        "name": "design-critique",
        "repository": "https://github.com/KiroCrewCaptain/kirocrew-app-design-critique",
        "commit": "092451a698caad5708379bca5437c58d5cf6f1d9",
        "version": "1.0.3",
        "treeSha256": "c1314f219392fe4c3d82a3ee48da44af82a7bf6665193621a06c564b17eb0bc7",
        "archiveSha256": "a373e0137f23375fca5ad85cdbd8279b956696df9e66803c2f125220c9d20e98",
        "minAppSdkLevel": 1,
        "legacyNav": {"route": "/design-critique", "icon": "PencilRuler"},
    },
    {
        "name": "pptx-maker",
        "repository": "https://github.com/KiroCrewCaptain/kirocrew-app-pptx-maker",
        "commit": "a622dc0eff74ffe2e927871f07400e7ab21ee5da",
        "version": "0.3.3",
        "treeSha256": "bcb5c102fb50df1541d5dab1fb59d186b291479e5b90a890edf681832d0fb976",
        "archiveSha256": "c9c9525a76c7852613661f7e6016c137e0d7a548069f97abf841515ff56c4677",
        "minAppSdkLevel": 1,
        "legacyNav": {"route": "/pptx-maker", "icon": "Presentation"},
    },
]


def test_the_shipped_pin_file_validates_and_pins_exactly_the_shipped_table():
    """A pin change edits this table with the row ``adoption_pin.py compute`` printed."""
    document = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    assert document == {"schemaVersion": 1, "pins": _SHIPPED_PINS}
    assert sorted(parse_adoption_pins(document)) == sorted(row["name"] for row in _SHIPPED_PINS)


def test_every_shipped_pin_matches_its_wheel_built_in_version():
    """D12: a first-adoption pin carries the wheel built-in's version (downgrade-safe)."""
    from kiro_crew.apps.execution import _shipped_builtin_apps

    shipped = {}
    for root, name in _shipped_builtin_apps():
        shipped[name] = json.loads((root / "app.json").read_text(encoding="utf-8"))["version"]
    document = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    for pin in parse_adoption_pins(document).values():
        assert pin.name in shipped
        assert pin.version == shipped[pin.name]


def test_the_package_ships_the_pin_file():
    setup = (_SRC.parents[1] / "setup.cfg").read_text(encoding="utf-8")
    assert "apps/adoption-pins.json" in setup


_ADOPTION_MODULES = (
    "apps/builtin_adoption.py",
    "apps/builtin_adoption_apply.py",
    "apps/builtin_adoption_checks.py",
    "apps/builtin_adoption_fetch.py",
    "apps/builtin_adoption_markers.py",
    "apps/builtin_adoption_mcp.py",
    "apps/builtin_adoption_pins.py",
    "apps/builtin_adoption_state.py",
    "apps/manager_adopt.py",
    "apps/agent_config_writer.py",
)


def _builtin_identifiers() -> set[str]:
    from kiro_crew.apps.builtins import BUILTIN_NAMES
    from kiro_crew.apps.execution import _shipped_builtin_apps

    names = set(BUILTIN_NAMES)
    for root, name in _shipped_builtin_apps():
        names |= {name, root.name}
    return names


def _string_literals(source: str) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


@pytest.mark.parametrize("rel", _ADOPTION_MODULES)
def test_no_adoption_module_names_a_shipped_built_in(rel):
    builtins = _builtin_identifiers()
    assert builtins, "the census found no shipped built-in"
    literals = _string_literals((_SRC / rel).read_text(encoding="utf-8"))
    assert sorted(literals & builtins) == []


def test_the_literal_scan_finds_a_planted_name():
    some = sorted(_builtin_identifiers())[0]
    assert some in _string_literals(f"X = {some!r}\n")


# --------------------------------------------------------------------------- #
# scripts/adoption_pin.py: the release captain's tool measures what STAGE measures
# --------------------------------------------------------------------------- #


def _pin_tool(monkeypatch):
    """A private copy of the script; its ``sys.path`` insert stays inside the test."""
    import importlib.util
    import sys

    path = _SRC.parents[1] / "scripts" / "adoption_pin.py"
    spec = importlib.util.spec_from_file_location("adoption_pin_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "adoption_pin_tool", module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("repository", "commit"),
    [
        ("--upload-pack=touch /tmp/x", COMMIT),
        ("https://example.com/owner/repo", COMMIT),
        (REPOSITORY + ".git", COMMIT),
        (REPOSITORY, "main"),
    ],
)
def test_the_pin_tool_refuses_a_bad_target_before_any_git_call(monkeypatch, repository, commit):
    tool = _pin_tool(monkeypatch)
    cloned = []
    monkeypatch.setattr(tool, "_clone", lambda *args: cloned.append(args))
    with pytest.raises(SystemExit):
        tool._compute(repository, commit)
    assert cloned == []


def test_the_pin_tool_leaves_the_probe_to_the_temporary_directory(monkeypatch):
    """A bare rmtree of a git checkout fails on Windows' read-only object files."""
    tool = _pin_tool(monkeypatch)
    seen = []

    def fake_clone(_repository, _commit, dest):
        dest.mkdir()
        (dest / "app.json").write_text(
            json.dumps({"name": NAME, "version": "1.2.3"}), encoding="utf-8"
        )

    def fake_measure(pin, work):
        seen.append((work / "probe").is_dir())
        return {"name": pin.name}

    monkeypatch.setattr(tool, "_clone", fake_clone)
    monkeypatch.setattr(tool, "_measure", fake_measure)
    assert tool._compute(REPOSITORY, COMMIT) == 0
    assert seen == [True]


@requires_git
def test_the_pin_tool_checks_out_without_the_operators_git_config(tmp_path, monkeypatch):
    """Git for Windows' installer sets ``core.autocrlf=true``; STAGE never reads it."""
    import subprocess

    from builtin_adoption_helpers import make_bare_repo, write_tree

    tree = write_tree(tmp_path / "tree", {"name": NAME})
    bare, commit = make_bare_repo(tmp_path / "git", tree)
    operator_config = tmp_path / "operator.gitconfig"
    operator_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(operator_config))
    probe = subprocess.run(
        ["git", "config", "--get", "core.autocrlf"],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert probe.stdout.strip() == "true", "the operator config is in effect for plain git"
    checkout = tmp_path / "checkout"
    _pin_tool(monkeypatch)._clone(bare.as_uri(), commit, checkout)
    assert b"\r\n" not in (checkout / "app.json").read_bytes()
    from kiro_crew.apps.builtin_adoption_fetch import adoption_tree_digest

    assert adoption_tree_digest(checkout) == adoption_tree_digest(tree)


# --------------------------------------------------------------------------- #
# scripts/adoption_pin.py: minAppSdkLevel comes from the seams the tree uses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("manifest", "files", "uses"),
    [
        ({}, {}, {}),
        ({"agentTools": {}}, {}, {"app.json agentTools": 2}),
        (
            {"permissions": {"agentTurns": True, "workerAutoApprove": True}},
            {},
            {"app.json permissions.agentTurns": 2, "app.json permissions.workerAutoApprove": 2},
        ),
        ({"permissions": {"agentTurns": "true"}}, {}, {}),
        ({"credentialMasks": ["data/token.json"]}, {}, {"app.json credentialMasks": 3}),
        ({}, {"backend/routes.py": "def f(ctx):\n    return ctx.turns.run\n"}, {"ctx.turns": 2}),
        ({}, {"backend/_host.py": "x = _bound_ctx().lifecycle\n"}, {"ctx.lifecycle": 2}),
        (
            {},
            {"backend/hooks.py": "async def f(ctx):\n    return await ctx.app_token()\n"},
            {"ctx.app_token": 5},
        ),
        (
            {},
            {"backend/hooks.py": "def f(ctx):\n    return ctx.gateway_origin()\n"},
            {"ctx.gateway_origin": 5},
        ),
        (
            {},
            {"backend/_host.py": "def f(self):\n    return self._ctx.untrusted\n"},
            {"ctx.untrusted": 2},
        ),
        (
            {},
            {
                "backend/_host.py": "def f(bound):\n    return getattr(bound(), 'auto_approve', None)\n"
            },
            {"ctx.auto_approve": 2},
        ),
        (
            {},
            {
                "backend/_host.py": "def f(app_context):\n    return hasattr(app_context, 'lifecycle')\n"
            },
            {"ctx.lifecycle": 2},
        ),
        ({}, {"backend/_host.py": "def f(ctx:\n    ctx.lifecycle\n"}, {"ctx.lifecycle": 2}),
        ({}, {"backend/manifest.py": "def f(manifest):\n    return manifest.lifecycle\n"}, {}),
        ({}, {"tests/fakehost.py": "ctx.turns = None\n"}, {}),
        ({}, {"backend/test_seams.py": "ctx.turns = None\n"}, {}),
        ({}, {"conftest.py": "ctx.untrusted = None\n"}, {}),
    ],
)
def test_the_pin_tool_finds_every_seam_above_level_one(
    monkeypatch, tmp_path, manifest, files, uses
):
    from builtin_adoption_helpers import repository_manifest, write_tree

    tree = write_tree(tmp_path / "tree", repository_manifest(**manifest), files)
    assert _pin_tool(monkeypatch)._seam_level_uses(tree) == uses


def test_the_pin_tool_never_lets_a_level_fall_below_what_the_tree_uses(monkeypatch):
    tool = _pin_tool(monkeypatch)
    uses = {"ctx.turns": 2, "app.json credentialMasks": 3}
    assert tool._sdk_level_refusal(NAME, 1, {}) is None
    assert tool._sdk_level_refusal(NAME, 3, uses) is None
    for level in (1, 2):
        refusal = tool._sdk_level_refusal(NAME, level, uses)
        assert refusal is not None and "--min-sdk-level 3" in refusal
        assert "app.json credentialMasks" in refusal
    assert "ctx.turns" in tool._sdk_level_refusal(NAME, 1, uses)
    assert "ctx.turns" not in tool._sdk_level_refusal(NAME, 2, uses)


def _measure_tree(monkeypatch, tmp_path, tool, files, level):
    """``_measure`` end to end on a local tree, with the network and the E-checks stubbed."""
    import hashlib
    import shutil

    from builtin_adoption_helpers import make_archive, repository_manifest, write_tree

    tree = write_tree(tmp_path / "tree", repository_manifest(), files)
    archive = make_archive(tree, COMMIT)

    def fake_download(_url, dest):
        dest.write_bytes(archive)
        return hashlib.sha256(archive).hexdigest()

    monkeypatch.setattr(tool, "_clone", lambda _r, _c, dest: shutil.copytree(tree, dest))
    monkeypatch.setattr(tool, "_download", fake_download)
    monkeypatch.setattr(tool, "_shipped", lambda _name: (tree, repository_manifest()))
    monkeypatch.setattr(tool, "pinned_tree_refusal", lambda *_args, **_kwargs: None)
    work = tmp_path / "work"
    work.mkdir()
    pin = _parse([_row(minAppSdkLevel=level)])[NAME]
    return tool._measure(pin, work)


def test_the_pin_tool_refuses_level_one_for_a_tree_that_uses_level_two(monkeypatch, tmp_path):
    tool = _pin_tool(monkeypatch)
    files = {"backend/_host.py": "def f(ctx):\n    return getattr(ctx, 'lifecycle', None)\n"}
    with pytest.raises(SystemExit, match="--min-sdk-level 2"):
        _measure_tree(monkeypatch, tmp_path / "a", tool, files, 1)
    row = _measure_tree(monkeypatch, tmp_path / "b", tool, files, 2)
    assert row["minAppSdkLevel"] == 2


def test_the_pin_tool_keeps_level_one_for_a_tree_without_newer_seams(monkeypatch, tmp_path):
    tool = _pin_tool(monkeypatch)
    assert _measure_tree(monkeypatch, tmp_path, tool, {}, 1)["minAppSdkLevel"] == 1


@pytest.mark.parametrize("level", [0, 99])
def test_the_pin_tool_refuses_a_level_this_build_cannot_run_before_any_git_call(monkeypatch, level):
    tool = _pin_tool(monkeypatch)
    cloned = []
    monkeypatch.setattr(tool, "_clone", lambda *args: cloned.append(args))
    with pytest.raises(SystemExit, match="--min-sdk-level"):
        tool._compute(REPOSITORY, COMMIT, level)
    assert cloned == []


def test_the_pin_tool_passes_the_flag_through_to_the_row(monkeypatch):
    tool = _pin_tool(monkeypatch)
    levels = []

    def fake_clone(_repository, _commit, dest):
        dest.mkdir()
        (dest / "app.json").write_text(
            json.dumps({"name": NAME, "version": "1.2.3"}), encoding="utf-8"
        )

    monkeypatch.setattr(tool, "_clone", fake_clone)
    monkeypatch.setattr(tool, "_measure", lambda pin, _work: levels.append(pin.min_app_sdk_level))
    assert tool.main(["compute", REPOSITORY, COMMIT]) == 0
    assert tool.main(["compute", REPOSITORY, COMMIT, "--min-sdk-level", "2"]) == 0
    assert levels == [1, 2]
