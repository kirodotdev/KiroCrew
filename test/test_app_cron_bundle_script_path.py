"""An app cron's bundle script must resolve against the bundle, not the CWD.

``register_app_crons_with_service`` vetted an app cron's ``script`` through the
generic operator resolver with no app-bundle context. That resolver resolved a
relative path with ``Path(expanduser(part)).resolve()`` -- against the gateway
PROCESS's current working directory -- and then required the result to sit under
``<config_dir>/crons/``. Both halves are wrong for a script an app ships in its
own bundle under ``<config_dir>/apps/<app>/``, so no spelling an app author could
write in a manifest ever passed: a bare filename resolved to the CWD, a
bundle-relative path resolved to a CWD join, and an absolute bundle path tripped
the ``crons/`` containment check. The cron was re-vetted and denied on every
gateway start and every re-registration pass, and the phantom path in the denial
tracked whatever directory the gateway happened to start in.

The fix gives :func:`resolve_script_path` an optional ``app_root``: it becomes
the base a relative spec resolves against and narrows containment to that one
bundle. The bridge passes it and then PERSISTS the resolved absolute spec,
because every later consumer re-resolves ``job.script`` holding no app context.
A bundle root accepts ``.py`` files only -- a bundle also holds ``.app_secret``
(the app's gateway credential) and app data, and the cron script surface is
readable through the dashboard's script-source endpoint.

Containment, link and sensitivity behaviour is unchanged. The bundle roots are
an explicit opt-in on both keywords, so a caller passing neither -- ``cron_add``,
the CLI, both vault-grant sites -- keeps the operator contract byte for byte,
including its refusal of an absolute bundle path.

Must be runnable with ``--noconftest`` (no hypothesis dependency).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_escaping_link
from kiro_crew.cron_script import resolve_script_path

APP = "bundle-app"
CRON = f"{APP}/refresh"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated crew home with a ``crons/`` dir and one installed app bundle."""
    root = tmp_path / "crew-home"
    (root / "crons").mkdir(parents=True)
    bundle = root / "apps" / APP
    bundle.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    return root


def _bundle(home: Path, app: str = APP) -> Path:
    return home / "apps" / app


def _write_app_crons(home: Path, defs: list[dict], app: str = APP) -> None:
    (_bundle(home, app) / "app-crons.json").write_text(json.dumps(defs, indent=2))


def _register(app: str = APP):
    """Drive the bridge with a mocked CronSDK; returns (registered, add_kwargs)."""
    import kiro_crew.apps.execution as execution_mod
    from kiro_crew.apps.bridges import register_app_crons_with_service

    sdk = MagicMock()
    sdk.list_jobs.return_value = []
    sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="job-1"))
    with patch.object(execution_mod, "third_party_execution_allowed", lambda: True):
        with patch("kiro_crew.apps.bridges.CronSDK", return_value=sdk):
            registered = asyncio.run(register_app_crons_with_service(app, MagicMock()))
    kwargs = (
        sdk.add_job_if_absent_async.call_args.kwargs if sdk.add_job_if_absent_async.called else {}
    )
    return registered, kwargs


