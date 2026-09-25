"""The CPP ``remote_provisioners`` seam: descriptor, Default adapter, composition.

The HTTP half (listing, ``provider_id`` on a launch, engine routing) lives in
``test_cloud_handlers.py::TestProvisionerSeam``; the job-side half in
``test_cloud_launch_job.py::TestProvisionerOnTheJob``. This file pins the
contract objects themselves and that the default context composes the seam.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from kiro_crew.cloud.launch_engine import RealLaunchEngine
from kiro_crew.platform.defaults import (
    BUILTIN_REMOTE_PROVISIONER,
    FARGATE_PROVISIONER_ID,
    FARGATE_REMOTE_PROVISIONER,
    DefaultRemoteProvisionerProvider,
)
from kiro_crew.platform.interfaces import (
    BUILTIN_PROVISIONER_ID,
    RemoteProvisioner,
    RemoteProvisionerProvider,
)


class TestFargateLane:
    """The second lane the core ships, offered only when ``cloud.json`` names it.

    Written against the PROVIDER rather than the HTTP surface, because the property
    is which engines exist -- the listing and routing halves are pinned in
    ``test_cloud_handlers.py::TestProvisionerSeam``.
    """

    @staticmethod
    def _write_config(home, block) -> None:
        import json

        path = home / "cloud.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"profile": "p", "region": "us-east-1"}
        if block is not None:
            payload["fargate"] = block
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _complete_block() -> dict:
        # The credential secret is a conforming pair: the canonical name
        # kirocrew/crew/<crew>/<ENV> and the ARN that is that name plus one
        # six-character service suffix. The account id is fictional. Without a
        # secret named for the model credential the block is INCOMPLETE, because
        # the engine would refuse every launch made through the lane.
        return {
            "cluster": "kirocrew-crew-prod",
            "subnets": ["subnet-a"],
            "security_groups": ["sg-1"],
            "image": "public.ecr.aws/example/kirocrew-crew-base@sha256:" + "b" * 64,
            "secrets": [
                [
                    "kirocrew/crew/demo/KIRO_IDENTITY",
                    "arn:aws:secretsmanager:us-east-1:123456789012:"
                    "secret:kirocrew/crew/demo/KIRO_IDENTITY-AbCdEf",
                ]
            ],
            "cpu_architecture": "X86_64",
        }

    def test_unconfigured_offers_only_ec2_and_refuses_the_fargate_engine(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        provider = DefaultRemoteProvisionerProvider()
        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]
        with pytest.raises(KeyError):
            provider.engine_for(FARGATE_PROVISIONER_ID)

    def test_configured_offers_the_lane_and_builds_the_engine_from_the_block(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        block = self._complete_block()
        self._write_config(config_dir(), block)

        provider = DefaultRemoteProvisionerProvider()
        assert [p.id for p in provider.provisioners()] == [
            BUILTIN_PROVISIONER_ID,
            FARGATE_PROVISIONER_ID,
        ]
        engine = provider.engine_for(FARGATE_PROVISIONER_ID)
        # The spec's fields come from the block, not from a default -- the engine
        # refuses to guess any of them, so a wrong value here is a wrong launch.
        spec = engine._require_spec()
        assert spec.placement.cluster == block["cluster"]
        assert spec.placement.subnets == tuple(block["subnets"])
        assert spec.placement.security_groups == tuple(block["security_groups"])
        assert spec.image == block["image"]
        assert spec.cpu_architecture == block["cpu_architecture"]

    def test_the_configured_bound_reaches_the_engine(self, monkeypatch, tmp_path):
        """The wiring, which is the whole point: a number in the file bounds a launch.

        Asserted on the engine the provider BUILT, not on the config object, because a
        config object holding the right number proves nothing about a launch: read
        ``FargateConfig`` here instead and the test passes with the wiring deleted. The
        engine's own ``bounds`` is the only place the file's number can change what a
        task gets.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        block = {**self._complete_block(), "task_ttl_seconds": 43200}
        self._write_config(config_dir(), block)

        engine = DefaultRemoteProvisionerProvider().engine_for(FARGATE_PROVISIONER_ID)
        assert engine._bounds.ttl_seconds == 43200
        # The cap must NOT move because the file set a lifetime: it has no key, so the
        # engine's own value has to survive an operator override of its sibling.
        from kiro_crew.cloud.fargate_engine import DEFAULT_MAX_RUNNING_TASKS

        assert engine._bounds.max_running == DEFAULT_MAX_RUNNING_TASKS

    def test_an_omitted_bound_leaves_the_engine_on_its_own_default(self, monkeypatch, tmp_path):
        """The lane is unchanged for every operator who does not set the key.

        Read from the engine's own constants rather than written out, so it cannot drift
        from them and cannot pass by coincidence.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.cloud.fargate_engine import (
            DEFAULT_MAX_RUNNING_TASKS,
            DEFAULT_TASK_TTL_SECONDS,
        )
        from kiro_crew.config.loader import config_dir

        self._write_config(config_dir(), self._complete_block())

        engine = DefaultRemoteProvisionerProvider().engine_for(FARGATE_PROVISIONER_ID)
        assert engine._bounds.ttl_seconds == DEFAULT_TASK_TTL_SECONDS
        assert engine._bounds.max_running == DEFAULT_MAX_RUNNING_TASKS

    def test_a_bound_the_engine_refuses_leaves_the_lane_unoffered(self, monkeypatch, tmp_path):
        """A lifetime of zero is the offered-and-refusing state, so it voids the block.

        The alternative is a lane that lists in the selector and then raises out of
        ``TaskBounds`` on its first launch, which is the state this module's
        "incomplete means absent" rule exists to prevent.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        self._write_config(config_dir(), {**self._complete_block(), "task_ttl_seconds": 0})

        provider = DefaultRemoteProvisionerProvider()
        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]
        with pytest.raises(KeyError):
            provider.engine_for(FARGATE_PROVISIONER_ID)

    def test_the_ec2_lane_is_unchanged_either_way(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        provider = DefaultRemoteProvisionerProvider()
        assert isinstance(provider.engine_for(BUILTIN_PROVISIONER_ID), RealLaunchEngine)
        self._write_config(config_dir(), self._complete_block())
        assert isinstance(provider.engine_for(BUILTIN_PROVISIONER_ID), RealLaunchEngine)

    def test_an_incomplete_block_leaves_the_lane_unoffered(self, monkeypatch, tmp_path):
        """Offered-and-refusing is the state this avoids."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        self._write_config(config_dir(), {"cluster": "only-a-cluster"})
        provider = DefaultRemoteProvisionerProvider()
        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]
        with pytest.raises(KeyError):
            provider.engine_for(FARGATE_PROVISIONER_ID)

    def test_a_block_missing_only_the_credential_secret_leaves_the_lane_unoffered(
        self, monkeypatch, tmp_path
    ):
        """The most likely way to reach offered-and-refusing, so pin it at the lane.

        Every placement field is present and only the model-credential secret is
        gone. The engine would build a task definition and refuse it, so this block
        must leave the lane unregistered rather than registered and rejecting. The
        config-level boundary asserts the same thing; without this case a change
        that drops the requirement passes the seam suite untouched.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        self._write_config(config_dir(), {**self._complete_block(), "secrets": []})
        provider = DefaultRemoteProvisionerProvider()
        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]
        with pytest.raises(KeyError):
            provider.engine_for(FARGATE_PROVISIONER_ID)

    def test_a_config_read_that_raises_does_not_take_the_selector_down(self, monkeypatch):
        """One lane's misconfiguration must not hide the other lane.

        ``provisioners()`` builds the Set-up list, so raising here would render the
        tab empty -- turning a Fargate problem into no lanes at all.
        """
        import kiro_crew.cloud.config as cloud_config

        def boom(*_args, **_kwargs):
            raise OSError("unreadable")

        monkeypatch.setattr(cloud_config.CloudConfig, "load", classmethod(boom))
        provider = DefaultRemoteProvisionerProvider()
        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]

    def test_the_block_is_read_per_call_not_cached(self, monkeypatch, tmp_path):
        """An operator who edits the file gets the answer from the next request."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        provider = DefaultRemoteProvisionerProvider()
        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]
        self._write_config(config_dir(), self._complete_block())
        assert FARGATE_PROVISIONER_ID in [p.id for p in provider.provisioners()]
        (config_dir() / "cloud.json").unlink()
        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]

    def test_the_descriptor_names_its_own_kind_so_the_dashboard_skips_it(self):
        """``kind`` equals the id, deliberately.

        The dashboard draws ``aws_ec2`` with the core's own form and SKIPS a kind it
        has no renderer for. Naming a kind of its own keeps this lane absent from
        the selector until a renderer exists, rather than handing the EC2 form a
        lane that takes no instance type.
        """
        assert FARGATE_REMOTE_PROVISIONER.id == FARGATE_PROVISIONER_ID
        assert FARGATE_REMOTE_PROVISIONER.kind == FARGATE_PROVISIONER_ID
        assert FARGATE_REMOTE_PROVISIONER.kind != BUILTIN_PROVISIONER_ID

    def test_importing_the_platform_does_not_pull_the_cloud_config_module(self):
        """The deferral is load-bearing twice over, so it is pinned.

        ``kiro_crew.cloud`` reaches ``kiro_crew.sandbox``, which imports
        ``kiro_crew.platform.current_context`` -- a module-level import here raises
        ``ImportError: cannot import name 'current_context' from partially
        initialized module``, because this module loads during ``platform`` init.
        It is also 105 ms and 122 modules against a 126 ms init, for a lane most
        deployments have not configured.
        """
        import os
        import subprocess
        import sys
        from pathlib import Path

        from kiro_crew.subprocess_utf8 import UTF8_TEXT

        probe = (
            "import sys, kiro_crew.platform.defaults as d;"
            "print('cloud.config' if 'kiro_crew.cloud.config' in sys.modules else 'deferred')"
        )
        # Run from the repository root with src/ on the path, so the child imports
        # this checkout rather than whatever is installed.
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "src"))
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            cwd=root,
            env=env,
            timeout=120,
            **UTF8_TEXT,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "deferred", result.stdout


class TestDescriptor:
    def test_builtin_descriptor_is_the_ec2_lane(self):
        assert BUILTIN_PROVISIONER_ID == "aws_ec2"
        assert BUILTIN_REMOTE_PROVISIONER.id == BUILTIN_PROVISIONER_ID
        # id and kind coincide for the built-in: the core's own form draws it.
        assert BUILTIN_REMOTE_PROVISIONER.kind == BUILTIN_PROVISIONER_ID
        assert BUILTIN_REMOTE_PROVISIONER.posix_only is True
        assert BUILTIN_REMOTE_PROVISIONER.step_labels == ()

    def test_descriptor_is_frozen_and_hashable(self):
        p = RemoteProvisioner(id="x", kind="k", label="X")
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.id = "y"  # type: ignore[misc]
        assert hash(p)  # step_labels is a tuple, not a dict, so this holds

    def test_step_labels_are_key_value_pairs(self):
        p = RemoteProvisioner(
            id="devspace",
            kind="amazon_devspace",
            label="Amazon DevSpace",
            posix_only=False,
            step_labels=(("provision", "Create the DevSpace"),),
        )
        assert dict(p.step_labels) == {"provision": "Create the DevSpace"}


class TestDefaultProvider:
    def test_lists_exactly_the_builtin(self):
        rows = DefaultRemoteProvisionerProvider().provisioners()
        assert rows == [BUILTIN_REMOTE_PROVISIONER]

    def test_engine_for_the_builtin_is_the_ec2_engine(self):
        eng = DefaultRemoteProvisionerProvider().engine_for(BUILTIN_PROVISIONER_ID)
        assert isinstance(eng, RealLaunchEngine)

    def test_engine_for_anything_else_is_a_key_error(self):
        with pytest.raises(KeyError):
            DefaultRemoteProvisionerProvider().engine_for("devspace")

    def test_satisfies_the_protocol_shape(self):
        p: RemoteProvisionerProvider = DefaultRemoteProvisionerProvider()
        assert callable(p.provisioners) and callable(p.engine_for)


class TestComposition:
    def test_default_context_composes_the_seam(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context

        ctx = build_default_context(KiroCrewConfig.load(), profile="standalone")
        assert isinstance(ctx.remote_provisioners, DefaultRemoteProvisionerProvider)

    def test_companion_can_replace_it_with_dataclasses_replace(self, monkeypatch, tmp_path):
        """The companion's composition root is ``dataclasses.replace`` on the base
        context; a new required field must not break that path."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context

        class Two:
            def provisioners(self):
                return [BUILTIN_REMOTE_PROVISIONER, RemoteProvisioner("d", "amazon_devspace", "D")]

            def engine_for(self, provisioner_id):
                raise KeyError(provisioner_id)

        base = build_default_context(KiroCrewConfig.load(), profile="standalone")
        ctx = dataclasses.replace(base, remote_provisioners=Two())
        assert [p.id for p in ctx.remote_provisioners.provisioners()] == ["aws_ec2", "d"]


