"""An approved launch that pins the gateway's own interpreter survives an upgrade.

``apps/bridges.py`` rewrites a manifest's bare ``python3`` (and the ``kirocrew``
host CLI) to ``sys.executable``, inserts the ``deps_boot`` shim by absolute
path, and writes the package's ``site-packages`` into the declared
``PYTHONPATH``. All three live inside the install, which the desktop and CLI
installers lay out under a VERSIONED directory -- so before this rule the
approval fingerprint recorded ``.../0.8.0.2/payload/...`` paths and every
upgrade produced a ``changed_needs_reapproval`` refusal for a program that had
not changed.

``hash_command`` / ``hash_declared_env`` now encode exactly those three tokens
by role (``hashing.install_aliases``), matched by raw-string equality. These
tests pin: the same digest across two releases, a real change still changing
it, every other token byte-for-byte the legacy digest, no literal argv colliding
with the encoding, the two end-to-end approval paths, and the invariant that
the spellings ``bridges.py`` actually emits ARE the alias keys.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import hashing, launch_approval
from kiro_crew.mcp_gateway.hashing import hash_command, launch_token_bytes


def _pin_release(monkeypatch: pytest.MonkeyPatch, prefix: Path) -> dict[str, str]:
    """Point the alias inputs at a throwaway release tree; return its spellings."""
    site = prefix / "lib" / "python3.12" / "site-packages"
    pkg = site / "kiro_crew"
    interpreter = prefix / "bin" / "python3.12"
    monkeypatch.setattr(hashing.sys, "executable", str(interpreter))
    monkeypatch.setattr(hashing, "_package_dir", lambda: str(pkg))
    return {
        "interpreter": str(interpreter),
        "deps_boot": str(pkg / "apps" / "deps_boot.py"),
        "site": str(site),
        "prefix": str(prefix),
    }


def _legacy_hash(command: str, args: list[str]) -> str:
    """The pre-rule digest: UTF-8 tokens joined by ``\\0``."""
    h = hashlib.sha256()
    h.update(command.encode("utf-8"))
    h.update(b"\0")
    for a in args:
        h.update(a.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _legacy_env_hash(env: dict[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(env):
        h.update(k.encode("utf-8") + b"=" + env[k].encode("utf-8") + b"\0")
    return h.hexdigest()


def test_interpreter_hash_is_stable_across_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ["-s", str(tmp_path / "apps" / "demo" / "server.py")]
    old = _pin_release(monkeypatch, tmp_path / "kirocrew" / "0.8.0.2" / "payload")
    before = hash_command(old["interpreter"], args)
    new = _pin_release(monkeypatch, tmp_path / "kirocrew" / "0.8.0.3" / "payload")
    after = hash_command(new["interpreter"], args)

    assert before == after
    # Any other interpreter -- beside it, or elsewhere -- is a different launch.
    assert hash_command(str(Path(new["prefix"]) / "bin" / "python3.13"), args) != after
    assert hash_command(str(tmp_path / "other" / "bin" / "python3.12"), args) != after
    # And a changed argument still changes the launch.
    assert hash_command(new["interpreter"], args + ["--debug"]) != after


def test_deps_boot_shim_argv_folds_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shim ``bridges.py`` inserts by absolute path moves with the release as well."""
    server = str(tmp_path / "apps" / "demo" / "server.py")
    deps = str(tmp_path / "apps" / "demo" / ".deps")
    digests = []
    for version in ("0.8.0.2", "0.8.0.3"):
        rel = _pin_release(monkeypatch, tmp_path / version)
        digests.append(hash_command(rel["interpreter"], [rel["deps_boot"], deps, server]))
    assert digests[0] == digests[1]
    # A source-less install spells the shim ``.pyc``; same role.
    assert launch_token_bytes(
        rel["deps_boot"] + "c", hashing._ALIAS_DEPS_BOOT
    ) == launch_token_bytes(rel["deps_boot"], hashing._ALIAS_DEPS_BOOT)