class TestBundleScriptRegistration:
    """The bug: a bundle script never registered, and the failure tracked the CWD."""

    @pytest.mark.parametrize("run_from", ["home", "elsewhere"])
    def test_a_bundle_script_registers_from_any_process_cwd(
        self, home, tmp_path, monkeypatch, run_from
    ):
        """The registration outcome must not depend on the gateway's CWD.

        Parameterised over two working directories on purpose: the reported
        symptom was three different phantom paths for ONE installed app across
        three launch contexts, so a single-CWD test could pass against a fix
        that merely moved which directory is guessed.
        """
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": "job.py:run"}])
        elsewhere = tmp_path / "some-other-cwd"
        elsewhere.mkdir()
        monkeypatch.chdir(home if run_from == "home" else elsewhere)

        registered, kwargs = _register()

        assert registered == [CRON]
        # Persisted absolute, not the manifest's relative spec: the fire-time
        # gate, the launcher and the source endpoint all re-resolve this string
        # with no app context, so a relative value would reintroduce the bug
        # after registration had already passed.
        assert kwargs["script"] == f"{_bundle(home) / 'job.py'}:run"

    def test_the_persisted_spec_re_resolves_for_a_downstream_consumer(self, home, monkeypatch):
        """What the bridge stores must satisfy the context-free resolver.

        This is the half a registration-only fix misses: vetting would pass and
        the job would be created, then the fire-time governance gate would
        re-resolve the same spec through the operator resolver and deny it.
        """
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": "job.py:run"}])
        monkeypatch.chdir(home)

        _registered, kwargs = _register()
        file_path, func = resolve_script_path(kwargs["script"], allow_bundle_roots=True)

        assert Path(file_path) == _bundle(home) / "job.py"
        assert func == "run"

    def test_a_missing_bundle_script_is_still_denied(self, home, monkeypatch):
        """The fix must not turn a genuinely absent script into a registration."""
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": "absent.py:run"}])
        monkeypatch.chdir(home)

        registered, kwargs = _register()

        assert registered == []
        assert kwargs == {}


