"""The CPP ``remote_provisioners`` seam: descriptor, Default adapter, composition.

The HTTP half (listing, ``provider_id`` on a launch, engine routing) lives in
``test_cloud_handlers.py::TestProvisionerSeam``; the job-side half in
``test_cloud_launch_job.py::TestProvisionerOnTheJob``. This file pins the
contract objects themselves and that the default context composes the seam.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.cloud.launch_engine import RealLaunchEngine
from kiro_crew.platform.defaults import (
    BUILTIN_REMOTE_PROVISIONER,
    DefaultRemoteProvisionerProvider,
)
from kiro_crew.platform.interfaces import (
    BUILTIN_PROVISIONER_ID,
    RemoteProvisioner,
    RemoteProvisionerProvider,
)


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
