"""A ``config.json`` saved with a UTF-8 byte-order mark is still the user's config.

Editors on Windows (Notepad, PowerShell ``Out-File``, VS Code "UTF-8 with BOM")
write ``EF BB BF`` ahead of the JSON, and ``json.loads`` refuses a leading
U+FEFF. Before this, both config readers treated such a file as unparseable: the
loader marked it degraded and ran the gateway on DEFAULTS (every onboarding flag
false, so first run reopened on every load), and every locked read-modify-write
failed closed with ``ConfigReadError``, so no setting could be saved either.

The contract pinned here: the BOM is accepted on read, the real values load,
writes come back as plain UTF-8 without it, and nothing the user set is lost on
the way. A file that is actually malformed still fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_local_path,
    config_path,
    read_config_for_update,
    update_config_locked,
)

_BOM = b"\xef\xbb\xbf"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert config_path().parent == tmp_path
    return tmp_path


def _user_document() -> dict:
    # A key the dataclass does not model is the one a whole-document rewrite
    # from defaults would drop, so it is the sharpest probe for "nothing lost".
    return {
        "dashboard": {"import_onboarded": True, "onboarded": True, "theme_mode": "light"},
        "x_user_note": "kept by hand",
    }


def _write_with_bom(path: Path, document: dict) -> None:
    path.write_bytes(_BOM + json.dumps(document, indent=2).encode("utf-8"))


def test_load_reads_the_real_values_of_a_bom_saved_config(home: Path) -> None:
    _write_with_bom(config_path(), _user_document())

    cfg = KiroCrewConfig.load()

    assert cfg.dashboard.import_onboarded is True
    assert cfg.dashboard.onboarded is True
    assert cfg.dashboard.theme_mode == "light"
    assert not cfg.degraded_sections


def test_locked_update_keeps_every_user_key_and_drops_the_bom(home: Path) -> None:
    _write_with_bom(config_path(), _user_document())

    def _mutate(doc: dict) -> dict:
        doc["dashboard"]["import_onboarded"] = False
        return doc

    update_config_locked(mutate=_mutate)

    raw = config_path().read_bytes()
    assert not raw.startswith(_BOM)
    written = json.loads(raw.decode("utf-8"))
    assert written["dashboard"]["import_onboarded"] is False
    assert written["dashboard"]["onboarded"] is True
    assert written["dashboard"]["theme_mode"] == "light"
    assert written["x_user_note"] == "kept by hand"


def test_read_for_update_accepts_the_bom(home: Path) -> None:
    _write_with_bom(config_path(), _user_document())

    assert read_config_for_update() == _user_document()


def test_a_bom_saved_overlay_still_overlays(home: Path) -> None:
    config_path().write_text(json.dumps(_user_document()), encoding="utf-8")
    _write_with_bom(config_local_path(), {"dashboard": {"theme_mode": "dark"}})

    cfg = KiroCrewConfig.load()

    assert cfg.dashboard.theme_mode == "dark"
    assert cfg.dashboard.import_onboarded is True


def test_a_malformed_config_still_fails_closed(home: Path) -> None:
    # The BOM is the only thing tolerated: a truncated document is still
    # unreadable, and the write must refuse rather than replace it.
    original = _BOM + b'{"dashboard": {"import_onboarded": tr'
    config_path().write_bytes(original)

    with pytest.raises(ConfigReadError):
        update_config_locked(mutate=lambda doc: doc)

    assert config_path().read_bytes() == original


def test_hot_reload_does_not_call_a_bom_saved_config_torn(home: Path) -> None:
    # The live watcher keeps the previous snapshot while a file is "torn", so a
    # BOM read as torn would silently discard every hot edit.
    from kiro_crew.config.live import ConfigWatch

    _write_with_bom(config_path(), _user_document())
    _write_with_bom(config_local_path(), {"dashboard": {"theme_mode": "dark"}})

    assert ConfigWatch._document_is_torn() is False


def test_a_bom_saved_overlay_still_owns_its_trust_grant(home: Path) -> None:
    # The loader applies a BOM'd overlay, so the overlay-ownership gates must see
    # it too: otherwise a revoke edits only config.json, answers 200, and the
    # overlay re-grants the app on the next load.
    from kiro_crew.apps.manager import trust_grant_removal_blocked
    from kiro_crew.dashboard.handlers.security import _overlay_owned_trust_settings

    config_path().write_text(json.dumps({"agent": {"apps_trusted": ["demo"]}}), encoding="utf-8")
    _write_with_bom(config_local_path(), {"agent": {"apps_trusted": ["demo"]}})

    assert "demo" in KiroCrewConfig.load().agent.apps_trusted
    assert _overlay_owned_trust_settings() == ["apps_trusted"]
    assert trust_grant_removal_blocked("demo") is not None


def test_raw_config_reads_a_bom_saved_non_object_as_empty(home: Path) -> None:
    config_path().write_bytes(_BOM + b"[]")

    assert loader._raw_config() == {}


def test_a_bom_saved_non_object_knowledge_section_does_not_crash_the_embedder_setup(
    home: Path,
) -> None:
    from kiro_crew.dashboard.handlers import knowledge

    config_path().write_bytes(_BOM + b'{"knowledge": ["x"]}')

    knowledge._create_embedder(None)


def test_a_bom_saved_non_object_config_does_not_crash_the_embedder_setup(home: Path) -> None:
    from kiro_crew.dashboard.handlers import knowledge

    config_path().write_bytes(_BOM + b"[]")

    knowledge._create_embedder(None)


def test_the_agent_spec_builder_reads_a_bom_saved_config(home: Path) -> None:
    # build_agent_config reads the crew config through agent._load_json, so a
    # BOM there must not read as an absent file (hooks, aliases, OAuth client).
    from kiro_crew import agent

    _write_with_bom(config_path(), _user_document())

    assert agent._load_json(config_path())["x_user_note"] == "kept by hand"


def _config_reads_outside_the_helper(src: Path) -> tuple[list[str], int]:
    """Every direct read of the crew config files, and the helper's call count.

    A read is a ``.read_text``/``.read_bytes`` on, or a read-mode ``open`` of,
    ``config_path()`` / ``config_local_path()`` (or a local name bound to one, or
    ``config_dir()/data_home() / "config.json"``). The only one allowed is inside
    ``read_config_text`` itself.
    """
    import ast

    sources: set[str] = set()

    def _callee(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                return f.id
            if isinstance(f, ast.Attribute):
                return f.attr
        return None

    def _is_config(node: ast.AST, names: set[str]) -> bool:
        # See through a wrapper that keeps the path: Path(config_path()),
        # os.fspath(config_path()), str(config_path()).
        if _callee(node) in ("Path", "fspath", "str") and getattr(node, "args", None):
            return _is_config(node.args[0], names)
        if _callee(node) in sources:
            return True
        if isinstance(node, ast.Name) and node.id in names:
            return True
        return (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and node.right.value in ("config.json", "config.local.json")
            and _callee(node.left) in ("config_dir", "data_home")
        )

    offenders: list[str] = []
    helper_calls = 0
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        # App packages own their own data-dir config.json, not the crew one.
        if rel.startswith("apps/builtins/"):
            continue
        text = path.read_text(encoding="utf-8")
        # Only a module that names a config source can read one; parsing the
        # rest is what made this scan slow. read_config_text is the count.
        if not any(
            needle in text
            for needle in ("config_path", "config_local_path", "config.json", "read_config_text")
        ):
            continue
        tree = ast.parse(text)
        # An aliased import (``from kiro_crew.config import config_path as
        # _mc_config_path``) is the same source under another name.
        sources = {"config_path", "config_local_path"} | {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.asname and alias.name in ("config_path", "config_local_path")
        }
        every_node = list(ast.walk(tree))
        helper_calls += sum(
            isinstance(node, ast.Call) and _callee(node) == "read_config_text"
            for node in every_node
        )
        for scope in every_node:
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if scope.name == "read_config_text":
                continue
            names: set[str] = set()
            scope_nodes = list(ast.walk(scope))
            for node in scope_nodes:
                value = getattr(node, "value", None)
                if isinstance(node, ast.Assign) and value is not None and _is_config(value, names):
                    names.update(t.id for t in node.targets if isinstance(t, ast.Name))
                if (
                    isinstance(node, ast.AnnAssign)
                    and value is not None
                    and isinstance(node.target, ast.Name)
                    and _is_config(value, names)
                ):
                    names.add(node.target.id)
                if (
                    isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name)
                    and isinstance(node.iter, (ast.Tuple, ast.List))
                    and node.iter.elts
                    and all(_is_config(e, names) for e in node.iter.elts)
                ):
                    names.add(node.target.id)
            for node in scope_nodes:
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("read_text", "read_bytes")
                    and _is_config(func.value, names)
                ):
                    offenders.append(f"{rel}:{node.lineno}")
                if isinstance(func, ast.Name) and func.id == "open" and node.args:
                    mode = node.args[1] if len(node.args) > 1 else None
                    for kw in node.keywords:
                        if kw.arg == "mode":
                            mode = kw.value
                    reading = mode is None or (
                        isinstance(mode, ast.Constant) and "r" in str(mode.value)
                    )
                    if reading and _is_config(node.args[0], names):
                        offenders.append(f"{rel}:{node.lineno}")
    return sorted(set(offenders)), helper_calls


def test_every_config_reader_tolerates_a_bom() -> None:
    src = Path(loader.__file__).resolve().parents[1]
    offenders, helper_calls = _config_reads_outside_the_helper(src)

    assert offenders == [], (
        "read config.json/config.local.json through read_config_text, not a direct "
        f"read: {offenders}"
    )
    # A scan that found nothing would pass the assertion above vacuously.
    assert helper_calls >= 30, helper_calls