class TestBundleRootContainment:
    """Widening the root set must not widen what a script may point at.

    A resolver-level case here asserts the ``app_root`` parameter's own
    containment, so on base it fails as an unknown keyword rather than on
    behaviour -- the parameter is what the fix adds. Each case is therefore
    paired with a BRIDGE-driven one that runs identically on both sides, so the
    guard is pinned by end-to-end behaviour and not by a signature.
    """

    def test_one_app_may_not_name_another_apps_script(self, home):
        """``app_root`` narrows containment to ONE bundle, not to "any bundle".

        The distinction only shows on a path that IS inside the shared bundle
        roots and is NOT inside the calling app's own tree, which is exactly the
        cross-app case: ``<config_dir>/apps/other-app/job.py`` passes any
        shared-root check and must still be refused for ``bundle-app``. Asserted
        on the ABSOLUTE spelling so the lexical ``..`` guard is not what refuses
        it, leaving canonical containment as the only thing under test.
        """
        other = home / "apps" / "other-app"
        other.mkdir(parents=True)
        victim = other / "job.py"
        victim.write_text("def run(ctx):\n    return 'x'\n")

        # Reachable for a persisted-spec consumer, which trusts the shared roots.
        assert Path(resolve_script_path(f"{victim}:run", allow_bundle_roots=True)[0]) == victim
        # Refused for this app, whose containment is its own bundle alone.
        with pytest.raises(PermissionError, match="must be under"):
            resolve_script_path(f"{victim}:run", app_root=_bundle(home))

    def test_the_bridge_refuses_another_apps_script(self, home, monkeypatch):
        """End-to-end: a manifest naming a sibling app's script is denied."""
        other = home / "apps" / "other-app"
        other.mkdir(parents=True)
        victim = other / "job.py"
        victim.write_text("def run(ctx):\n    return 'x'\n")
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": f"{victim}:run"}])
        monkeypatch.chdir(home)

        registered, kwargs = _register()

        assert registered == []
        assert kwargs == {}

    def test_the_bridge_refuses_a_spec_escaping_the_bundle(self, home, monkeypatch):
        """End-to-end: a manifest cannot reach out of its own bundle."""
        other = home / "apps" / "other-app"
        other.mkdir(parents=True)
        (other / "job.py").write_text("def run(ctx):\n    return 'x'\n")
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": "../other-app/job.py:run"}])
        monkeypatch.chdir(home)

        registered, kwargs = _register()

        assert registered == []
        assert kwargs == {}

    def test_a_non_py_bundle_file_is_refused(self, home):
        """``.app_secret`` is the app's gateway credential and lives in the bundle.

        ``is_sensitive_path`` does not cover it, and the dashboard's cron
        script-source endpoint renders a resolved script's bytes, so the suffix
        check is what keeps every non-module bundle file off that surface.
        """
        (_bundle(home) / ".app_secret").write_text("s3cr3t-app-token")

        with pytest.raises(PermissionError, match="must be a .py file"):
            resolve_script_path(".app_secret:run", app_root=_bundle(home))

    def test_the_bridge_refuses_a_non_py_bundle_file(self, home, monkeypatch):
        """End-to-end: a manifest naming the app's own credential file is denied."""
        (_bundle(home) / ".app_secret").write_text("s3cr3t-app-token")
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": ".app_secret:run"}])
        monkeypatch.chdir(home)

        registered, kwargs = _register()

        assert registered == []
        assert kwargs == {}

    def test_a_non_py_bundle_file_is_refused_for_a_persisted_spec_too(self, home):
        """The same refusal on the path a persisted-spec consumer re-resolves.

        The suffix rule guards the opted-in root as well, so a stored spec
        pointing at the credential file is refused at fire time, not only at
        registration.
        """
        secret = _bundle(home) / ".app_secret"
        secret.write_text("s3cr3t-app-token")

        with pytest.raises(PermissionError, match="must be a .py file"):
            resolve_script_path(f"{secret}:run", allow_bundle_roots=True)

    def test_an_absolute_spec_is_judged_by_containment_not_the_lexical_rule(self, home):
        """An absolute spec is not rebased, and its ``..`` is not refused lexically.

        The upward-traversal rule exists for a spec being joined onto a base: it
        asks whether the join escapes the bundle. An absolute path has no base to
        escape from, so ``..`` inside one is ordinary path syntax that
        ``resolve()`` collapses, and containment is the right judge of where it
        lands. Written with a ``..`` segment on purpose: for a plain absolute
        path, pathlib's own absolute-replacement makes rebasing a no-op, so that
        spelling could not tell the two behaviours apart.
        """
        nested = _bundle(home) / "sub"
        nested.mkdir()
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        spec = f"{nested}/../job.py:run"

        file_path, func = resolve_script_path(spec, app_root=_bundle(home))

        assert Path(file_path) == _bundle(home) / "job.py"
        assert func == "run"

    def test_an_absolute_spec_leaving_the_bundle_is_still_refused(self, home, tmp_path):
        """And the passthrough is not an escape hatch: containment still decides."""
        outside = tmp_path / "outside.py"
        outside.write_text("def run(ctx):\n    return 'x'\n")

        with pytest.raises(PermissionError, match="must be under"):
            resolve_script_path(f"{outside}:run", app_root=_bundle(home))

    @pytest.mark.parametrize("spelling", ["dotdot", "link"])
    def test_a_spec_leaving_the_bundle_is_refused(self, home, tmp_path, spelling):
        """Neither spelling of an escape gets out of the bundle.

        ``dotdot`` is refused lexically before any join, so the verdict does not
        depend on the host's path grammar. ``link`` is refused on its RESOLVED
        target, which is what no lexical check can see. Both run on Windows too:
        ``make_escaping_link`` uses a directory junction there, which needs no
        privilege and travels the same reparse machinery a symlink does, so the
        containment assertion stays exercised instead of skipped.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("def run(ctx):\n    return 'x'\n")
        if spelling == "dotdot":
            rel = f"../../{outside.name}/secret.py"
        else:
            rel = make_escaping_link(_bundle(home), outside)

        with pytest.raises(PermissionError):
            resolve_script_path(f"{rel}:run", app_root=_bundle(home))

    def test_the_bridge_refuses_a_bundle_link_leaving_the_root(self, home, tmp_path, monkeypatch):
        """End-to-end: a linked bundle script is judged on its target."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("def run(ctx):\n    return 'x'\n")
        rel = make_escaping_link(_bundle(home), outside)
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": f"{rel}:run"}])
        monkeypatch.chdir(home)

        registered, kwargs = _register()

        assert registered == []
        assert kwargs == {}

    def test_a_path_under_no_trusted_root_is_refused(self, home, tmp_path):
        stray = tmp_path / "stray.py"
        stray.write_text("def run(ctx):\n    return 'x'\n")

        with pytest.raises(PermissionError, match="must be under"):
            resolve_script_path(f"{stray}:run", allow_bundle_roots=True)