def test_only_the_three_exact_spellings_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment, case variants, traversal and siblings are all literal."""
    rel = _pin_release(monkeypatch, tmp_path / "prefix")
    folded = (
        (rel["interpreter"], hashing._ALIAS_INTERPRETER),
        (rel["deps_boot"], hashing._ALIAS_DEPS_BOOT),
        (rel["deps_boot"] + "c", hashing._ALIAS_DEPS_BOOT),
        (rel["site"], hashing._ALIAS_SITE_PACKAGES),
    )
    for token, role in folded:
        assert launch_token_bytes(token, role).startswith(b"\xffkirocrew-install:")
    literal = (
        str(Path(rel["prefix"]) / "bin" / "python3"),  # a sibling under the prefix
        rel["interpreter"].upper(),  # a case variant of the interpreter
        os.sep.join((rel["prefix"], "..", "server")),  # traversal through the prefix
        rel["prefix"],  # the prefix itself
        rel["site"] + os.sep,  # a trailing separator
        str(Path(rel["site"]) / "kiro_crew" / "apps" / "other.py"),  # another package file
    )
    for token in literal:
        for role in (
            hashing._ALIAS_INTERPRETER,
            hashing._ALIAS_DEPS_BOOT,
            hashing._ALIAS_SITE_PACKAGES,
        ):
            assert launch_token_bytes(token, role) == token.encode("utf-8"), token


def test_an_alias_folds_only_in_the_slot_the_gateway_emits_it_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot names the role, not the token: a spelling out of place hashes literally.

    ``bridges.py`` writes the interpreter as the command, the shim as an
    argument and ``site-packages`` as a ``PYTHONPATH`` segment, and nowhere
    else. The same spellings in any other position are operator- or
    agent-written content, so a launch that carries them there is still a
    changed launch when a release moves them.
    """
    server = str(tmp_path / "apps" / "demo" / "server.py")
    digests: dict[str, list[str]] = {"argv": [], "env": [], "cmd": []}
    for version in ("0.8.0.2", "0.8.0.3"):
        rel = _pin_release(monkeypatch, tmp_path / version)
        # site-packages as an argument value, the interpreter as an argument.
        digests["argv"].append(
            hash_command(
                rel["interpreter"], ["--plugin-root", rel["site"], rel["interpreter"], server]
            )
        )
        # site-packages under any key but PYTHONPATH.
        digests["env"].append(
            hashing.hash_declared_env({"PLUGIN_ROOT": rel["site"], "LD_LIBRARY_PATH": rel["site"]})
        )
        # the shim or the package directory as the command.
        digests["cmd"].append(
            hash_command(rel["deps_boot"], [server]) + hash_command(rel["site"], [])
        )
    for slot, pair in digests.items():
        assert pair[0] != pair[1], slot
    # ...and in their own slots the same spellings still fold.
    assert hash_command(rel["interpreter"], [rel["deps_boot"], server]) == hash_command(
        _pin_release(monkeypatch, tmp_path / "0.8.0.4")["interpreter"],
        [_pin_release(monkeypatch, tmp_path / "0.8.0.4")["deps_boot"], server],
    )
    legacy_args = ["--plugin-root", rel["site"], server]
    assert hash_command("/usr/bin/node", legacy_args) == _legacy_hash("/usr/bin/node", legacy_args)
    assert hashing.hash_declared_env({"PLUGIN_ROOT": rel["site"]}) == _legacy_env_hash(
        {"PLUGIN_ROOT": rel["site"]}
    )