class TestTheConfirmationReachesTheEngine:
    """The operator's confirmation is the one launch input that does NOT come from
    ``cloud.json``, which is exactly why it can contradict it.

    So the seam carries it through untouched. Comparing it here -- against the same read of
    the file that built the spec -- would be the file confirming itself, and normalising it
    would be a second copy of a rule the engine already owns.
    """

    @staticmethod
    def _block() -> dict:
        arn = (
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
            "kirocrew/crew/demo/KIRO_IDENTITY-abcdef"
        )
        return {
            "cluster": "kirocrew-crew-prod",
            "subnets": ["subnet-a"],
            "security_groups": ["sg-1"],
            "image": "public.ecr.aws/example/kirocrew-crew-base@sha256:" + "a" * 64,
            "secrets": [["kirocrew/crew/demo/KIRO_IDENTITY", arn]],
            "cpu_architecture": "X86_64",
        }

    def _provider_over(self, monkeypatch, tmp_path, block):
        import json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        home = config_dir()
        home.mkdir(parents=True, exist_ok=True)
        (home / "cloud.json").write_text(json.dumps({"profile": "", "fargate": block}))
        return DefaultRemoteProvisionerProvider()

    def test_it_arrives_in_the_spec_exactly_as_given(self, monkeypatch, tmp_path):
        provider = self._provider_over(monkeypatch, tmp_path, self._block())

        engine = provider.engine_for(
            FARGATE_PROVISIONER_ID, confirmed_recipient="whatever the operator confirmed"
        )

        assert engine._require_spec().confirmed_recipient == "whatever the operator confirmed"

    def test_a_caller_that_confirms_nothing_gets_a_spec_that_cannot_launch(
        self, monkeypatch, tmp_path
    ):
        """The default is refused, not waved through.

        This is the path a RESUMED launch job takes: the job file carries no confirmation,
        because persisting one would put the operator's answer on disk beside the file it is
        meant to be independent of -- and then a rewrite of both is silent again. So a launch
        resumed after a gateway restart is refused and must be re-requested by the operator.
        """
        import pytest as _pytest

        provider = self._provider_over(monkeypatch, tmp_path, self._block())

        engine = provider.engine_for(FARGATE_PROVISIONER_ID)

        assert engine._require_spec().confirmed_recipient == ""
        with _pytest.raises(ValueError, match="confirmed"):
            engine.provision(tag="kc-a1b2c3", size_key="1024/2048", profile="p", region="us-east-1")

    def test_the_builtin_lane_ignores_it(self, monkeypatch, tmp_path):
        """Nothing in ``cloud.json`` chooses what the EC2 lane's credential goes to, so there
        is nothing to confirm and a value passed is ignored rather than refused -- which lets
        a caller confirm uniformly instead of branching on the lane."""
        provider = self._provider_over(monkeypatch, tmp_path, self._block())

        engine = provider.engine_for(BUILTIN_PROVISIONER_ID, confirmed_recipient="ignored")

        assert isinstance(engine, RealLaunchEngine)

    def test_the_seam_contract_declares_it(self):
        """A Protocol that omitted it would let a companion's provider satisfy the type while
        dropping the operator's confirmation on the floor."""
        import inspect

        sig = inspect.signature(RemoteProvisionerProvider.engine_for)
        param = sig.parameters["confirmed_recipient"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default == ""


class TestTheOperatorCanReadTheRecipientBeforeConfirming:
    """The DISPLAY half of the confirmation, which is the half that makes it a decision.

    A value obtainable only by attempting a launch and reading the refusal turns "confirm the
    recipient" into pasting a string back, and an operator who never saw it cannot recognise a
    wrong image. So the resolved recipient rides on the descriptor -- the row a launch card is
    drawn from and the one `GET /api/cloud/provisioners` publishes.
    """

    @staticmethod
    def _block() -> dict:
        arn = (
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
            "kirocrew/crew/demo/KIRO_IDENTITY-abcdef"
        )
        return {
            "cluster": "kirocrew-crew-prod",
            "subnets": ["subnet-a"],
            "security_groups": ["sg-1"],
            "image": "public.ecr.aws/example/kirocrew-crew-base@sha256:" + "a" * 64,
            "secrets": [["kirocrew/crew/demo/KIRO_IDENTITY", arn]],
            "cpu_architecture": "X86_64",
        }

    def _provider(self, monkeypatch, tmp_path, block):
        import json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        home = config_dir()
        home.mkdir(parents=True, exist_ok=True)
        (home / "cloud.json").write_text(json.dumps({"fargate": block}))
        return DefaultRemoteProvisionerProvider()

    def test_the_fargate_row_carries_the_resolved_recipient(self, monkeypatch, tmp_path):
        from kiro_crew.cloud.config import FargateConfig

        block = self._block()
        provider = self._provider(monkeypatch, tmp_path, block)

        row = next(p for p in provider.provisioners() if p.id == FARGATE_PROVISIONER_ID)

        expected = FargateConfig.from_mapping(block).credential_recipient()
        assert expected, "the fixture's block resolves no recipient, so this measures nothing"
        assert row.confirm_before_launch == expected

    def test_a_changed_image_changes_what_the_row_shows(self, monkeypatch, tmp_path):
        """Read per call, so the operator sees the CURRENT block rather than a startup snapshot
        -- and a block edited after the page loaded shows its own recipient on the next read."""
        provider = self._provider(monkeypatch, tmp_path, self._block())
        before = next(
            p for p in provider.provisioners() if p.id == FARGATE_PROVISIONER_ID
        ).confirm_before_launch

        import json

        from kiro_crew.config.loader import config_dir

        substituted = {**self._block(), "image": "public.ecr.aws/attacker/x@sha256:" + "b" * 64}
        (config_dir() / "cloud.json").write_text(json.dumps({"fargate": substituted}))

        after = next(
            p for p in provider.provisioners() if p.id == FARGATE_PROVISIONER_ID
        ).confirm_before_launch
        assert after != before
        assert "attacker" in after

    def test_the_builtin_row_has_nothing_to_confirm(self, monkeypatch, tmp_path):
        """Nothing in a configuration file chooses what the EC2 lane's credential reaches, so
        its row carries no value and the launch handler requires none."""
        provider = self._provider(monkeypatch, tmp_path, self._block())

        row = next(p for p in provider.provisioners() if p.id == BUILTIN_PROVISIONER_ID)

        assert row.confirm_before_launch == ""

    def test_an_incomplete_block_offers_no_row_at_all(self, monkeypatch, tmp_path):
        """Consistent with the lane being unregistered: there is no launch, so there is
        nothing to show and nothing to confirm."""
        provider = self._provider(monkeypatch, tmp_path, {**self._block(), "cluster": ""})

        assert [p.id for p in provider.provisioners()] == [BUILTIN_PROVISIONER_ID]

    def test_the_descriptor_declares_the_field(self):
        """On the shared descriptor, so an edition's lane can carry one too rather than the
        mechanism being special-cased to Fargate."""
        import dataclasses

        from kiro_crew.platform.interfaces import RemoteProvisioner

        field = {f.name: f for f in dataclasses.fields(RemoteProvisioner)}["confirm_before_launch"]
        assert field.default == ""


class TestTheAliasRefusalSitsWhereTheFileIsConsumed:
    """Item 6: the refusal belongs to the launch, not to every spawn on the host.

    A strict no-alias check on the universal spawn path failed EVERY sandboxed spawn -- chat
    turns, cron jobs, subagents -- when ``cloud.json`` had a second name, which is what stow,
    chezmoi and ``rsync --link-dest`` leave behind. The exposure is one lane's launch, so the
    refusal is at the point that launch reads the file.
    """

    @pytest.mark.parametrize("shape", ("symlink", "second hardlink"))
    def test_an_aliased_config_does_not_break_the_linux_spawn_path(
        self, monkeypatch, tmp_path, shape
    ):
        """EXECUTED, not inspected, and that distinction is the point.

        The refusal this replaces lived in ``_materialize_sealable_ceilings``, which
        ``namespace_argv`` calls on every Linux sandboxed spawn. A test that AST-inspects
        ``wrap_argv`` cannot see it, and a test that calls only the warn helper cannot either:
        both pass while every spawn on the host dies. So this runs the ceiling builder itself,
        with the file in the shape a dotfile manager leaves, and requires it to return.

        Both shapes matter and only one is a link. ``rsync --link-dest`` and hardlinking
        backups produce the second, which no symlink check sees at all.
        """
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        home = tmp_path
        home.mkdir(parents=True, exist_ok=True)
        real = home / "dotfiles-cloud.json"
        real.write_text("{}")
        target = home / "cloud.json"
        if shape == "symlink":
            target.symlink_to(real)
        else:
            os.link(real, target)
            assert target.stat().st_nlink > 1, "the fixture did not produce a second link"

        # Must not raise: a SandboxCeilingUnsealable here is a spawn the host cannot make.
        created = sandbox_mod._materialize_sealable_ceilings()

        assert isinstance(created, list)

    def test_the_launch_still_refuses_that_same_aliased_config(self, monkeypatch, tmp_path):
        """The complement, so the test above cannot be satisfied by removing the check.

        The alias harm is credential delivery to a container the owner did not choose, and it
        is answered where the launch reads the file rather than where a spawn starts.
        """
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        real = tmp_path / "dotfiles-cloud.json"
        real.write_text("{}")
        (tmp_path / "cloud.json").symlink_to(real)

        with pytest.raises(sandbox_mod.SandboxCeilingUnsealable):
            sandbox_mod.require_unaliased_cloud_config()

    def test_the_spawn_path_does_not_call_the_strict_file_check_at_all(self):
        """A ratchet over the whole module, because the regression was a SECOND call site.

        The name was removed from ``wrap_argv`` and the same behaviour was still reached
        through ``_materialize_sealable_ceilings``, so a per-function check is what missed it.
        This asserts the strict FILE check's callers are exactly the CONSUME seams, and
        names them: the provisioner seam for ``cloud.json``, the launch record's own read
        for ``cloud_launch_state.json``, and the wrapper the backend-grant readers call
        (``harnesses.json`` in the descriptor loader, ``backend-routing-attestations.json``
        in the attestation store). A spawn-path caller appearing under any name fails
        here, which is the property that was missed when the behaviour moved one function over.
        """
        import ast
        import inspect

        import kiro_crew.sandbox as sandbox_mod

        tree = ast.parse(inspect.getsource(sandbox_mod))
        callers = {
            fn.name
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef)
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and ast.unparse(c.func) == "_require_real_file_nofollow"
        }
        assert callers == {
            "require_unaliased_cloud_config",
            "require_unaliased_launch_state",
            "require_unaliased_grant_file",
        }, sorted(callers)

    def test_the_consumer_refuses_an_aliased_config(self, monkeypatch, tmp_path):
        """And the launch path still refuses it, so the alias costs the lane, not the box."""
        import json

        import pytest as _pytest

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.loader import config_dir

        home = config_dir()
        home.mkdir(parents=True, exist_ok=True)
        real = tmp_path / "elsewhere.json"
        real.write_text(
            json.dumps(
                {
                    "fargate": {
                        "cluster": "c",
                        "subnets": ["subnet-a"],
                        "security_groups": ["sg-1"],
                        "image": "public.ecr.aws/x/base@sha256:" + "a" * 64,
                        "secrets": [
                            [
                                "kirocrew/crew/demo/KIRO_IDENTITY",
                                "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                                "kirocrew/crew/demo/KIRO_IDENTITY-abcdef",
                            ]
                        ],
                        "cpu_architecture": "X86_64",
                    }
                }
            )
        )
        (home / "cloud.json").symlink_to(real)

        with _pytest.raises(Exception) as exc:
            DefaultRemoteProvisionerProvider().engine_for(FARGATE_PROVISIONER_ID)

        assert "cloud.json" in str(exc.value)