class TestAuthoringPathsGainNothing:
    """``allow_bundle_roots`` must open the bundle roots to NOTHING else.

    The roots are an explicit opt-in, taken only by the three consumers that
    re-resolve a spec already vetted and persisted. Every other caller, including
    agent-callable ``cron_add`` and the CLI, stays confined to ``crons/``: these
    pin that boundary from the outside, by making the call those callers make.
    """

    def test_a_bundle_path_is_refused_with_neither_keyword(self, home, tmp_path, monkeypatch):
        """The default is the operator contract: ``crons/`` only.

        This is the exact call shape ``cron_add`` (``mcp_cron.py:2445``), the CLI
        (``cli_commands.py:1699``) and both vault-grant sites make.
        """
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(PermissionError, match="must be under"):
            resolve_script_path(f"{_bundle(home) / 'job.py'}:run")

    def test_the_same_path_resolves_for_a_persisted_spec_consumer(self, home, monkeypatch):
        """And the opt-in is what separates the two, not the path."""
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        spec = f"{_bundle(home) / 'job.py'}:run"

        file_path, _func = resolve_script_path(spec, allow_bundle_roots=True)

        assert Path(file_path) == _bundle(home) / "job.py"

    def test_cron_add_refuses_a_bundle_script(self, home, tmp_path, monkeypatch):
        """End-to-end through the agent-facing MCP tool's own vetting."""
        from kiro_crew.mcp_cron import _vet_script_file

        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        monkeypatch.chdir(tmp_path)

        # The cron_add handler resolves first and returns "Error: <exc>" on a
        # raise, so the refusal happens before the body scan is ever reached.
        with pytest.raises(PermissionError, match="must be under"):
            path, _ = resolve_script_path(f"{_bundle(home) / 'job.py'}:run")
            _vet_script_file(path)


class TestCronSdkDirectEntryPoint:
    """``CronSDK.add_job`` is the SECOND way a bundle script reaches vetting.

    ``bridges`` is the gateway-startup registrar; an app's own backend creates a
    job by calling the SDK directly, and it names its script the only way it can
    -- relative to its own bundle. That call site resolved through the same
    context-free resolver, so it carried the same CWD dependence.
    """

    def _sdk(self, app: str = APP):
        from kiro_crew.apps.cron_sdk import CronSDK

        service = MagicMock()
        service.add_job = MagicMock(return_value=MagicMock(id="job-2", name=CRON))
        return CronSDK(app, service), service

    def test_a_relative_bundle_script_resolves_against_the_bundle(
        self, home, tmp_path, monkeypatch
    ):
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        elsewhere = tmp_path / "foreign-cwd"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        sdk, service = self._sdk()

        sdk.add_job(CRON, "go", every_secs=600, script="job.py:run")

        # Persisted absolute, for the same reason the bridge does it: the
        # fire-time gate and the launcher re-resolve this string context-free.
        assert service.add_job.call_args.kwargs["script"] == f"{_bundle(home) / 'job.py'}:run"

    def test_a_relative_spec_naming_no_bundle_file_is_refused(self, home, tmp_path, monkeypatch):
        """A bundle base must not become a way to smuggle in a CWD file.

        ``absent.py`` exists in the process CWD but NOT in the bundle, so the
        bundle base is what makes this a refusal; resolving against the CWD
        would accept it.
        """
        elsewhere = tmp_path / "foreign-cwd"
        elsewhere.mkdir()
        (elsewhere / "absent.py").write_text("def run(ctx):\n    return 'x'\n")
        monkeypatch.chdir(elsewhere)
        sdk, service = self._sdk()

        with pytest.raises(ValueError, match="cron script rejected"):
            sdk.add_job(CRON, "go", every_secs=600, script="absent.py:run")
        assert not service.add_job.called


