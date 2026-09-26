"""The out-of-process provisioning predicates and the sites pinned to them.

``manifest.py`` owns the answer to "does the runtime install this app's root
``requirements.txt`` out of process?": the module-style entry-point test, the
stdio-server test, and their union. Two provisioners (``backend.py`` at spawn,
``bridges.py`` at registration) and the install-time desktop gate in
``registry.py`` all decide from those same names. The cross-pin tests below fail
the moment any of the three re-spells a condition inline, because a copy that
drifts is exactly how a gate ends up waiving what the runtime does not provision.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from conftest import requires_symlinks
from kiro_crew.apps import backend, bridges
from kiro_crew.apps import manifest as manifest_mod
from kiro_crew.apps import registry
from kiro_crew.apps.manifest import (
    AppManifest,
    file_entry_point_refusal,
    has_stdio_mcp_server,
    is_module_style_entry_point,
    requirements_in_tree,
    runtime_provisions_requirements,
)


def _manifest(**fields) -> AppManifest:
    return AppManifest.from_dict({"name": "demo", **fields})


class TestIsModuleStyleEntryPoint:
    def test_a_dotted_extensionless_name_with_no_file_is_a_module(self, tmp_path):
        assert is_module_style_entry_point("kiro_crew.apps.builtins.demo.server", tmp_path)
        assert is_module_style_entry_point("my_pkg.server", tmp_path)

    @pytest.mark.parametrize(
        "entry",
        ["server.py", "backend/app.py", "dist/main.js", "run.sh", "srv.mjs", "a.cjs", "x.ts"],
    )
    def test_a_script_suffix_or_a_path_separator_is_a_file(self, tmp_path, entry):
        assert not is_module_style_entry_point(entry, tmp_path)

    def test_a_file_with_the_literal_dotted_name_is_a_file(self, tmp_path):
        (tmp_path / "server.main").write_text("", encoding="utf-8")
        assert not is_module_style_entry_point("server.main", tmp_path)

    def test_an_undotted_or_empty_name_is_not_a_module(self, tmp_path):
        assert not is_module_style_entry_point("server", tmp_path)
        assert not is_module_style_entry_point("", tmp_path)


class TestHasStdioMcpServer:
    def test_an_entry_without_url_is_stdio(self):
        assert has_stdio_mcp_server(
            _manifest(mcpServers={"tool": {"command": "python3", "args": ["srv.py"]}})
        )

    def test_a_url_entry_is_remote_and_not_stdio(self):
        assert not has_stdio_mcp_server(
            _manifest(mcpServers={"remote": {"url": "http://127.0.0.1:9100/mcp"}})
        )

    def test_one_stdio_entry_among_url_entries_counts(self):
        assert has_stdio_mcp_server(
            _manifest(
                mcpServers={
                    "remote": {"url": "http://127.0.0.1:9100/mcp"},
                    "local": {"command": "node", "args": ["srv.js"]},
                }
            )
        )

    def test_no_servers_is_not_stdio(self):
        assert not has_stdio_mcp_server(_manifest())


class TestRuntimeProvisionsRequirements:
    """The union of the two provisioners' conditions, case by case."""

    def test_a_file_style_entry_point_is_provisioned_at_spawn(self, tmp_path):
        (tmp_path / "server.py").write_text("", encoding="utf-8")
        assert runtime_provisions_requirements(
            _manifest(backend={"entryPoint": "server.py", "type": "asgi"}), tmp_path
        )

    def test_a_declared_file_the_spawn_would_refuse_is_not_provisioned_at_spawn(self, tmp_path):
        """``_start_app_backend_body`` returns before ``provision_app_deps`` when
        the entry is not a regular file inside the root, so the backend
        provisioner never runs for it -- only a stdio server, which bridges.py
        provisions for at registration without any such requirement, can still
        make the union true."""
        stdio = {"tool": {"command": "python3", "args": ["srv.py"]}}
        missing = _manifest(backend={"entryPoint": "server.py", "type": "asgi"})
        assert not runtime_provisions_requirements(missing, tmp_path)
        assert runtime_provisions_requirements(
            _manifest(backend={"entryPoint": "server.py"}, mcpServers=stdio), tmp_path
        )

    def test_a_module_style_entry_point_is_never_provisioned(self, tmp_path):
        stdio = {"tool": {"command": "python3", "args": ["srv.py"]}}
        assert not runtime_provisions_requirements(
            _manifest(backend={"entryPoint": "my_pkg.server", "type": "asgi"}), tmp_path
        )
        # Not even when a stdio server is declared beside it: bridges.py returns
        # before provisioning on the same module-style test.
        assert not runtime_provisions_requirements(
            _manifest(backend={"entryPoint": "my_pkg.server"}, mcpServers=stdio), tmp_path
        )

    def test_without_an_entry_point_a_stdio_server_is_provisioned_at_registration(self, tmp_path):
        assert runtime_provisions_requirements(
            _manifest(mcpServers={"tool": {"command": "python3", "args": ["srv.py"]}}), tmp_path
        )

    def test_nothing_out_of_process_means_nothing_provisions(self, tmp_path):
        assert not runtime_provisions_requirements(_manifest(), tmp_path)
        assert not runtime_provisions_requirements(
            _manifest(mcpServers={"remote": {"url": "http://127.0.0.1:9100/mcp"}}), tmp_path
        )

    def test_hooks_do_not_enter_the_predicate(self, tmp_path):
        """Hooks are the install gate's own exclusion, layered on top: the
        runtime provisions a file-style entry point's requirements whether or
        not the manifest also declares a hook."""
        (tmp_path / "server.py").write_text("", encoding="utf-8")
        assert runtime_provisions_requirements(
            _manifest(
                backend={
                    "entryPoint": "server.py",
                    "type": "asgi",
                    "hooks": {"on_startup": "backend.hooks:start"},
                }
            ),
            tmp_path,
        )