def test_a_stub_launched_by_its_short_name_hashes_the_gateways_launch_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriter (long spelling) and stub (8.3 spelling of the same files) agree.

    ``rewriter._cmd_safe_command`` launches the stub through the install's 8.3
    short path when the path holds a space, so the stub's ``sys.executable``
    and package directory come back short while the launch it hashes carries
    the gateway's long spellings. The stub's alias table therefore also knows
    each of its own paths by its long name -- computed from itself, never from
    the token -- and the two processes reach one digest.
    """
    real_long_path = hashing._windows_long_path
    long_prefix = tmp_path / "Program Files" / "kirocrew" / "0.8.0.2" / "payload"
    short_prefix = tmp_path / "PROGRA~1" / "kirocrew" / "0.8.0.2" / "payload"
    # The gateway: long spellings, hashes the launch it writes.
    gw = _pin_release(monkeypatch, long_prefix)
    launch_args = [gw["deps_boot"], str(tmp_path / "apps" / "demo" / ".deps"), "server.py"]
    env = {"PYTHONPATH": gw["site"]}
    gateway_cmd = hash_command(gw["interpreter"], launch_args)
    gateway_env = hashing.hash_declared_env(env)

    # The stub: the same files under their 8.3 names, and a long-name resolver
    # that answers for its OWN paths only.
    stub = _pin_release(monkeypatch, short_prefix)
    long_of = {
        stub["interpreter"]: gw["interpreter"],
        stub["deps_boot"]: gw["deps_boot"],
        stub["deps_boot"] + "c": gw["deps_boot"] + "c",
        stub["site"]: gw["site"],
    }
    monkeypatch.setattr(hashing, "_IS_WINDOWS", True)
    monkeypatch.setattr(hashing, "_windows_long_path", lambda path: long_of.get(path))
    assert hash_command(gw["interpreter"], launch_args) == gateway_cmd
    assert hashing.hash_declared_env(env) == gateway_env
    assert hashing.install_resident(gw["interpreter"])
    # Without the long-name entries the stub would hash the launch literally.
    monkeypatch.setattr(hashing, "_windows_long_path", lambda path: None)
    assert hash_command(gw["interpreter"], launch_args) != gateway_cmd
    # Off Windows the real resolver is inert: one spelling per path.
    monkeypatch.setattr(hashing, "_windows_long_path", real_long_path)
    monkeypatch.setattr(hashing, "_IS_WINDOWS", False)
    assert hashing._windows_long_path(stub["interpreter"]) is None
    assert len(hashing.install_aliases()) == 4


def test_tokens_outside_the_aliases_keep_the_legacy_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that names none of the three hashes byte-for-byte as before."""
    _pin_release(monkeypatch, tmp_path / "prefix")
    for command, args in (
        ("/usr/bin/node", ["server.js", "--stdio"]),
        (str(tmp_path / "apps" / "x" / ".venv" / "bin" / "python"), ["-m", "srv"]),
        ("npx", ["-y", "@scope/pkg"]),
        ("", []),
    ):
        assert hash_command(command, args) == _legacy_hash(command, args)
    for env in (
        {"PATH": "/usr/bin" + os.pathsep + "/bin" + os.pathsep, "MODE": "a"},
        {"URL": "https://example.test:8443/x", "EMPTY": ""},
        {"PYTHONPATH": str(tmp_path / "apps" / "x" / ".deps")},
        {},
    ):
        assert hashing.hash_declared_env(env) == _legacy_env_hash(env)
        assert hashing.hash_effective_env(env) == _legacy_env_hash(env)


def test_no_literal_argv_can_spell_the_alias_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``0xFF`` is not UTF-8, so no typed token sequence reproduces the digest."""
    rel = _pin_release(monkeypatch, tmp_path / "prefix")
    approved = hash_command(rel["interpreter"], ["-m", "srv"])
    forgeries = (
        ("\xffkirocrew-install:interpreter\xff", ["-m", "srv"]),
        ("", ["kirocrew-install:interpreter", "-m", "srv"]),
        ("kirocrew-install:interpreter", ["-m", "srv"]),
        ("\x00kirocrew-install:interpreter\x00", ["-m", "srv"]),
    )
    for command, args in forgeries:
        assert hash_command(command, args) != approved
    with pytest.raises(UnicodeDecodeError):
        launch_token_bytes(rel["interpreter"], hashing._ALIAS_INTERPRETER).decode("utf-8")


def test_declared_env_site_packages_segment_folds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the fingerprint: the host-CLI pin's ``PYTHONPATH`` segment."""
    stable = str(tmp_path / "apps" / "demo")
    digests = []
    for version in ("0.8.0.2", "0.8.0.3"):
        rel = _pin_release(monkeypatch, tmp_path / version)
        env = {
            "PYTHONPATH": rel["site"] + os.pathsep + stable,
            "KIROCREW_HOME": str(tmp_path / "h"),
        }
        digests.append(hashing.hash_declared_env(env))
    assert digests[0] == digests[1]
    other = {"PYTHONPATH": str(tmp_path / "elsewhere"), "KIROCREW_HOME": str(tmp_path / "h")}
    assert hashing.hash_declared_env(other) != digests[0]


