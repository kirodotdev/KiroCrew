"""Behaviour of the resolve-once store for npm-launcher MCP servers.

The store's whole contract is that it can only ever REMOVE dependency resolution
from a launch, never add a failure to one. So most of what is pinned here is the
shape of a miss: a spec that is not an npm launcher, a record that is absent,
malformed, stale, or points at a file that no longer exists must each read as
"launch it the way you would have anyway" rather than as an error.

No test spawns npm. ``install`` is exercised through a stubbed ``npm`` that
writes a tree, so the assertions are about the store's own logic -- entry-point
discovery, the atomic commit, containment -- and not about npm's behaviour.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import resolve_once as R

# --- Spec parsing -----------------------------------------------------------


class TestParseNpmLauncher:
    @pytest.mark.parametrize(
        "command,args,expected_pkg,expected_pass",
        [
            ("npx", ["-y", "foo@1.2.3"], "foo@1.2.3", ()),
            ("npx", ["-y", "foo@1.2.3", "--flag", "v"], "foo@1.2.3", ("--flag", "v")),
            ("/usr/bin/npx", ["foo"], "foo", ()),
            ("npm", ["exec", "-y", "foo@1.0.0"], "foo@1.0.0", ()),
            ("npx", ["-y", "--", "foo@1.0.0"], "foo@1.0.0", ()),
            ("npx", ["-y", "@scope/foo@0.3.0", "--x"], "@scope/foo@0.3.0", ("--x",)),
        ],
    )
    def test_recognised_shapes(self, command, args, expected_pkg, expected_pass) -> None:
        spec = R.parse_npm_launcher(command, args)
        assert spec is not None
        assert spec.package == expected_pkg
        assert spec.passthrough == expected_pass

    @pytest.mark.parametrize(
        "command,args",
        [
            ("node", ["server.js"]),
            ("uvx", ["some-server"]),
            ("npm", ["run", "build"]),
            ("npm", []),
            ("npx", []),
            ("npx", ["-y"]),
            # An unknown flag may or may not consume a value, so the package
            # boundary is unknowable and guessing could launch the wrong thing.
            ("npx", ["--package", "other", "foo"]),
        ],
    )
    def test_passthrough_shapes(self, command, args) -> None:
        assert R.parse_npm_launcher(command, args) is None

    @pytest.mark.parametrize(
        "package,pinned",
        [
            ("foo@1.2.3", True),
            ("foo@1.2.3-rc.1", True),
            ("@scope/foo@0.0.1", True),
            ("foo@latest", False),
            ("foo@^1.2.0", False),
            ("foo@1.x", False),
            ("foo", False),
            # A leading @ is the scope marker, not a version separator.
            ("@scope/foo", False),
        ],
    )
    def test_pinned_detection(self, package, pinned) -> None:
        assert R.NpmSpec(package=package, passthrough=()).pinned is pinned

    def test_digest_keys_on_the_spec_as_written(self) -> None:
        # The record's identity is the question, not the answer: an unpinned spec
        # must keep ONE directory across refreshes instead of leaking a new one
        # per upstream release.
        a = R.NpmSpec(package="foo@latest", passthrough=())
        b = R.NpmSpec(package="foo@latest", passthrough=())
        c = R.NpmSpec(package="foo@latest", passthrough=("--x",))
        assert a.digest == b.digest
        assert a.digest != c.digest

    def test_a_spec_that_refuses_to_install_is_not_ours_to_resolve(self) -> None:
        """``npx --no-install`` is the user declining a download.

        Treating it as just another launcher flag and then installing the package
        anyway would run exactly the lifecycle code that invocation refused, and
        would make a command that is SUPPOSED to fail on a missing package
        succeed instead.
        """
        assert R.parse_npm_launcher("npx", ["--no-install", "foo@1.0.0"]) is None
        assert R.parse_npm_launcher("npx", ["--no", "foo@1.0.0"]) is None
        # The refusal has to survive being mixed with flags we do consume.
        assert R.parse_npm_launcher("npx", ["-y", "--no-install", "foo@1.0.0"]) is None

    def test_a_flag_shaped_package_is_refused(self) -> None:
        """A spec that looks like an option must never reach ``npm install``.

        ``npx -- --prefix=/somewhere`` puts a flag where the package belongs. npm
        would parse it as ITS option, so pre-resolving it would redirect the
        install into that directory -- writing into whatever project the path
        names. Refused at parse time, and the call site also passes ``--`` so
        neither layer alone is load-bearing.
        """
        assert R.parse_npm_launcher("npx", ["--", "--prefix=/tmp/victim"]) is None
        assert R.parse_npm_launcher("npx", ["-y", "--", "-g"]) is None

    def test_the_npm_argv_puts_the_package_after_a_separator(self) -> None:
        # Structural: the operand separator has to sit immediately before the
        # package, or a flag-shaped spec that slipped past the parser would be
        # read by npm as an option.
        source = inspect.getsource(R.install)
        assert '"--",\n        spec.package,' in source

    def test_the_flags_npx_merely_consumes_are_still_consumed(self) -> None:
        spec = R.parse_npm_launcher("npx", ["-y", "--prefer-offline", "foo@1.0.0"])
        assert spec is not None
        assert spec.package == "foo@1.0.0"


# --- Records and freshness --------------------------------------------------


class TestRecordIO:
    def test_two_writers_do_not_publish_each_others_record(self, tmp_path) -> None:
        """Concurrent writers must not share one temp path.

        The timed pass and an operator pressing "Update now" can both be here at
        once. With a fixed ``record.json.tmp`` the second writer truncated the
        first's temp file, the first's rename published the SECOND's bytes, and
        the second's rename then failed with its temp file already gone -- which
        sent it down the cleanup path where it deleted the very tree the freshly
        published record pointed at. This pins that no writer's rename fails and
        no writer can see another's temp file.
        """
        directory = tmp_path / "spec"
        directory.mkdir()
        first = R.ResolvedRecord(
            package="foo@latest", entrypoint="r-a/x.js", resolved_at=1.0, pinned=False
        )
        second = R.ResolvedRecord(
            package="foo@latest", entrypoint="r-b/x.js", resolved_at=2.0, pinned=False
        )
        seen: list[str] = []
        real_replace = os.replace

        def interleaving_replace(src, dst):
            # Simulate the other writer landing between our write and our rename.
            seen.append(os.path.basename(str(src)))
            if len(seen) == 1:
                R.write_record(str(directory), second)
            return real_replace(src, dst)

        original = R.os.replace
        R.os.replace = interleaving_replace  # type: ignore[assignment]
        try:
            R.write_record(str(directory), first)
        finally:
            R.os.replace = original  # type: ignore[assignment]

        # Each writer used its own temp file, so neither rename raised.
        assert len(set(seen)) == len(seen)
        # Whichever record is live, it is one of the two written -- never a mix,
        # and never absent.
        live = R.read_record(str(directory))
        assert live in (first, second)
        # No temp file is left behind to be mistaken for a resolution tree.
        leftovers = [p for p in os.listdir(directory) if p != "record.json"]
        assert leftovers == []

    def test_round_trip(self, tmp_path) -> None:
        rec = R.ResolvedRecord(
            package="foo@1.0.0",
            entrypoint="r-1/node_modules/foo/cli.js",
            resolved_at=123.0,
            pinned=True,
        )
        R.write_record(str(tmp_path), rec)
        assert R.read_record(str(tmp_path)) == rec

    def test_absent_reads_as_miss(self, tmp_path) -> None:
        assert R.read_record(str(tmp_path)) is None

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            json.dumps(["a", "list"]),
            json.dumps({"entrypoint": "x", "resolved_at": 1}),  # no package
            json.dumps({"package": "p", "resolved_at": 1}),  # no entrypoint
            json.dumps({"package": "p", "entrypoint": "x"}),  # no timestamp
            json.dumps({"package": "p", "entrypoint": "x", "resolved_at": "soon"}),
        ],
    )
    def test_malformed_reads_as_miss(self, tmp_path, payload) -> None:
        # The file is ours, but a truncated write must degrade to a cache miss
        # rather than raise on the spawn path.
        (tmp_path / "record.json").write_text(payload, encoding="utf-8")
        assert R.read_record(str(tmp_path)) is None

    def test_write_is_atomic_leaves_no_temp(self, tmp_path) -> None:
        R.write_record(
            str(tmp_path),
            R.ResolvedRecord(package="p", entrypoint="e", resolved_at=1.0, pinned=False),
        )
        assert sorted(p.name for p in tmp_path.iterdir()) == ["record.json"]

    def test_write_commits_through_a_rename(self, tmp_path, monkeypatch) -> None:
        # Atomicity IS the contract: the spawn path reads this file without any
        # lock, so it must never observe a partially-written one. Pinned by
        # observing the rename rather than by racing a reader.
        seen: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst, *a, **k):
            seen.append((str(src), str(dst)))
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(R.os, "replace", spy)
        R.write_record(
            str(tmp_path),
            R.ResolvedRecord(package="p", entrypoint="e", resolved_at=1.0, pinned=False),
        )
        assert len(seen) == 1
        src, dst = seen[0]
        assert src.endswith(".tmp")
        assert dst.endswith("record.json")


class TestVersionParsing:
    @pytest.mark.parametrize(
        "package,version",
        [
            ("foo@1.2.3", "1.2.3"),
            ("@scope/foo@1.2.3", "1.2.3"),
            ("foo", ""),
            # A leading @ is the scope marker. Reading it as a version separator
            # would make the "version" of an unversioned scoped package its own
            # name, which is wrong even though both spellings read as unpinned.
            ("@scope/foo", ""),
        ],
    )
    def test_version_of(self, package, version) -> None:
        assert R._version_of(package) == version


class TestFreshness:
    def _rec(self, *, pinned: bool, age: float) -> R.ResolvedRecord:
        return R.ResolvedRecord(
            package="p", entrypoint="e", resolved_at=time.time() - age, pinned=pinned
        )

    def test_pinned_is_never_stale(self) -> None:
        # Re-asking the registry about an exact version cannot change the answer.
        rec = self._rec(pinned=True, age=10 * R.DEFAULT_REFRESH_SECS)
        assert R.is_stale(rec, refresh_secs=R.DEFAULT_REFRESH_SECS) is False

    def test_unpinned_goes_stale_on_the_clock(self) -> None:
        fresh = self._rec(pinned=False, age=60)
        old = self._rec(pinned=False, age=R.DEFAULT_REFRESH_SECS + 60)
        assert R.is_stale(fresh, refresh_secs=R.DEFAULT_REFRESH_SECS) is False
        assert R.is_stale(old, refresh_secs=R.DEFAULT_REFRESH_SECS) is True

    def test_zero_window_always_stale_for_unpinned(self) -> None:
        assert R.is_stale(self._rec(pinned=False, age=0), refresh_secs=0) is True


# --- The synchronous lookup on the spawn path -------------------------------


def _commit_tree(home: str, spec: R.NpmSpec, *, entry_rel: str = "r-1/node_modules/foo/cli.js"):
    """Materialise a resolved tree + record the way ``install`` would."""
    root = R.spec_dir(home, spec)
    target = os.path.join(root, os.path.dirname(entry_rel))
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(root, entry_rel), "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env node\n")
    R.write_record(
        root,
        R.ResolvedRecord(
            package=spec.package,
            entrypoint=entry_rel,
            resolved_at=time.time(),
            pinned=spec.pinned,
        ),
    )
    return root


class TestResolvedLaunch:
    def test_hit_returns_node_and_replays_passthrough(self, tmp_path) -> None:
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=("--flag",))
        root = _commit_tree(str(tmp_path), spec)
        launch = R.resolved_launch(str(tmp_path), "npx", ["-y", "foo@1.0.0", "--flag"])
        assert launch is not None
        command, args = launch
        assert command == "node"
        assert args == [os.path.join(root, "r-1/node_modules/foo/cli.js"), "--flag"]

    def test_launch_never_searches_path(self, tmp_path, monkeypatch) -> None:
        """The lookup runs on the event loop, so it must not stat every PATH entry.

        ``shutil.which`` walks the whole of ``PATH``; one stalled NFS or autofs
        mount there would freeze routing and heartbeat processing for every
        session. The name is handed to the spawn, which resolves it off the loop
        -- exactly as the ``npx`` launcher this replaces was already resolved.
        """

        def boom(*_a, **_k):  # pragma: no cover - the assertion is that it is unused
            raise AssertionError("resolved_launch must not search PATH")

        monkeypatch.setattr(R.shutil, "which", boom)
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        _commit_tree(str(tmp_path), spec)
        launch = R.resolved_launch(str(tmp_path), "npx", ["-y", "foo@1.0.0"])
        assert launch is not None
        assert launch[0] == "node"

    def test_non_npm_command_is_a_miss(self, tmp_path) -> None:
        assert R.resolved_launch(str(tmp_path), "node", ["server.js"]) is None

    def test_unresolved_spec_is_a_miss(self, tmp_path) -> None:
        assert R.resolved_launch(str(tmp_path), "npx", ["-y", "never-seen@1.0.0"]) is None

    def test_missing_entrypoint_is_a_miss(self, tmp_path) -> None:
        # A pruned tree or a partial install leaves the record behind; launching
        # a path that is gone would fail where npx would have worked.
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        root = _commit_tree(str(tmp_path), spec)
        os.remove(os.path.join(root, "r-1/node_modules/foo/cli.js"))
        assert R.resolved_launch(str(tmp_path), "npx", ["-y", "foo@1.0.0"]) is None

    def test_traversing_entrypoint_is_refused(self, tmp_path, monkeypatch) -> None:
        # ``entrypoint`` is replayed from a file on disk, so a hand-edit must not
        # turn the record into an exec of an arbitrary path.
        outside = tmp_path / "outside.js"
        outside.write_text("x", encoding="utf-8")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        root = R.spec_dir(str(tmp_path), spec)
        os.makedirs(root, exist_ok=True)
        R.write_record(
            root,
            R.ResolvedRecord(
                package=spec.package,
                entrypoint="../../../outside.js",
                resolved_at=time.time(),
                pinned=True,
            ),
        )
        assert R.resolved_launch(str(tmp_path), "npx", ["-y", "foo@1.0.0"]) is None

    def test_digest_collision_execs_nothing(self, tmp_path, monkeypatch) -> None:
        """A 64-bit digest collision must NOT exec the other spec's program (CWE-328).

        ``spec_dir`` keys on the ``NpmSpec.digest`` property = a SHA-256 truncated
        to 16 hex chars (64 bits), so two distinct specs can collide onto the same
        directory. Force that: pin every spec's digest to one constant so the
        installed ``innocent`` tree and a requested ``evil`` spec share a dir.
        Without the ``record.package == spec.package`` guard the path-containment
        and isfile checks both pass and the launcher execs the WRONG program;
        with it the mismatch is a cache miss. A control run for the installed
        spec (same constant digest) still launches, proving the miss is the
        package-equality guard and not a broken directory.
        """
        monkeypatch.setattr(R.shutil, "which", lambda _n: "/usr/bin/node")
        monkeypatch.setattr(R.NpmSpec, "digest", property(lambda self: "deadbeefdeadbeef"))
        installed = R.NpmSpec(package="innocent@1.0.0", passthrough=())
        _commit_tree(str(tmp_path), installed)

        # The requested spec resolves to the SAME dir but names a different pkg.
        assert R.resolved_launch(str(tmp_path), "npx", ["-y", "evil@9.9.9"]) is None
        # Control: the spec the tree was actually installed for still launches.
        assert R.resolved_launch(str(tmp_path), "npx", ["-y", "innocent@1.0.0"]) is not None

    def test_stale_record_still_launches(self, tmp_path, monkeypatch) -> None:
        # Staleness is the prefetcher's business. A stale tree still launches
        # correctly, and treating "due for refresh" as "cannot launch" would turn
        # an update signal into an outage.
        monkeypatch.setattr(R.shutil, "which", lambda _n: "/usr/bin/node")
        spec = R.NpmSpec(package="foo@latest", passthrough=())
        root = R.spec_dir(str(tmp_path), spec)
        os.makedirs(os.path.join(root, "r-1/node_modules/foo"), exist_ok=True)
        with open(os.path.join(root, "r-1/node_modules/foo/cli.js"), "w", encoding="utf-8") as fh:
            fh.write("x")
        R.write_record(
            root,
            R.ResolvedRecord(
                package="foo@latest",
                entrypoint="r-1/node_modules/foo/cli.js",
                resolved_at=0.0,
                pinned=False,
            ),
        )
        assert R.resolved_launch(str(tmp_path), "npx", ["-y", "foo@latest"]) is not None


# --- Enumerating what to prefetch -------------------------------------------


class TestLaunchSpecs:
    def test_dedupes_the_disambiguated_twin(self) -> None:
        # The rewriter writes both a bare server-name key and an args-hashed one
        # for the same launch; resolving twice would install the same tree twice.
        out = R.launch_specs(
            {
                "KIROCREW_MCP_TARGET_FOO": "npx -y foo@1.0.0",
                "KIROCREW_MCP_TARGET_FOO__deadbeef": "npx -y foo@1.0.0",
            }
        )
        assert out == [("npx", ["-y", "foo@1.0.0"])]

    def test_accepts_the_legacy_prefix_and_ignores_others(self) -> None:
        out = R.launch_specs(
            {
                "MC_MCP_TARGET_OLD": "npx -y old@1.0.0",
                "PATH": "npx -y not-a-target@1.0.0",
            }
        )
        assert out == [("npx", ["-y", "old@1.0.0"])]

    def test_unparsable_and_empty_are_skipped(self) -> None:
        assert R.launch_specs({"KIROCREW_MCP_TARGET_A": '"unterminated'}) == []
        assert R.launch_specs({"KIROCREW_MCP_TARGET_B": "   "}) == []


# --- Installing (npm stubbed) -----------------------------------------------


#: The npm double below is a ``#!/bin/sh`` script, so every test that spawns it is
#: POSIX-only. That is a limitation of the HARNESS, not of the module: the install
#: path shells out to whatever ``npm`` the host has (``npm.cmd`` on Windows) and
#: its logic is platform-neutral. The reaping tests are POSIX-specific for a second
#: reason -- ``start_new_session`` and process groups do not exist on Windows, where
#: ``platform_compat.kill_process_tree`` uses ``taskkill /T`` and has its own tests.
#:
#: Everything portable is still exercised on Windows: spec parsing, record IO,
#: freshness, launch substitution, and the npm-context guard all run there.
_posix_only = pytest.mark.skipif(os.name == "nt", reason="the npm double is a POSIX shell script")


def _fake_npm(
    tmp_path,
    *,
    bin_field,
    rc: int = 0,
    name: str = "foo",
    write_tree_anyway: bool = False,
    main: str | None = None,
    shebang: str = "#!/usr/bin/env node",
) -> str:
    """A shell stub standing in for npm that writes a tree and exits ``rc``.

    Reads the ``--prefix`` it was handed so the test exercises the real prefix
    plumbing rather than assuming where install lands.

    ``write_tree_anyway`` makes it write the tree and STILL exit non-zero, which
    is the realistic partial-failure npm actually produces and the only shape
    that proves the return code is what gates the commit.

    ``bin_field=None`` omits ``bin`` entirely (with ``main`` to prove ``main`` is
    not a fallback), and ``shebang`` sets the interpreter line so the
    node-runnable check can be exercised both ways. The written bin is named
    after the manifest value, so a non-``.js`` name really does depend on its
    shebang.
    """
    key = f"{rc}-{int(write_tree_anyway)}-{name}-{abs(hash((str(bin_field), main, shebang)))}"
    script = tmp_path / f"fake-npm-{key}"
    manifest: dict = {"name": name, "version": "1.0.0"}
    if bin_field is not None:
        manifest["bin"] = bin_field
    if main is not None:
        manifest["main"] = main
    payload = json.dumps(manifest)
    if isinstance(bin_field, str):
        rel_files = [bin_field]
    elif isinstance(bin_field, dict):
        rel_files = [v for v in bin_field.values() if isinstance(v, str)]
    else:
        rel_files = []
    if main:
        rel_files.append(main)
    rel_files = rel_files or ["cli.js"]
    early_exit = "" if write_tree_anyway else f"[ {rc} -ne 0 ] && exit {rc}\n"
    writes = "".join(
        f'mkdir -p "$(dirname "$prefix/node_modules/{name}/{rel}")"\n'
        f'printf "%s\\n" "{shebang}" > "$prefix/node_modules/{name}/{rel}"\n'
        for rel in rel_files
    )
    script.write_text(
        "#!/bin/sh\n"
        'prefix=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "--prefix" ]; then prefix="$2"; shift 2; else shift; fi\n'
        "done\n" + early_exit + f'mkdir -p "$prefix/node_modules/{name}"\n'
        f"cat > \"$prefix/node_modules/{name}/package.json\" <<'EOF'\n{payload}\nEOF\n"
        + writes
        + f"exit {rc}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


_sandbox_calls: list[dict] = []


@pytest.fixture(autouse=True)
def _hermetic_sandbox(monkeypatch):
    """Spawn the npm stub directly instead of through the real OS sandbox.

    ``install`` routes its spawn through ``sandboxed_spawn_argv``, and that
    routing is what ``test_spawn_audit`` enforces -- by reading the source, not
    by running it. Exercising the real chokepoint here would make every install
    test depend on the HOST instead of on this module: a kernel that refuses
    ``unshare(CLONE_NEWUSER)``, or a suite run that redirected ``HOME`` so the
    box's own sandbox config is out of view, raises
    ``SandboxUnavailableError`` before npm is ever reached. MEASURED: exactly
    that turned two of these tests red under the full suite while a scoped run
    of the same tests passed, because the sandbox probe caches per worker
    process and the answer depended on which test primed it first.

    The passthrough records what it was handed so the routing arguments can
    still be pinned below -- the audit proves the call exists, this proves it
    asks for the right mode.
    """
    _sandbox_calls.clear()

    def _passthrough(argv, **kwargs):
        _sandbox_calls.append({"argv": list(argv), **kwargs})
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(R, "sandboxed_spawn_argv", _passthrough)


@_posix_only
@pytest.mark.asyncio
class TestInstall:
    async def test_install_asks_the_sandbox_for_the_launch_mode(self, tmp_path) -> None:
        # Parity with the probe and the launch is the whole argument for routing
        # an install through here: a weaker mode would make a registry that
        # those two can reach unreachable for this one.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        await R.install(home, spec, npm=_fake_npm(tmp_path, bin_field="cli.js"))
        assert len(_sandbox_calls) == 1
        assert _sandbox_calls[0]["mode"] == "standard"
        assert _sandbox_calls[0]["strip_python_env"] is True

    async def test_commits_a_record_pointing_at_the_bin(self, tmp_path) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        rec = await R.install(home, spec, npm=_fake_npm(tmp_path, bin_field="cli.js"))
        assert rec is not None
        assert rec.pinned is True
        assert rec.entrypoint.endswith("node_modules/foo/cli.js")
        assert os.path.isfile(os.path.join(R.spec_dir(home, spec), rec.entrypoint))
        assert R.read_record(R.spec_dir(home, spec)) == rec

    async def test_bin_table_prefers_the_package_own_name(self, tmp_path) -> None:
        # The alternative sorts BEFORE the package name on purpose: with a
        # first-by-sort-order fallback this case would pick the wrong binary, so
        # it is what proves the preference is doing the work.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        rec = await R.install(
            home, spec, npm=_fake_npm(tmp_path, bin_field={"aaa": "other.js", "foo": "cli.js"})
        )
        assert rec is not None
        assert rec.entrypoint.endswith("cli.js")

    async def test_failed_install_commits_nothing(self, tmp_path) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        assert (
            await R.install(home, spec, npm=_fake_npm(tmp_path, bin_field="cli.js", rc=1)) is None
        )
        # No record and no leftover tree: a launch must fall back cleanly.
        assert R.read_record(R.spec_dir(home, spec)) is None
        assert R.resolved_launch(home, "npx", ["-y", "foo@1.0.0"]) is None

    async def test_tree_with_nothing_runnable_commits_nothing(self, tmp_path) -> None:
        # Committing here would leave every later launch resolving to a missing
        # path and silently falling back forever.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        assert await R.install(home, spec, npm=_fake_npm(tmp_path, bin_field={})) is None
        assert R.read_record(R.spec_dir(home, spec)) is None

    async def test_nonzero_exit_blocks_the_commit_even_with_a_written_tree(self, tmp_path) -> None:
        # npm can fail after writing a partial tree. The return code, not the
        # presence of files, is what decides whether a resolution is trustworthy.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        npm = _fake_npm(tmp_path, bin_field="cli.js", rc=1, write_tree_anyway=True)
        assert await R.install(home, spec, npm=npm) is None
        assert R.read_record(R.spec_dir(home, spec)) is None

    async def test_traversing_bin_is_refused_at_install(self, tmp_path) -> None:
        # ``bin`` is third-party metadata. A record is a durable instruction to
        # exec a file, so one naming a path outside the store is never committed.
        # The escape target is created here rather than borrowed from the host, so
        # the refusal is what the assertion observes and not a missing file.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        spec_root = R.spec_dir(home, spec)
        os.makedirs(os.path.dirname(spec_root), exist_ok=True)
        escape = os.path.join(os.path.dirname(spec_root), "escape.js")
        with open(escape, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env node\n")
        # Relative to spec_root the entry point is r-<id>/node_modules/foo/<bin>,
        # so four levels up lands one directory ABOVE spec_root -- exactly where
        # the file above sits.
        npm = _fake_npm(tmp_path, bin_field="../../../../escape.js")
        assert os.path.isfile(escape)
        assert await R.install(home, spec, npm=npm) is None
        assert R.read_record(spec_root) is None

    async def test_missing_npm_is_not_an_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(R.shutil, "which", lambda _n: None)
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        assert await R.install(str(tmp_path), spec) is None

    async def test_noisy_install_output_is_drained_but_not_buffered(self, tmp_path) -> None:
        """A hostile or merely chatty package must not be able to OOM the gateway.

        ``communicate()`` retains everything the child writes, so unbounded
        third-party lifecycle output became unbounded gateway memory. The pipe is
        still drained to EOF -- a child whose stdout fills would otherwise block on
        write and never exit, turning noisy output into a hang -- but only the head
        is kept.
        """
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        # ~2 MiB of output, then a normal successful tree.
        noisy = tmp_path / "noisy-npm"
        inner = _fake_npm(tmp_path, bin_field="cli.js")
        noisy.write_text(
            "#!/bin/sh\n"
            "i=0\n"
            'while [ $i -lt 2048 ]; do printf "%01024d\\n" $i; i=$((i+1)); done\n'
            f'exec "{inner}" "$@"\n',
            encoding="utf-8",
        )
        noisy.chmod(0o755)
        rec = await R.install(home, spec, npm=str(noisy), timeout_secs=120)
        # It still succeeded -- draining means the child was never blocked.
        assert rec is not None
        assert rec.entrypoint.endswith("cli.js")

    async def test_the_retained_output_is_capped(self, tmp_path) -> None:
        """The cap is what bounds memory, so assert on what is actually RETAINED.

        A structural check that ``_drain_capped`` is called does not catch a cap
        removed inside it, and the behavioural install test passes either way
        because draining alone is enough to let the child exit. Only measuring the
        returned length pins the bound.
        """
        noisy = tmp_path / "noisy"
        noisy.write_text(
            "#!/bin/sh\n" 'i=0\nwhile [ $i -lt 64 ]; do printf "%01024d\\n" $i; i=$((i+1)); done\n',
            encoding="utf-8",
        )
        noisy.chmod(0o755)
        proc = await asyncio.create_subprocess_exec(
            str(noisy),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        kept = await R._drain_capped(proc)
        # The child wrote ~64 KiB; retention stays at the cap.
        assert len(kept) == R._MAX_INSTALL_OUTPUT_BYTES
        assert R._MAX_INSTALL_OUTPUT_BYTES <= 65536
        # And it was fully drained, so the child exited rather than blocking.
        assert proc.returncode == 0
        source = inspect.getsource(R.install)
        assert "_drain_capped(proc)" in source
        assert "communicate()" not in source

    async def test_a_timeout_reaps_the_whole_process_tree(self, tmp_path, monkeypatch) -> None:
        """npm is the parent of the work, not the work.

        An install runs third-party lifecycle scripts as grandchildren. Killing
        only npm on a timeout leaves those running with filesystem and network
        access and nobody waiting on them, which turns a bounded deadline into an
        unbounded background process.
        """
        killed: list[tuple[int, int]] = []
        from kiro_crew import platform_compat

        real_kill = platform_compat.kill_process_tree_async

        async def fake_kill_tree(pid, sig):
            # Record AND actually kill: a recording-only spy would leave the stub
            # running and make this test wait out its own sleep, which is the very
            # failure mode being tested.
            killed.append((pid, sig))
            return await real_kill(pid, sig)

        monkeypatch.setattr(platform_compat, "kill_process_tree_async", fake_kill_tree)
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        # A stub that never exits on its own, so the deadline is what ends it.
        sleeper = tmp_path / "sleeping-npm"
        sleeper.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
        sleeper.chmod(0o755)
        rec = await R.install(home, spec, npm=str(sleeper), timeout_secs=0.4)
        assert rec is None
        # The GROUP was signalled, not just the pid.
        assert killed and killed[0][1] == platform_compat.SIGKILL

    async def test_the_install_is_spawned_in_its_own_process_group(self) -> None:
        # Reaping the tree is only possible if the tree is a group of its own;
        # the two halves have to stay together.
        source = inspect.getsource(R.install)
        assert "start_new_session=True" in source

    async def test_cancellation_reaps_the_tree_too(self, tmp_path, monkeypatch) -> None:
        # Broker shutdown cancels the prefetch task. Without a cancel handler the
        # install and its descendants outlive the gateway that asked for them.
        source = inspect.getsource(R.install)
        assert "except asyncio.CancelledError:" in source
        assert source.index("except asyncio.CancelledError:") < source.index("raise")

    async def test_an_ambiguous_bin_table_is_a_miss(self, tmp_path) -> None:
        # npx ERRORS on a table with no name match rather than picking one, so
        # substituting any of them would run a program npx would not have run.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        rec = await R.install(
            home,
            spec,
            npm=_fake_npm(tmp_path, bin_field={"aaa": "a.js", "bbb": "b.js"}),
        )
        assert rec is None

    async def test_a_lone_bin_entry_is_unambiguous(self, tmp_path) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        rec = await R.install(home, spec, npm=_fake_npm(tmp_path, bin_field={"other": "cli.js"}))
        assert rec is not None
        assert rec.entrypoint.endswith("cli.js")

    async def test_no_bin_at_all_is_a_miss_even_with_main(self, tmp_path) -> None:
        # npx runs a package's BIN. Falling back to ``main`` would launch
        # something npx never launches.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        rec = await R.install(home, spec, npm=_fake_npm(tmp_path, bin_field=None, main="cli.js"))
        assert rec is None

    async def test_a_non_node_interpreter_is_a_miss(self, tmp_path) -> None:
        # The recorded launch is ``node <entrypoint>``; npx would exec through the
        # shebang. A ``sh`` bin therefore has to miss, or the hit is a crash.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        rec = await R.install(
            home, spec, npm=_fake_npm(tmp_path, bin_field="run", shebang="#!/bin/sh")
        )
        assert rec is None

    async def test_a_node_shebang_without_a_js_suffix_is_a_hit(self, tmp_path) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        rec = await R.install(
            home,
            spec,
            npm=_fake_npm(tmp_path, bin_field="run", shebang="#!/usr/bin/env node"),
        )
        assert rec is not None
        assert rec.entrypoint.endswith("run")

    async def test_second_install_leaves_the_previous_tree_inside_the_grace_window(
        self, tmp_path
    ) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@latest", passthrough=())
        npm = _fake_npm(tmp_path, bin_field="cli.js")
        first = await R.install(home, spec, npm=npm)
        assert first is not None
        # Same millisecond would collide on the resolution name.
        time.sleep(0.01)
        second = await R.install(home, spec, npm=npm)
        assert second is not None
        root = R.spec_dir(home, spec)
        trees = sorted(p for p in os.listdir(root) if p.startswith("r-"))
        live = os.path.dirname(os.path.dirname(os.path.dirname(second.entrypoint)))
        first_tree = os.path.dirname(os.path.dirname(os.path.dirname(first.entrypoint)))
        # BOTH survive: the superseded tree is inside its grace window. Deleting
        # it here is what would break a launch that had already read the old
        # record and was about to exec the path it named.
        assert trees == sorted([first_tree, live])
        assert os.path.isfile(os.path.join(root, first.entrypoint))

    async def test_a_superseded_tree_is_swept_once_past_the_grace_window(self, tmp_path) -> None:
        # The grace window is a disk-space concession, not a leak: the next sweep
        # collects whatever the previous one spared.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@latest", passthrough=())
        root = R.spec_dir(home, spec)
        os.makedirs(os.path.join(root, "r-old", "node_modules"), exist_ok=True)
        os.makedirs(os.path.join(root, "r-live"), exist_ok=True)
        old = os.path.join(root, "r-old")
        aged = time.time() - 7200.0
        os.utime(old, (aged, aged))
        R._sweep_old_resolutions(root, "r-live", grace_secs=3600.0)
        remaining = sorted(p for p in os.listdir(root) if p.startswith("r-"))
        assert remaining == ["r-live"]

    async def test_the_live_tree_is_never_swept_however_old(self, tmp_path) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@latest", passthrough=())
        root = R.spec_dir(home, spec)
        os.makedirs(os.path.join(root, "r-live"), exist_ok=True)
        aged = time.time() - 999999.0
        os.utime(os.path.join(root, "r-live"), (aged, aged))
        R._sweep_old_resolutions(root, "r-live", grace_secs=1.0)
        assert os.path.isdir(os.path.join(root, "r-live"))


@_posix_only
@pytest.mark.asyncio
class TestEnsureAndPrefetch:
    async def test_fresh_record_is_reused_without_installing(self, tmp_path) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        _commit_tree(home, spec)
        # Reuse without touching npm; the failing-npm proof is the next test.
        rec = await R.ensure_resolved(home, "npx", ["-y", "foo@1.0.0"])
        assert rec is not None

    async def test_force_reinstalls_even_a_pinned_record(self, tmp_path) -> None:
        # Pressing "update now" is the operator asking to go to the registry, not
        # asking whether it is time to -- so it must bypass the window that a
        # pinned record can never leave on its own.
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        _commit_tree(home, spec, entry_rel="r-old/node_modules/foo/cli.js")
        before = R.read_record(R.spec_dir(home, spec))
        assert before is not None and before.entrypoint.startswith("r-old/")

        npm = _fake_npm(tmp_path, bin_field="cli.js")
        after = await R.ensure_resolved(home, "npx", ["-y", "foo@1.0.0"], force=True, npm=npm)
        assert after is not None
        assert after.entrypoint != before.entrypoint
        assert R.read_record(R.spec_dir(home, spec)) == after

    async def test_without_force_a_pinned_record_is_not_reinstalled(self, tmp_path) -> None:
        home = str(tmp_path / "home")
        spec = R.NpmSpec(package="foo@1.0.0", passthrough=())
        _commit_tree(home, spec, entry_rel="r-old/node_modules/foo/cli.js")
        # An npm stub that always fails proves the install path was never entered.
        npm = _fake_npm(tmp_path, bin_field="cli.js", rc=1)
        rec = await R.ensure_resolved(home, "npx", ["-y", "foo@1.0.0"], npm=npm)
        assert rec is not None
        assert rec.entrypoint.startswith("r-old/")

    async def test_prefetch_reports_per_package_outcomes(self, tmp_path, monkeypatch) -> None:
        home = str(tmp_path / "home")
        monkeypatch.setattr(R.shutil, "which", lambda _n: None)  # npm absent
        outcomes = await R.prefetch(
            home,
            {
                "KIROCREW_MCP_TARGET_A": "npx -y foo@1.0.0",
                "KIROCREW_MCP_TARGET_B": "uvx not-npm",
            },
        )
        # The npm target is reported unresolved; the non-npm one is not reported
        # at all, because there is nothing to pre-resolve about a plain binary.
        assert outcomes == {"foo@1.0.0": "unresolved"}

    async def test_prefetch_survives_a_raising_target(self, tmp_path, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("registry on fire")

        monkeypatch.setattr(R, "install", boom)
        outcomes = await R.prefetch(str(tmp_path), {"KIROCREW_MCP_TARGET_A": "npx -y foo@latest"})
        assert outcomes == {"foo@latest": "error"}


# --- The launch-time guard on a target's own npm context ---------------------


class TestSubstitutionActuallyHappens:
    """The wrapper must substitute for an ORDINARY production target.

    This exists because an earlier revision of this PR added a guard that refused
    substitution whenever the target's env carried an npm-affecting key -- and the
    env handed to the resolver is ``_scrub_sensitive_env(dict(os.environ))``, a
    full inherited environment that ALWAYS contains ``PATH``. The guard therefore
    fired on every launch and silently disabled the whole feature, while the tests
    for it passed because they hand-built three-key env dicts. So the thing worth
    pinning is not a predicate in isolation but that a realistic env still gets the
    fast path.
    """

    def test_a_realistic_inherited_env_still_gets_the_store(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.mcp_gateway import gatewayd as G

        monkeypatch.setattr(G, "_resolve_once_home", lambda: str(tmp_path))
        monkeypatch.setattr(G, "resolved_launch", lambda *_a, **_k: ("node", ["/store/cli.js"]))
        # What the inner resolver really produces: the whole environment.
        realistic = dict(os.environ)
        realistic.setdefault("PATH", "/usr/bin:/bin")
        inner = ("npx", ["-y", "foo@1.0.0"], realistic, "/tmp")
        resolver = G.resolve_once_resolver(lambda _key: inner)
        command, args, env, work_dir = resolver(SimpleNamespace(server_name="s"))
        assert command == "node"
        assert args == ["/store/cli.js"]
        # Env and work_dir pass through untouched -- the PoolKey's env hash has to
        # keep describing what is actually spawned.
        assert env is realistic
        assert work_dir == "/tmp"

    def test_a_miss_passes_the_original_target_through(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.mcp_gateway import gatewayd as G

        monkeypatch.setattr(G, "_resolve_once_home", lambda: str(tmp_path))
        monkeypatch.setattr(G, "resolved_launch", lambda *_a, **_k: None)
        inner = ("npx", ["-y", "foo@1.0.0"], dict(os.environ), "/tmp")
        resolver = G.resolve_once_resolver(lambda _key: inner)
        assert resolver(SimpleNamespace(server_name="s")) == inner

    def test_a_raising_store_read_never_breaks_a_launch(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.mcp_gateway import gatewayd as G

        def boom(*_a, **_k):
            raise RuntimeError("store on fire")

        monkeypatch.setattr(G, "_resolve_once_home", lambda: str(tmp_path))
        monkeypatch.setattr(G, "resolved_launch", boom)
        inner = ("npx", ["-y", "foo@1.0.0"], dict(os.environ), "/tmp")
        resolver = G.resolve_once_resolver(lambda _key: inner)
        assert resolver(SimpleNamespace(server_name="s")) == inner


class _ReapProbe:
    """An install-tree child double that records how it is reaped.

    ``_reap_install_tree`` runs on the timeout and cancellation arms with the
    ``_drain_capped`` reader already cancelled, so the stdout pipe is undrained:
    a killed npm blocked writing into a full pipe -- or a lifecycle-script
    grandchild still holding it open -- makes a bare ``await proc.wait()`` hang
    forever (#6005). The bounded reap must drain via ``communicate()`` and never
    touch ``wait()``.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: "int | None" = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return -9


class TestReapInstallTree:
    @pytest.mark.asyncio
    async def test_reaps_via_communicate_not_wait(self, monkeypatch) -> None:
        """The reap must go through the bounded, pipe-draining ``kill_and_reap``,
        never a bare ``await proc.wait()`` that a full pipe can hang (#6005)."""
        from kiro_crew import platform_compat

        proc = _ReapProbe()
        tree_kills: "list[tuple[int, int]]" = []

        async def _fake_tree(pid, sig):
            tree_kills.append((pid, sig))
            return True

        # Fake pid + patched tree kill so no test can reach a real killpg.
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", _fake_tree)

        await R._reap_install_tree(proc)

        assert proc.communicate_calls == 1
        assert proc.wait_calls == 0
        assert tree_kills and tree_kills[0][0] == proc.pid