class TestDownstreamReResolution:
    """Every consumer that re-resolves ``job.script`` must reach the same verdict.

    Registration is only half the path. The spec the bridge stores is re-resolved
    later by callers that hold no app context at all, so a fix that only taught
    the REGISTRAR about bundles would pass vetting and then have the job denied
    at fire time -- the same silent nothing, one stage further on.
    """

    def _persisted(self, home: Path) -> str:
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        return f"{_bundle(home) / 'job.py'}:run"

    def test_the_fire_time_governance_gate_admits_a_bundle_script(
        self, home, tmp_path, monkeypatch
    ):
        """``mcp_cron`` re-resolves the spec on EVERY fire before allowing the run."""
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        monkeypatch.chdir(tmp_path)  # a CWD holding none of these files
        job = MagicMock()
        job.id = "job-3"
        job.command = ""
        job.script = self._persisted(home)

        assert vet_job_at_fire_time(job) is None

    def test_the_launcher_resolves_a_bundle_script_to_its_bundle(self, home, tmp_path, monkeypatch):
        """``run_script_sandboxed`` opens with exactly this resolution."""
        monkeypatch.chdir(tmp_path)
        spec = self._persisted(home)

        file_path, func = resolve_script_path(spec, allow_bundle_roots=True)

        assert Path(file_path) == _bundle(home) / "job.py"
        assert func == "run"

    def test_the_source_endpoint_refuses_a_bundle_script_without_a_500(
        self, home, tmp_path, monkeypatch
    ):
        """Resolution now succeeds; the READ stays pinned to ``crons/`` on purpose.

        The dashboard's script-source read goes through
        ``safe_read_file_bytes_nolink`` pinned to the crons root, and that pin is
        deliberately NOT widened here -- rendering bundle bytes is a separate
        decision from letting the cron run. What this pins is the endpoint's own
        invariant: no persisted job state may produce a 500, so the outcome is a
        typed 4xx refusal rather than an exception.
        """
        from kiro_crew.dashboard.handlers.cron import _read_script_source_sync

        monkeypatch.chdir(tmp_path)

        payload, refusal = _read_script_source_sync(self._persisted(home))

        assert payload is None
        assert refusal == ("script unreadable", "script_read_refused")


class TestTheAppRootIsNotCallerControlled:
    """An app may not choose the root its own script is confined to.

    ``ctx.cron`` hands a ``CronSDK`` to every app holding the ``cron``
    permission, and the resolved root is what the script is confined to before it
    is persisted for the launcher to EXECUTE. A root taken from a method keyword
    would let one app name a root of its choosing and get a ``.py`` under it
    executed, which is exactly the cross-bundle bypass the root exists to
    prevent. These pin that no mutator accepts such a keyword and that the value
    is derived from the SDK's own app name.
    """

    def _sdk(self):
        from kiro_crew.apps.cron_sdk import CronSDK

        service = MagicMock()
        service.add_job_async = AsyncMock(return_value=MagicMock(id="j", name=CRON))
        service.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="j", name=CRON))
        return CronSDK(APP, service), service

    @pytest.mark.parametrize("method", ["add_job", "add_job_async", "add_job_if_absent_async"])
    def test_no_mutator_accepts_an_app_root(self, home, method):
        """Asserted on the signature, because absence is the whole point."""
        import inspect

        from kiro_crew.apps.cron_sdk import CronSDK

        params = inspect.signature(getattr(CronSDK, method)).parameters

        assert "app_root" not in params

    def test_an_app_cannot_confine_its_script_to_a_root_it_chose(self, home, tmp_path):
        """The bypass attempt, driven end to end through the app-facing SDK.

        A ``.py`` in a directory of the app's choosing must not become an
        executable cron, even though it would satisfy containment against that
        directory.
        """
        attacker = tmp_path / "anywhere"
        attacker.mkdir()
        (attacker / "payload.py").write_text("def run(ctx):\n    return 'pwned'\n")
        sdk, service = self._sdk()

        with pytest.raises(TypeError):
            asyncio.run(
                sdk.add_job_if_absent_async(
                    CRON,
                    "go",
                    every_secs=600,
                    script=f"{attacker / 'payload.py'}:run",
                    app_root=attacker,
                )
            )
        assert not service.add_job_if_absent_async.called

    def test_the_same_absolute_payload_is_refused_without_the_keyword(self, home, tmp_path):
        """And removing the keyword is not a loophole: the path is refused anyway."""
        attacker = tmp_path / "anywhere"
        attacker.mkdir()
        (attacker / "payload.py").write_text("def run(ctx):\n    return 'pwned'\n")
        sdk, service = self._sdk()

        with pytest.raises(ValueError, match="cron script rejected"):
            asyncio.run(
                sdk.add_job_if_absent_async(
                    CRON, "go", every_secs=600, script=f"{attacker / 'payload.py'}:run"
                )
            )
        assert not service.add_job_if_absent_async.called