def test_approval_recorded_under_one_release_admits_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the approval store: approve on 0.8.0.2, resolve on 0.8.0.3."""
    args = [str(tmp_path / "apps" / "demo" / "server.py")]
    env_hash = launch_approval.env_fingerprint({})
    store = tmp_path / "approvals.json"

    old = _pin_release(monkeypatch, tmp_path / "kirocrew" / "0.8.0.2" / "payload")
    launch_approval.approve(
        {
            "demo": [
                launch_approval.ResolvedLaunch(
                    launch_approval.launch_fingerprint(old["interpreter"], args, {}),
                    old["interpreter"],
                    tuple(args),
                    frozenset({env_hash}),
                )
            ]
        },
        path=store,
    )

    new = _pin_release(monkeypatch, tmp_path / "kirocrew" / "0.8.0.3" / "payload")
    approvals = launch_approval.load_approvals(store)
    assert approvals.admits_command("DEMO", hash_command(new["interpreter"], args))
    assert approvals.admits_launch("DEMO", hash_command(new["interpreter"], args), env_hash)
    assert not approvals.admits_command("DEMO", hash_command(new["interpreter"], args + ["-v"]))
    assert not approvals.admits_command(
        "DEMO", hash_command(str(tmp_path / "elsewhere" / "python3.12"), args)
    )


def test_host_cli_pin_is_admitted_across_an_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``command: kirocrew`` pinned by ``_pin_host_cli_command`` under 0.8.0.2 admits under 0.8.0.3.

    The pin writes ``sys.executable`` into the command AND the versioned
    ``site-packages`` into the declared ``PYTHONPATH``; the approval fingerprint
    covers both halves, so both must fold.
    """
    from kiro_crew.apps import bridges

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    store = tmp_path / "approvals.json"
    fingerprints: list[str] = []
    launches: list[tuple[str, list[str], dict[str, str], str]] = []
    for version in ("0.8.0.2", "0.8.0.3"):
        rel = _pin_release(monkeypatch, tmp_path / "kirocrew" / version / "payload")
        monkeypatch.setattr(sys, "executable", rel["interpreter"])
        monkeypatch.setattr(
            bridges,
            "kiro_crew_file",
            lambda r=rel: str(Path(r["site"]) / "kiro_crew" / "__init__.py"),
        )
        cfg = bridges._pin_host_cli_command("demo", {"command": "kirocrew", "args": ["mcp-core"]})
        assert cfg["command"] == rel["interpreter"]
        assert cfg["env"]["PYTHONPATH"] == rel["site"]
        # The derived-env hash is taken when the operator approves, i.e. under
        # THIS release, exactly as the rewriter records it.
        launches.append(
            (
                cfg["command"],
                list(cfg["args"]),
                dict(cfg["env"]),
                launch_approval.env_fingerprint(cfg["env"]),
            )
        )
        fingerprints.append(
            launch_approval.launch_fingerprint(cfg["command"], cfg["args"], cfg["env"])
        )
    assert fingerprints[0] == fingerprints[1]

    old_cmd, old_args, _old_env, old_env_hash = launches[0]
    launch_approval.approve(
        {
            "demo": [
                launch_approval.ResolvedLaunch(
                    fingerprints[0], old_cmd, tuple(old_args), frozenset({old_env_hash})
                )
            ]
        },
        path=store,
    )
    new_cmd, new_args, new_env, _new_env_hash = launches[1]
    approvals = launch_approval.load_approvals(store)
    assert approvals.admits_launch(
        "DEMO", hash_command(new_cmd, new_args), launch_approval.env_fingerprint(new_env)
    )