class TestTheThreeSitesSharePredicates:
    """Cross-pin: each site imports the predicate and spells no copy of it.

    A source-level pin, because a behavioral test at one site cannot see a
    second site quietly growing its own inline variant.
    """

    INLINE_SHAPE_MARKERS = (
        '.endswith((".py"',  # the module-style suffix tuple re-spelled inline
        '"." in entry_point',  # the module-style dot test re-spelled inline
        'cfg.get("url")',  # the stdio-server test re-spelled inline
    )

    @pytest.mark.parametrize(
        "site, required_names",
        [
            (backend._start_app_backend_body, ("is_module_style_entry_point(",)),
            (
                bridges._maybe_provision_backendless_deps,
                ("is_module_style_entry_point(", "has_stdio_mcp_server("),
            ),
            (registry._requirements_owned_by_the_runtime, ("runtime_provisions_requirements(",)),
        ],
        ids=["backend spawn", "bridges registration", "registry desktop gate"],
    )
    def test_the_site_calls_the_shared_predicate_and_spells_no_copy(self, site, required_names):
        source = inspect.getsource(site)
        for name in required_names:
            assert name in source, f"{site.__qualname__} no longer calls {name}"
        for marker in self.INLINE_SHAPE_MARKERS:
            assert marker not in source, f"{site.__qualname__} re-spells the predicate: {marker}"

    def test_the_names_the_sites_import_are_manifest_py_s_objects(self):
        assert backend.is_module_style_entry_point is manifest_mod.is_module_style_entry_point
        assert bridges.is_module_style_entry_point is manifest_mod.is_module_style_entry_point
        assert bridges.has_stdio_mcp_server is manifest_mod.has_stdio_mcp_server
        assert (
            registry.runtime_provisions_requirements is manifest_mod.runtime_provisions_requirements
        )
        assert backend.requirements_in_tree is manifest_mod.requirements_in_tree
        assert registry.requirements_in_tree is manifest_mod.requirements_in_tree
        assert backend.file_entry_point_refusal is manifest_mod.file_entry_point_refusal

    def test_the_spawn_precondition_is_spelled_once(self):
        """The spawn's file-entry precondition (a regular file whose resolution
        stays inside the app root) is ``file_entry_point_refusal``'s alone: the
        spawn body calls it for its refusal reason, and the union calls it to know
        whether the backend provisioner will run at all. An inline re-spelling at
        the spawn would be a condition the gate cannot see drift."""
        source = inspect.getsource(backend._start_app_backend_body)
        assert "file_entry_point_refusal(" in source
        for marker in ("entry.is_file()", "entry.resolve().is_relative_to"):
            assert marker not in source, f"the spawn body re-spells its precondition: {marker}"

    INLINE_CONTAINMENT_MARKERS = (
        ".resolve(strict=True)",  # the strict-resolve pair re-spelled inline
        "is_relative_to(root_resolved)",  # the containment test re-spelled inline
        "in open_target.parents",  # its older spelling
    )

    @pytest.mark.parametrize(
        "site",
        [
            backend._provision_app_deps_locked,
            backend._deps_tree_stamp_current,
            registry._desktop_build_refusal,
        ],
        ids=["backend provisioning read", "backend activation gate", "registry desktop gate"],
    )
    def test_the_requirements_file_rule_is_spelled_once(self, site):
        """The file-acceptance rule (strict resolution to a regular file inside
        the strictly-resolved app root) is ``requirements_in_tree``'s alone: the
        two backend readers use it as their fast refusal ahead of the pinned
        open, and the gate predicts from it. A site that re-spells the pair would
        be a copy the gate cannot see drift."""
        source = inspect.getsource(site)
        assert "requirements_in_tree(" in source, f"{site.__qualname__} no longer calls the rule"
        for marker in self.INLINE_CONTAINMENT_MARKERS:
            assert marker not in source, f"{site.__qualname__} re-spells the rule: {marker}"

    def test_bridges_provisions_exactly_where_the_predicate_says_it_does(
        self, tmp_path, monkeypatch
    ):
        """Behavioral half of the pin for the registration provisioner: with a
        requirements.txt present and the app not a shipped builtin, it provisions
        for a stdio-server app whose entry point is absent or file-style and never
        for a module-style one -- the same three answers the predicate gives."""
        (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
        monkeypatch.setattr(bridges, "app_dir", lambda name: tmp_path)
        monkeypatch.setattr(bridges, "shipped_builtin_app_root", lambda name: None)
        calls: list[tuple[str, object]] = []
        monkeypatch.setattr(
            backend, "provision_app_deps", lambda name, root: calls.append((name, root)) or ""
        )
        stdio = {"srv": {"command": "python3", "args": ["s.py"]}}
        cases = [
            ("", True),
            ("server.py", True),
            ("my_pkg.server", False),
        ]
        for entry, expected in cases:
            calls.clear()
            manifest = SimpleNamespace(mcpServers=stdio, backend=SimpleNamespace(entryPoint=entry))
            bridges._maybe_provision_backendless_deps("app", manifest)
            assert (calls == [("app", tmp_path)]) is expected, entry
            typed = _manifest(backend={"entryPoint": entry}, mcpServers=stdio)
            assert runtime_provisions_requirements(typed, tmp_path) is expected, entry


class TestRequirementsInTree:
    """The provisioner's acceptance rule for the file, case by case."""

    def test_a_regular_file_resolves_to_itself_inside_the_root(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("fastapi\n", encoding="utf-8")
        resolved = requirements_in_tree(tmp_path, req)
        assert resolved is not None
        root_resolved, target = resolved
        assert root_resolved == tmp_path.resolve()
        assert target == req.resolve()

    @requires_symlinks
    def test_an_in_tree_link_resolves_to_its_target(self, tmp_path):
        (tmp_path / "requirements").mkdir()
        prod = tmp_path / "requirements" / "prod.txt"
        prod.write_text("fastapi\n", encoding="utf-8")
        req = tmp_path / "requirements.txt"
        req.symlink_to(prod)
        resolved = requirements_in_tree(tmp_path, req)
        assert resolved is not None
        assert resolved[1] == prod.resolve()

    @requires_symlinks
    def test_a_link_escaping_the_root_is_none(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        root = tmp_path / "app"
        root.mkdir()
        req = root / "requirements.txt"
        req.symlink_to(outside / "requirements.txt")
        assert requirements_in_tree(root, req) is None

    @requires_symlinks
    def test_a_dangling_link_is_none(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.symlink_to(tmp_path / "gone.txt")
        assert requirements_in_tree(tmp_path, req) is None

    def test_a_directory_and_an_absent_entry_are_none(self, tmp_path):
        (tmp_path / "requirements.txt").mkdir()
        assert requirements_in_tree(tmp_path, tmp_path / "requirements.txt") is None
        assert requirements_in_tree(tmp_path, tmp_path / "missing.txt") is None

    def test_a_file_over_the_provisioners_read_cap_is_none(self, tmp_path):
        """The provisioner's bounded read refuses a requirements.txt larger than
        its cap, so the rule refuses it too -- and the cap is one value, imported
        by the provisioner from here."""
        req = tmp_path / "requirements.txt"
        req.write_bytes(b"#" + b"x" * manifest_mod.REQUIREMENTS_TXT_MAX_BYTES)
        assert requirements_in_tree(tmp_path, req) is None
        req.write_bytes(b"x" * manifest_mod.REQUIREMENTS_TXT_MAX_BYTES)
        assert requirements_in_tree(tmp_path, req) is not None
        assert backend._DEPS_REQ_MAX_BYTES is manifest_mod.REQUIREMENTS_TXT_MAX_BYTES

    @requires_symlinks
    def test_a_symlink_loop_is_none_not_an_exception(self, tmp_path):
        """``Path.resolve`` raises RuntimeError on a loop before Python 3.13 (an
        ELOOP OSError from then on); a self-referential ``requirements.txt`` must
        answer "not a file the runtime reads", never escape as a 500 from the
        install route -- and the spawn precondition, which also resolves, must
        answer its refusal reason the same way."""
        req = tmp_path / "requirements.txt"
        req.symlink_to("requirements.txt")
        assert requirements_in_tree(tmp_path, req) is None
        (tmp_path / "server.py").symlink_to("server.py")
        # is_file() on a loop is False (ELOOP is ignored), so the spawn's reason
        # is "not found"; what matters is that no exception escapes.
        assert file_entry_point_refusal("server.py", tmp_path) == "not found"


class TestFileEntryPointRefusal:
    """The spawn's precondition for a file-style entry, case by case."""

    def test_a_regular_file_inside_the_root_spawns(self, tmp_path):
        (tmp_path / "server.py").write_text("", encoding="utf-8")
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "app.py").write_text("", encoding="utf-8")
        assert file_entry_point_refusal("server.py", tmp_path) == ""
        assert file_entry_point_refusal("backend/app.py", tmp_path) == ""

    def test_a_missing_file_is_not_found(self, tmp_path):
        assert file_entry_point_refusal("server.py", tmp_path) == "not found"
        (tmp_path / "server.py").mkdir()
        assert file_entry_point_refusal("server.py", tmp_path) == "not found"

    @requires_symlinks
    def test_a_link_leaving_the_root_escapes(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "server.py").write_text("", encoding="utf-8")
        root = tmp_path / "app"
        root.mkdir()
        (root / "server.py").symlink_to(outside / "server.py")
        assert file_entry_point_refusal("server.py", root) == "escapes app root"