class TestTheAsyncPathDoesNotStallTheLoop:
    """The bundle lookup and the body scan walk/read the filesystem.

    ``shipped_builtin_app_root`` does an ``iterdir`` over every builtin manifest
    source and then ``resolve`` + ``read_text`` + ``json.loads`` per entry, and
    the body scan reads up to ``_MAX_SCRIPT_SCAN_BYTES`` (256 KiB).
    ``bridges.register_app_crons_with_service`` awaits its work directly on the
    gateway event loop -- at app enable and at every gateway start -- so reaching
    either inline would park every request and the heartbeat for its duration.
    """

    def test_the_bridge_offloads_its_resolve_and_body_scan(self, home, monkeypatch):
        """Asserted by thread identity, which no source reading can fake."""
        import threading

        import kiro_crew.apps.bridges as bridges_mod

        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        _write_app_crons(home, [{"name": CRON, "every": 600, "script": "job.py:run"}])
        monkeypatch.chdir(home)

        seen: dict[str, int] = {}
        real = bridges_mod._resolve_and_vet_app_script

        def _record(*a, **kw):
            seen["thread"] = threading.get_ident()
            return real(*a, **kw)

        monkeypatch.setattr(bridges_mod, "_resolve_and_vet_app_script", _record)

        registered, _kwargs = _register()

        assert registered == [CRON]
        assert seen["thread"] != threading.get_ident()

    def test_the_bundle_walk_happens_once_per_sdk_not_once_per_job(self, home):
        """Memoised per instance, so a registrar looping over jobs pays it once."""
        import kiro_crew.apps.execution as execution_mod
        from kiro_crew.apps.cron_sdk import CronSDK

        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        service = MagicMock()
        service.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="j", name=CRON))
        sdk = CronSDK(APP, service)

        walks: list[str] = []
        real = execution_mod.shipped_builtin_app_root
        execution_mod.shipped_builtin_app_root = lambda n: (walks.append(n), real(n))[1]
        try:
            for i in range(3):
                asyncio.run(
                    sdk.add_job_if_absent_async(
                        f"{APP}/j{i}", "go", every_secs=600, script="job.py:run"
                    )
                )
        finally:
            execution_mod.shipped_builtin_app_root = real

        assert len(walks) == 1

    def test_the_async_mutator_runs_the_vet_off_the_loop(self, home):
        """Asserted by thread identity, which no source reading can fake.

        The vet is what touches the filesystem, so the question is which thread
        executes it, not whether a particular helper was called.
        """
        import threading

        from kiro_crew.apps.cron_sdk import CronSDK

        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'ok'\n")
        service = MagicMock()
        service.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="j", name=CRON))
        sdk = CronSDK(APP, service)

        seen: dict[str, int] = {}
        real_vet = sdk._vet_command_script

        def _record(*a, **kw):
            seen["vet"] = threading.get_ident()
            return real_vet(*a, **kw)

        sdk._vet_command_script = _record  # type: ignore[method-assign]

        async def _drive() -> int:
            await sdk.add_job_if_absent_async(CRON, "go", every_secs=600, script="job.py:run")
            return threading.get_ident()

        loop_thread = asyncio.run(_drive())

        assert seen["vet"] != loop_thread