def test_the_rewriters_own_spellings_are_the_alias_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpatched invariant: what ``bridges.py`` emits is exactly what the hash folds.

    This is the whole security argument -- a token is folded only when it is
    byte-identical to a string this process computed from its own
    ``sys.executable`` and package location -- so the emitted spellings must
    be those strings, on this host, with nothing monkeypatched.
    """
    from kiro_crew.apps import bridges

    aliases = hashing.install_aliases()
    assert aliases[sys.executable] == hashing._ALIAS_INTERPRETER
    assert aliases[str(bridges._DEPS_BOOT_PATH)] == hashing._ALIAS_DEPS_BOOT
    cfg = bridges._pin_host_cli_command("demo", {"command": "kirocrew", "args": ["mcp-core"]})
    assert aliases[cfg["command"]] == hashing._ALIAS_INTERPRETER
    assert aliases[cfg["env"]["PYTHONPATH"]] == hashing._ALIAS_SITE_PACKAGES
    assert hashing.install_resident(cfg["command"])


def test_install_resident_is_the_interpreter_alias_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = _pin_release(monkeypatch, tmp_path / "prefix")
    assert hashing.install_resident(rel["interpreter"])
    assert not hashing.install_resident(rel["deps_boot"])
    assert not hashing.install_resident(rel["site"])
    assert not hashing.install_resident(str(tmp_path / "apps" / "x" / ".venv" / "bin" / "python"))
    assert not hashing.install_resident("npx")


def test_runs_install_code_needs_the_interpreter_and_kiro_crew_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = _pin_release(monkeypatch, tmp_path / "prefix")
    py = rel["interpreter"]
    assert hashing.runs_install_code(py, ["-P", "-m", "kiro_crew", "app", "mcp", "demo"])
    assert hashing.runs_install_code(py, ["-s", "-m", "kiro_crew.mcp_core"])
    assert hashing.runs_install_code(py, ["-mkiro_crew.apps.x"])
    assert hashing.runs_install_code(py, [rel["deps_boot"], str(tmp_path / ".deps"), "server.py"])
    # A third-party server that merely runs ON the interpreter is not install code.
    assert not hashing.runs_install_code(py, [str(tmp_path / "apps" / "demo" / "server.py")])
    assert not hashing.runs_install_code(py, ["-m", "some_other_module"])
    assert not hashing.runs_install_code(py, ["-m", "kiro_crewish"])
    assert not hashing.runs_install_code(py, ["--mode", "kiro_crew"])
    # Nor is Kiro Crew code under a foreign interpreter.
    assert not hashing.runs_install_code(str(tmp_path / "x" / "python3"), ["-m", "kiro_crew"])


def test_binary_version_carries_the_code_fingerprint_for_the_interpreter_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the versioned path folded out of the hash, only the code fingerprint sees a release.

    Both identity consumers -- the pool's registration token and the
    shareability cache's key -- must fold it, or a host-CLI pin
    (``sys.executable -P -m kiro_crew app mcp <name>``, no file argv) would keep
    its pre-upgrade row across a release that replaced the server's code.
    """
    import kiro_crew.code_fingerprint as cf
    from kiro_crew.mcp_gateway import evaluate, stub

    rel = _pin_release(monkeypatch, tmp_path / "prefix")
    interpreter = Path(rel["interpreter"])
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"#!/bin/sh\nexit 0\n")
    outside = tmp_path / "elsewhere" / "python3"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"#!/bin/sh\nexit 0\n")
    args = ["-P", "-m", "kiro_crew", "app", "mcp", "demo"]

    monkeypatch.setattr(cf, "code_fingerprint", lambda: "release-A")
    monkeypatch.setattr(evaluate, "code_fingerprint", lambda: "release-A")
    pool_a = stub.pool_binary_version(str(interpreter), args)
    cache_a = evaluate._launch_fingerprint(str(interpreter), args)

    monkeypatch.setattr(cf, "code_fingerprint", lambda: "release-B")
    monkeypatch.setattr(evaluate, "code_fingerprint", lambda: "release-B")
    pool_b = stub.pool_binary_version(str(interpreter), args)
    cache_b = evaluate._launch_fingerprint(str(interpreter), args)

    assert pool_a != pool_b and pool_a.endswith("+release-A") and pool_b.endswith("+release-B")
    # The daemon-generation half stays argv-selected: only a managed subcommand
    # runs that check, so the install-code bridge reports no generation.
    assert stub._pool_binary_identity(str(interpreter), args) == (pool_b, "")
    assert cache_a != cache_b and "code:release-A" in cache_a and "code:release-B" in cache_b
    # Same bytes, same argv, but not the install's interpreter: no code
    # fingerprint, so a third-party server's identity is exactly what it was.
    assert "+" not in stub.pool_binary_version(str(outside), ["--stdio"])
    assert "code:" not in evaluate._launch_fingerprint(str(outside), ["--stdio"])
    # A third-party app server that merely runs ON the install's interpreter
    # (``python3 server.py`` rewritten) is not re-keyed by a Kiro Crew release:
    # its code is the file in argv, which the identity fingerprints itself.
    server = tmp_path / "apps" / "demo" / "server.py"
    server.parent.mkdir(parents=True)
    server.write_text("print('hi')\n")
    assert "+" not in stub.pool_binary_version(str(interpreter), [str(server)])
    assert "code:" not in evaluate._launch_fingerprint(str(interpreter), [str(server)])


def test_the_long_name_lookup_runs_once_per_own_path() -> None:
    """``install_aliases`` is called per hashed token, sometimes on the event loop.

    The long-name lookup is its only filesystem touch, so it must be memoized:
    hashing many tokens resolves each of this process's own paths at most once.
    """
    hashing._windows_long_path.cache_clear()
    for _ in range(5):
        hashing.hash_command(sys.executable, ["-m", "x", "--flag"])
    info = hashing._windows_long_path.cache_info()
    assert info.misses <= 4
    assert info.hits >= info.misses