class TestEditionBundleRootsAreOneAuthority:
    """Registration and fire time must agree on which trees hold a builtin bundle.

    ``shipped_builtin_app_root`` chooses a builtin's root by walking
    ``_builtin_manifest_sources``, which includes the active edition's
    ``apps_loader.manifest_sources()`` and so can point anywhere. The
    context-free root set reads that same authority, so a root good enough to
    register a cron is good enough to fire it.
    """

    def test_an_edition_manifest_source_is_trusted_at_fire_time(self, home, tmp_path, monkeypatch):
        import kiro_crew.apps.execution as execution_mod
        from kiro_crew.cron_script import _trusted_script_bundle_roots

        edition = tmp_path / "edition-builtins"
        (edition / "edition-app").mkdir(parents=True)
        script = edition / "edition-app" / "job.py"
        script.write_text("def run(ctx):\n    return 'ok'\n")
        monkeypatch.setattr(
            execution_mod, "_builtin_manifest_sources", lambda: (edition.resolve(),)
        )
        monkeypatch.chdir(tmp_path)

        assert edition.resolve() in _trusted_script_bundle_roots()
        # The persisted spec a registrar stores must also resolve for the
        # context-free consumers, which is the other end of the same agreement.
        file_path, func = resolve_script_path(f"{script}:run", allow_bundle_roots=True)
        assert Path(file_path) == script
        assert func == "run"

    def test_this_package_stays_trusted_when_the_seam_is_unavailable(
        self, home, tmp_path, monkeypatch
    ):
        """The fallback leg: a composition without the platform seam still works."""
        import kiro_crew.apps.execution as execution_mod
        import kiro_crew.cron_script as cron_script_mod
        from kiro_crew.cron_script import _trusted_script_bundle_roots

        def _boom():
            raise RuntimeError("platform seam not composed")

        monkeypatch.setattr(execution_mod, "_builtin_manifest_sources", _boom)

        roots = _trusted_script_bundle_roots()

        assert Path(cron_script_mod.__file__).parent.resolve() in roots


class TestAppSecretsAreHiddenFromScriptCrons:
    """A cron script child must not be able to read any app's bearer credential.

    ``<config_dir>/apps/<app>/.app_secret`` is not a marker:
    ``token_auth.validate_app_secret`` compares it and issues that app's scoped
    token, so a subprocess that reads the file can act as the app. The sandbox's
    crew-home mask set does not cover these files and the ``cc`` profile a cron
    script runs under leaves them readable, so the launcher masks them per spawn.
    """

    def _installed(self, home: Path, *names: str) -> list[Path]:
        secrets = []
        for n in names:
            d = home / "apps" / n
            d.mkdir(parents=True, exist_ok=True)
            s = d / ".app_secret"
            s.write_text(f"secret-for-{n}")
            secrets.append(s)
        return secrets

    @pytest.mark.parametrize("granted", [False, True])
    def test_every_app_secret_is_masked_on_the_spawn(self, home, monkeypatch, granted):
        """Asserted on the argument ``wrap_argv`` actually receives.

        Parameterised over granted and ungranted because the two compose what
        they hide differently, and a mask covering only one of them would leave
        the other's child able to read every app's credential.

        The stub raises ``SandboxUnavailableError`` once it has captured the
        argument, so the test ends at the spawn boundary and needs no fake
        process: what is under test is which paths the sandbox is told to hide.
        """
        import kiro_crew.cron_script as cs
        from kiro_crew.sandbox import SandboxUnavailableError

        secrets = self._installed(home, APP, "other-app", "third-app")
        script = home / "crons" / "job.py"
        script.write_text("def run(ctx):\n    return {'status': 'ok'}\n")

        seen: dict[str, tuple] = {}

        def _wrap(argv, mode="", extra_hidden_dirs=(), **kw):
            seen["hidden"] = tuple(extra_hidden_dirs)
            seen["mode"] = mode
            raise SandboxUnavailableError("captured", "test", "spawn argument captured")

        monkeypatch.setattr(cs, "wrap_argv", _wrap)
        monkeypatch.setattr(cs, "resolve_script_path", lambda spec, **_kw: (str(script), "run"))
        if granted:
            # The grant pin is not the subject; skip straight to the spawn.
            monkeypatch.setattr(cs, "_secret_env_precheck", lambda *a, **k: ({"T": "v"}, None))

        cs.run_script_sandboxed(
            f"{script}:run",
            "job-mask",
            "msg",
            secret_env={"T": "vault-name"} if granted else None,
            secret_env_pin="pin" if granted else "",
        )

        assert seen["mode"] == ("strict" if granted else "cc")
        for s in secrets:
            assert str(s) in seen["hidden"], f"{s} not masked (granted={granted})"

    def test_the_bundle_tree_itself_stays_visible(self, home, monkeypatch):
        """Masking leaves, not the tree: a bundle cron must still import its script."""
        import kiro_crew.cron_script as cs
        from kiro_crew.sandbox import SandboxUnavailableError

        self._installed(home, APP)
        script = _bundle(home) / "job.py"
        script.write_text("def run(ctx):\n    return {'status': 'ok'}\n")

        seen: dict[str, tuple] = {}

        def _wrap(argv, mode="", extra_hidden_dirs=(), **kw):
            seen["hidden"] = tuple(extra_hidden_dirs)
            raise SandboxUnavailableError("captured", "test", "spawn argument captured")

        monkeypatch.setattr(cs, "wrap_argv", _wrap)
        monkeypatch.setattr(cs, "resolve_script_path", lambda spec, **_kw: (str(script), "run"))

        cs.run_script_sandboxed(f"{script}:run", "job-vis", "msg")

        assert str(_bundle(home)) not in seen["hidden"]
        assert str(home / "apps") not in seen["hidden"]
        assert str(_bundle(home) / ".app_secret") in seen["hidden"]


class TestOperatorContractUnchanged:
    """No ``app_root`` must mean exactly today's behaviour."""

    def test_a_relative_spec_with_no_app_root_still_resolves_against_cwd(
        self, home, tmp_path, monkeypatch
    ):
        """The documented ``cron_add`` contract: a relative path is CWD-relative.

        Asserted as a MISS against a file that exists in the bundle: if the
        resolver had silently adopted a bundle base for context-free callers,
        this would resolve instead of raising.
        """
        (_bundle(home) / "job.py").write_text("def run(ctx):\n    return 'x'\n")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError):
            resolve_script_path("job.py:run")

    def test_an_operator_script_under_crons_still_resolves(self, home):
        script = home / "crons" / "operator.py"
        script.write_text("def run(ctx):\n    return 'x'\n")

        file_path, func = resolve_script_path(f"{script}:run")

        assert Path(file_path) == script
        assert func == "run"

    def test_a_non_py_file_under_crons_is_still_accepted(self, home):
        """The suffix rule belongs to bundle roots only; ``crons/`` is unchanged."""
        script = home / "crons" / "legacy_job"
        script.write_text("def run(ctx):\n    return 'x'\n")

        file_path, _func = resolve_script_path(f"{script}:run")

        assert Path(file_path) == script
