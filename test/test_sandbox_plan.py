"""The confinement plan: what one spawn masks, seals and re-opens, decided once.

:func:`kiro_crew.sandbox_plan.plan_confinement` is pure, so these tests hand it a literal
:class:`~kiro_crew.sandbox_plan.PlanHost` (the test adapter at the host seam) and assert
on the :class:`~kiro_crew.sandbox_plan.ConfinementPlan` it returns. The live adapter,
``kiro_crew.sandbox._live_plan_host``, has its own tests at the end: each host fact it
gathers reaches the plan.
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import sandbox, sandbox_plan, sandbox_seatbelt
from kiro_crew.dashboard.urls import dashboard_socket_name
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    CarveoutProbe,
    PlanHost,
    SandboxRequest,
    plan_confinement,
)

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

pytestmark = _POSIX_ONLY

HOME = "/srv/u"
CREW = f"{HOME}/.kiro/crew"
RUN = f"{CREW}/run"
VOICE = f"{RUN}/voice-runtime"
PROBE = f"{RUN}/mcp-tmp/probe-1"


def _host(**overrides: Any) -> PlanHost:
    """A small host: three tier dirs, one crew secret, one ceiling, the voice runtime."""
    fields: dict[str, Any] = {
        "home": HOME,
        "cwd": "/work",
        "tier_dirs": (".aws", ".gnupg", ".kiro/crew/policy_cache", ".kiro/crew/.env"),
        "cc_files": (".netrc", ".kiro/crew/.env"),
        "cc_expose_files": (".aws/config",),
        "crew_readonly_targets": (".kiro/crew/security_policy.json", ".kiro/crew/profiles"),
        "crew_hidden_dirs": (".kiro/crew/.env",),
        "unreadable_mask_leaves": ("token_signing.key",),
        "sensitive_env_prefixes": ("AWS_SECRET", "SSH_AUTH_SOCK"),
        "agent_denied_env_keys": ("SLACK_BOT_TOKEN",),
        "python_env_prefixes": ("PYTHONPATH",),
        "pod_grant_store_leaves": frozenset({".aws"}),
        "pod_masked_subleaves": (".aws/config", ".aws/credentials"),
        "voice_runtime_roots": (VOICE,),
        "voice_runtime_parents": (RUN,),
        "voice_runtime_ancestor_guards": (RUN, CREW, f"{HOME}/.kiro", HOME, "/srv"),
        "kiro_agents_targets": (f"{HOME}/.kiro/agents",),
        "uid": 1000,
        "gid": 1000,
        "ssh_accept_new": True,
    }
    fields.update(overrides)
    return PlanHost(**fields)


def _plan(
    tier: str = "strict",
    backend: str = BACKEND_NAMESPACE,
    host: PlanHost | None = None,
    **request: Any,
) -> sandbox_plan.ConfinementPlan:  # noqa: E501
    return plan_confinement(SandboxRequest(tier=tier, backend=backend, **request), host or _host())


def _probe(path: str, *, is_dir: bool = True, canonical: str | None = None) -> CarveoutProbe:
    return CarveoutProbe(raw=path, lexical=path, canonical=canonical or path, is_dir=is_dir)


# --------------------------------------------------------------------------- #
# The tier and the mask sources.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tier, files, expose, hide_ssh",
    [
        ("strict", (f"{HOME}/.netrc", f"{CREW}/.env"), (), True),
        ("cc", (f"{HOME}/.netrc", f"{CREW}/.env"), ((f"{HOME}/.aws/config", "config"),), False),
        ("standard", (), (), False),
        ("auto", (), (), False),  # an unknown level masks the strict dirs and nothing else
    ],
)
def test_the_tier_selects_files_exposures_and_the_ssh_mask(
    tier: str, files: tuple[str, ...], expose: tuple[tuple[str, str], ...], hide_ssh: bool
) -> None:
    plan = _plan(tier)
    assert plan.files == files
    assert plan.expose == expose
    assert plan.hide_ssh is hide_ssh
    assert plan.ssh_dir == f"{HOME}/.ssh" and plan.ssh_known_hosts == f"{HOME}/.ssh/known_hosts"


def test_the_masks_run_tier_dirs_pod_relocated_degraded_voice_then_caller() -> None:
    host = _host(
        pod_os_home="/pods/p1",
        relocated_policy_cache_dirs=("/srv/crew/policy_cache",),
        relocated_crew_hidden=("/srv/crew/.env",),
        md_notebook_degraded_dirs=(f"{CREW}/workspace/md-notebook",),
    )
    plan = _plan("strict", host=host, extra_hidden_dirs=("/data/private",))
    assert [m.path for m in plan.masks] == [
        f"{HOME}/.aws",
        f"{HOME}/.gnupg",
        f"{CREW}/policy_cache",
        f"{CREW}/.env",
        # The pod's remapped home: the tier minus its own grant store, plus the
        # profile files that grant store would otherwise expose.
        "/pods/p1/.gnupg",
        "/pods/p1/.kiro/crew/policy_cache",
        "/pods/p1/.kiro/crew/.env",
        "/pods/p1/.aws/config",
        "/pods/p1/.aws/credentials",
        "/srv/crew/policy_cache",
        "/srv/crew/.env",
        f"{CREW}/workspace/md-notebook",
        VOICE,
        "/data/private",
    ]
    assert [m.origin for m in plan.masks][-1] == "caller"
    assert {m.origin for m in plan.masks[:-1]} == {"tier"}


def test_a_tier_without_the_grant_store_gets_no_pod_sub_leaves() -> None:
    host = _host(tier_dirs=(".gnupg",), pod_os_home="/pods/p1")
    assert sandbox_plan.pod_home_targets(
        (".gnupg",), HOME, "/pods/p1", frozenset({".aws"}), (".aws/config",)
    ) == ["/pods/p1/.gnupg"]
    plan = _plan("standard", host=host)
    assert "/pods/p1/.aws/config" not in plan.sensitive_dirs
    assert sandbox_plan.pod_home_targets((".aws",), HOME, None, frozenset(), ()) == []
    assert sandbox_plan.pod_home_targets((".aws",), HOME, HOME, frozenset(), ()) == []


def test_the_sensitive_files_carry_every_mask_so_the_child_classifies_each() -> None:
    """A caller's hidden path may be a FILE: every path goes to both loops."""
    plan = _plan("strict", extra_hidden_dirs=("/data/key.pem",))
    assert "/data/key.pem" in plan.sensitive_dirs
    assert "/data/key.pem" in plan.sensitive_files
    assert plan.sensitive_files[: len(plan.files)] == plan.files


# --------------------------------------------------------------------------- #
# The read-only seals hold whatever extra_visible_dirs says.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_a_lifted_policy_cache_stays_read_only_and_the_lift_is_recorded(backend: str) -> None:
    cache = f"{CREW}/policy_cache"
    plan = _plan("strict", backend=backend, extra_visible_dirs=(cache,))
    [mask] = [m for m in plan.masks if m.path == cache]
    assert mask.cancelled and mask.read_only_when_cancelled
    assert cache not in plan.sensitive_dirs
    assert plan.cancellations == (sandbox_plan.Cancellation(cache, (cache,), True),)
    if backend == BACKEND_NAMESPACE:
        assert cache in plan.readonly


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize(
    "visible",
    [
        (f"{CREW}/security_policy.json",),
        (f"{CREW}/profiles", f"{HOME}/.kiro/agents"),
        (CREW, HOME, "/"),
    ],
)
def test_the_ceilings_stay_read_only_whatever_is_made_visible(
    backend: str, visible: tuple[str, ...]
) -> None:
    plan = _plan("strict", backend=backend, extra_visible_dirs=visible)
    for ceiling in (f"{CREW}/security_policy.json", f"{CREW}/profiles", f"{HOME}/.kiro/agents"):
        assert ceiling in plan.readonly
    assert RUN in plan.runtime_parents


def test_a_lift_of_an_ordinary_mask_only_cancels_it() -> None:
    plan = _plan("strict", extra_visible_dirs=(f"{HOME}/.gnupg/sub",))
    assert f"{HOME}/.gnupg" not in plan.sensitive_dirs
    assert f"{HOME}/.gnupg" not in plan.readonly
    assert plan.cancellations == (
        sandbox_plan.Cancellation(f"{HOME}/.gnupg", (f"{HOME}/.gnupg/sub",), False),
    )


def test_the_boolean_opt_in_seals_every_caller_mask_not_the_tier_dirs() -> None:
    """``seal_hidden_dirs=True`` seals every caller mask of the request; tier dirs never.

    A write into a hidden directory in the sandbox lands in a WRITABLE empty tmpfs
    stand-in and is lost on namespace teardown. The launcher seals that stand-in
    read-only so the write fails loudly -- but only when the spawn opted in, and only
    for its own ``extra_hidden_dirs`` (caller) masks. The tier host-credential dirs
    (``.aws`` re-exposing ``.aws/config`` in cc mode, ``.gnupg`` ...) are never caller
    masks, so they keep their writable stand-in -- which is what ``restore_exposed_files``
    and the vendor CLIs depend on.
    """
    bundle = "/data/bundle/data"
    other = "/data/other"
    plan = _plan("cc", extra_hidden_dirs=(bundle, other), seal_hidden_dirs=True)
    seal = set(plan.seal_windowless_masks)
    # Every caller mask of the opted-in request is sealed.
    assert bundle in seal and other in seal
    # The tier host-credential dirs are never caller masks, so never sealed: their tools
    # keep working and the cc-mode .aws re-expose does not hit a read-only mount.
    assert f"{HOME}/.aws" not in seal
    assert f"{HOME}/.gnupg" not in seal
    # Everything in the set is actually masked (not a cancelled lift).
    assert seal <= set(plan.sensitive_dirs)
    # The payload the launcher reads carries the same set.
    assert set(sandbox_plan.namespace_payload(plan)["seal_windowless_masks"]) == seal


def test_a_mask_seals_nothing_by_default() -> None:
    """With ``seal_hidden_dirs`` unset, nothing is sealed -- the default is unchanged.

    This is the invariant the review asked for: no mask is read-only unless its creator
    explicitly opted in, so a tier spawn and an ordinary caller mask behave exactly as
    they did before the opt-in seal existed.
    """
    plan = _plan("cc", extra_hidden_dirs=("/data/bundle/data",))
    assert plan.seal_windowless_masks == ()
    assert sandbox_plan.namespace_payload(plan)["seal_windowless_masks"] == []


# --------------------------------------------------------------------------- #
# The places the two backends differ, as declared capabilities.
# --------------------------------------------------------------------------- #


def test_the_capabilities_are_declared_per_renderer() -> None:
    caps = sandbox_plan.CAPABILITIES
    assert set(caps) == {BACKEND_NAMESPACE, BACKEND_SEATBELT}
    assert caps[BACKEND_SEATBELT].cc_unmaskable_dirs == frozenset()
    assert caps[BACKEND_NAMESPACE].cc_unmaskable_dirs == frozenset()


def test_cc_masks_aws_on_both_backends_and_exposes_only_config() -> None:
    linux = _plan("cc", BACKEND_NAMESPACE)
    macos = _plan("cc", BACKEND_SEATBELT)
    assert f"{HOME}/.aws" in linux.sensitive_dirs
    aws = {m.path: m for m in macos.masks}[f"{HOME}/.aws"]
    assert aws.exposed == (f"{HOME}/.aws/config",)
    assert not aws.cancelled
    # strict hides .aws with no exception; standard does not touch it.
    assert _plan("strict", BACKEND_SEATBELT).masks[0].path == f"{HOME}/.aws"
    assert _plan("strict", BACKEND_SEATBELT).masks[0].exposed == ()


def test_seatbelt_cc_profile_denies_aws_except_config() -> None:
    """Everything under ~/.aws but ``config`` is unreadable in a macOS cc profile:
    the credentials file, the SSO token cache and any link name inside it. The
    exception is a ``literal``, so it opens one path and nothing beneath ``config``."""
    profile = sandbox_seatbelt.render_seatbelt_profile(_plan("cc", BACKEND_SEATBELT))
    aws = f"{HOME}/.aws"
    read_rule = (
        f'(deny file-read* (require-all (subpath "{aws}")'
        f' (require-not (literal "{aws}/config"))))'
    )
    assert read_rule in profile.splitlines()
    assert f'(deny file-link (subpath "{aws}"))' in profile.splitlines()
    assert f'(deny file-read* (subpath "{aws}"))' not in profile
    # The only exception under ~/.aws is config itself.
    assert profile.count("require-not") == 1
    # A literal exception: nothing beneath a ``config`` directory or link name opens.
    assert f'(require-not (subpath "{aws}/config"))' not in profile
    for leaf in ("credentials", "sso/cache/abc123.json", "cli/cache/x.json", "config.bak"):
        assert f"{aws}/{leaf}" not in profile


#: For each declared capability: the tier and request that exercise it, and a value it
#: takes on each backend that is not that backend's own.
_CAPABILITY_FLIPS: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {
    "cc_unmaskable_dirs": (
        "cc",
        {},
        {BACKEND_NAMESPACE: frozenset({".aws"}), BACKEND_SEATBELT: frozenset({".aws"})},
    ),
}


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize(
    "capability", sorted(f.name for f in fields(sandbox_plan.RendererCapabilities))
)
def test_every_declared_capability_changes_either_backends_plan(
    monkeypatch: pytest.MonkeyPatch, capability: str, backend: str
) -> None:
    """A capability is a switch the planning reads, not a label: flipped on its own,
    it changes the plan of whichever backend declares it."""
    tier, request, flipped = _CAPABILITY_FLIPS[capability]
    declared = _plan(tier, backend, **request)
    caps = sandbox_plan.CAPABILITIES[backend]
    monkeypatch.setitem(
        sandbox_plan.CAPABILITIES, backend, replace(caps, **{capability: flipped[backend]})
    )
    assert _plan(tier, backend, **request) != declared


def test_a_window_holding_a_masked_leaf_is_refused_where_rules_cannot_be_reordered() -> None:
    apps = f"{CREW}/apps"
    window = f"{apps}/meetings/data"
    leaf = f"{window}/edits"
    request = {"extra_hidden_dirs": (apps, leaf), "extra_private_dirs": (window,)}
    linux = _plan("standard", BACKEND_NAMESPACE, **request)
    macos = _plan("standard", BACKEND_SEATBELT, **request)
    assert linux.windows == (window,) and linux.refusals == ()
    assert macos.windows == ()
    assert [r.message for r in macos.refusals] == [sandbox_plan.WINDOW_REFUSAL]


def test_a_window_equal_to_its_mask_is_refused_everywhere() -> None:
    apps = f"{CREW}/apps"
    for backend in (BACKEND_NAMESPACE, BACKEND_SEATBELT):
        plan = _plan("standard", backend, extra_hidden_dirs=(apps,), extra_private_dirs=(apps,))
        assert plan.windows == ()
        assert {r.kind for r in plan.refusals} == {"private-window"}


def test_seatbelt_keeps_a_window_over_a_lift_and_never_lifts_the_voice_runtime() -> None:
    apps = f"{CREW}/apps"
    window = f"{apps}/alpha/data"
    request = {
        "extra_hidden_dirs": (apps,),
        "extra_private_dirs": (window,),
        "extra_visible_dirs": (f"{apps}/alpha", VOICE),
    }
    macos = _plan("standard", BACKEND_SEATBELT, **request)
    [caller] = [m for m in macos.masks if m.path == apps]
    assert caller.windows == (window,) and not caller.cancelled
    assert caller.window_ancestors == (f"{apps}/alpha", apps)
    [voice] = [m for m in macos.masks if m.path == VOICE]
    assert not voice.cancelled and voice.write_sealed
    linux = _plan("standard", BACKEND_NAMESPACE, **request)
    assert apps not in linux.sensitive_dirs and VOICE not in linux.sensitive_dirs
    assert linux.windows == ()


def test_seatbelt_records_the_directories_realpath_stats_to_reach_an_exposed_file() -> None:
    """A file carved out of a Seatbelt mask is reached through the masked directories
    above it, and ``realpath`` stats each one: a launch config staged in a masked tree is
    unopenable by a CLI that canonicalizes its path unless they are recorded."""
    staging = f"{CREW}/auth-store-staging"
    exposed = f"{staging}/sub/strands-mcp-1-x.json"
    macos = _plan(
        "standard", BACKEND_SEATBELT, extra_hidden_dirs=(staging,), extra_expose_files=(exposed,)
    )
    [caller] = [m for m in macos.masks if m.path == staging]
    assert caller.exposed == (exposed,)
    assert caller.exposed_ancestors == (f"{staging}/sub", staging)
    aws = {m.path: m for m in _plan("cc", BACKEND_SEATBELT).masks}[f"{HOME}/.aws"]
    assert aws.exposed_ancestors == (f"{HOME}/.aws",)
    unexposed = {m.path: m for m in _plan("strict", BACKEND_SEATBELT).masks}[f"{HOME}/.aws"]
    assert unexposed.exposed_ancestors == ()


def test_only_the_namespace_launcher_folds_crew_home_aliases() -> None:
    alias = (f"{HOME}/.kiro/crew", "/real/crew", 1, 2)
    request = {"crew_home_aliases": (alias,), "extra_hidden_dirs": (f"{CREW}/apps",)}
    linux = _plan("strict", BACKEND_NAMESPACE, **request)
    macos = _plan("strict", BACKEND_SEATBELT, **request)
    assert "/real/crew/apps" in linux.sensitive_dirs
    assert f"{CREW}/apps" not in linux.sensitive_dirs
    assert "/real/crew/security_policy.json" in linux.readonly
    assert f"{CREW}/apps" in [m.path for m in macos.masks]
    assert linux.identities.crew_home_aliases == (alias,)


def test_seatbelt_write_seals_the_trees_whose_writes_matter() -> None:
    macos = _plan("strict", BACKEND_SEATBELT)
    by_path = {m.path: m for m in macos.masks}
    assert by_path[f"{CREW}/policy_cache"].write_sealed
    assert by_path[f"{CREW}/.env"].write_sealed and by_path[f"{CREW}/.env"].literal_write_sealed
    assert by_path[VOICE].write_sealed
    assert not by_path[f"{HOME}/.aws"].write_sealed


# --------------------------------------------------------------------------- #
# Write carve-outs.
# --------------------------------------------------------------------------- #


def test_a_carve_out_inside_the_runtime_parent_is_approved_in_both_spellings() -> None:
    host = _host(carveout_probes=(_probe(PROBE, canonical="/real/run/mcp-tmp/probe-1"),))
    host = replace(host, voice_runtime_parents=(RUN, "/real/run"))
    plan = _plan("standard", host=host, extra_writable_dirs=(PROBE,))
    assert plan.writable == (PROBE, "/real/run/mcp-tmp/probe-1")
    assert plan.refusals == ()


@pytest.mark.parametrize(
    "candidate, probe, reason",
    [
        ("relative/dir", None, "not an absolute path"),
        (
            f"{RUN}/missing",
            _probe(f"{RUN}/missing", is_dir=False),
            "not an existing real directory",
        ),
        ("/elsewhere/dir", _probe("/elsewhere/dir"), "is outside every carveable runtime parent"),
        (RUN, _probe(RUN), f"sealed path {VOICE!r} would be re-opened by it"),
        (f"{VOICE}/x", _probe(f"{VOICE}/x"), f"it lies inside sealed subtree {VOICE!r}"),
    ],
)
def test_a_carve_out_that_would_reopen_a_seal_is_refused_as_data(
    candidate: str, probe: CarveoutProbe | None, reason: str
) -> None:
    host = _host(carveout_probes=(probe,) if probe else ())
    for backend in (BACKEND_NAMESPACE, BACKEND_SEATBELT):
        plan = _plan("standard", backend, host=host, extra_writable_dirs=(candidate,))
        assert plan.writable == ()
        [refused] = plan.refusals
        assert refused.kind == "carve-out"
        assert reason in refused.message % refused.args


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_a_tree_lifted_read_only_cannot_grow_a_writable_window(backend: str) -> None:
    """A mask a caller lifted still guards its subtree against a carve-out."""
    exposed = f"{RUN}/exposed"
    inside = f"{exposed}/inside"
    host = _host(carveout_probes=(_probe(inside),))
    plan = _plan(
        "standard",
        backend,
        host=host,
        extra_hidden_dirs=(exposed,),
        extra_visible_dirs=(exposed,),
        extra_writable_dirs=(inside,),
    )
    assert plan.writable == ()
    [refused] = plan.refusals
    assert f"it lies inside sealed subtree {exposed!r}" in refused.message % refused.args


def test_the_carve_out_candidates_are_the_spellings_the_plan_validates() -> None:
    alias = (f"{HOME}/.kiro/crew", "/real/crew", 1, 2)
    request = SandboxRequest(
        tier="strict", extra_writable_dirs=(PROBE,), crew_home_aliases=(alias,)
    )
    assert sandbox_plan.carveout_candidates(request) == ("/real/crew/run/mcp-tmp/probe-1",)
    seatbelt = replace(request, backend=BACKEND_SEATBELT)
    assert sandbox_plan.carveout_candidates(seatbelt) == (PROBE,)


# --------------------------------------------------------------------------- #
# The environment scrub and the namespace identities.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tier, strip, forward, expected",
    [
        ("standard", False, False, ("AWS_SECRET", "SSH_AUTH_SOCK")),
        ("strict", False, False, ("AWS_SECRET", "SSH_AUTH_SOCK", "SLACK_BOT_TOKEN")),
        ("cc", True, False, ("AWS_SECRET", "SSH_AUTH_SOCK", "SLACK_BOT_TOKEN", "PYTHONPATH")),
        ("strict", True, True, ("AWS_SECRET", "SLACK_BOT_TOKEN", "PYTHONPATH")),
    ],
)
def test_the_scrub_list_follows_the_tier_and_the_two_opt_ins(
    tier: str, strip: bool, forward: bool, expected: tuple[str, ...]
) -> None:
    plan = _plan(tier, strip_python_env=strip, forward_ssh_auth_sock=forward)
    assert plan.env_scrub_prefixes == expected


def test_an_established_target_under_an_earlier_mask_is_not_required() -> None:
    plan = _plan(
        "strict",
        required_mask_targets=(f"{HOME}/.gnupg/pubring", "/data/ceiling", "/data/ceiling"),
    )
    assert plan.identities.required_mask_targets == ("/data/ceiling",)


def test_the_identities_pass_through_deduplicated_and_folded() -> None:
    alias = (f"{HOME}/.kiro/crew", "/real/crew", 1, 2)
    plan = _plan(
        "strict",
        crew_home_aliases=(alias,),
        extra_hidden_dir_ids=((f"{CREW}/apps", 3, 4),),
        extra_private_dir_ids=((f"{CREW}/apps/a", 5, 6),),
        extra_alias_credential_ids=((7, 8), (7, 8), (9, 10)),
        fail_closed_file_masks=((f"{CREW}/x.key", 11, 12), (f"{CREW}/x.key", 11, 12)),
        mask_occupants=((f"{CREW}/apps", (3, 4, 1, 1, 5, 6)), (f"{HOME}/.ssh", (7, 8, 0))),
    )
    ids = plan.identities
    assert ids.hidden_dir_ids == {"/real/crew/apps": (3, 4)}
    assert ids.private_dir_ids == {"/real/crew/apps/a": (5, 6)}
    assert ids.alias_credential_ids == ((7, 8), (9, 10))
    assert ids.fail_closed_file_masks == (("/real/crew/x.key", 11, 12),)
    assert ids.mask_occupants == {
        f"{HOME}/.ssh": (7, 8, 0),
        "/real/crew/apps": (3, 4, 1, 1, 5, 6),
    }


def test_the_payload_carries_no_value_python_cannot_read_as_a_literal() -> None:
    plan = _plan("strict", mask_occupants=((f"{HOME}/.ssh", (1, 2, True, 1, 3, 4)),))
    payload = sandbox_plan.namespace_payload(plan)

    def _walk(value: object) -> None:
        assert value is not None and not isinstance(value, bool)
        if isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(payload)
    assert payload["mask_occupants"][f"{HOME}/.ssh"] == [1, 2, 1, 1, 3, 4]
    assert payload["hide_ssh"] == 1
    assert payload["strict_host_key_opt"] == " -o StrictHostKeyChecking=accept-new"
    # _host() leaves runtime_dir_durable at its False default: no per-user systemd
    # manager, so the durable host-wide /dev/shm leads the volatile /run/user/$UID.
    assert payload["stand_in_roots"] == ["/dev/shm", "/run/user/1000"]


@pytest.mark.parametrize(
    "durable, expected",
    [
        (True, ["/run/user/1000", "/dev/shm"]),
        (False, ["/dev/shm", "/run/user/1000"]),
    ],
    ids=["manager-present", "no-manager"],
)
def test_the_stand_in_roots_lead_with_the_durable_one_without_a_user_manager(
    durable: bool, expected: list[str]
) -> None:
    # On a host with no per-user systemd manager, /run/user/$UID is torn down and remade
    # across session churn, and a bind source pinned there corrupts the host crew-home
    # leaf (.env) to 0 bytes on teardown. The planner steers the launcher
    # to the manager-independent /dev/shm first in that case, and keeps /run/user/$UID
    # first where a manager keeps it coherent. Both candidates are always present, so
    # the launcher's own cross-fs check and mid-create retry are unchanged.
    plan = _plan("strict", host=_host(runtime_dir_durable=durable))
    assert sandbox_plan.namespace_payload(plan)["stand_in_roots"] == expected


# --------------------------------------------------------------------------- #
# The planner is pure.
# --------------------------------------------------------------------------- #


_REQUEST = {
    "extra_hidden_dirs": (f"{CREW}/apps", "relative/hidden"),
    "extra_visible_dirs": (f"{CREW}/policy_cache",),
    "extra_private_dirs": (f"{CREW}/apps/a/data",),
    "extra_writable_dirs": (PROBE, "relative"),
    "extra_expose_files": (f"{HOME}/.aws/sso/token.json",),
}


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_the_planner_reads_nothing_but_its_arguments(backend: str) -> None:
    """No filesystem call, no working directory, no environment, no logging."""
    reached: list[str] = []

    def _forbidden(name: str):  # noqa: ANN202
        def _refuse(*_args: object, **_kwargs: object) -> object:
            reached.append(name)
            raise AssertionError(f"the planner reached {name}")

        return _refuse

    host = _host(carveout_probes=(_probe(PROBE),))
    expected = _plan("strict", backend, host=host, **_REQUEST)
    with pytest.MonkeyPatch.context() as patched:
        for module, names in (
            (os, ("stat", "lstat", "listdir", "scandir", "getcwd", "readlink", "access", "open")),
            (os.path, ("exists", "lexists", "isdir", "isfile", "islink", "realpath")),
            (os.path, ("expanduser",)),
            (builtins, ("open",)),
            (logging.Logger, ("warning", "info", "debug", "error")),
        ):
            for name in names:
                patched.setattr(module, name, _forbidden(f"{module.__name__}.{name}"))
        patched.setattr(os, "environ", {})
        try:
            plan = _plan("strict", backend, host=host, **_REQUEST)
        except AssertionError:
            plan = None
    assert reached == []
    assert plan == expected


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_the_same_request_and_host_give_the_same_plan(backend: str) -> None:
    host = _host(carveout_probes=(_probe(PROBE),))
    first = _plan("cc", backend, host=host, **_REQUEST)
    assert all(_plan("cc", backend, host=host, **_REQUEST) == first for _ in range(3))


def test_a_relative_request_path_resolves_against_the_hosts_working_directory() -> None:
    plan = _plan("strict", extra_hidden_dirs=("rel/hidden",), host=_host(cwd="/work/dir"))
    assert "/work/dir/rel/hidden" in plan.sensitive_dirs


def test_an_unknown_backend_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        _plan("strict", "windows-job-object")


# --------------------------------------------------------------------------- #
# The Seatbelt renderer: one golden.
# --------------------------------------------------------------------------- #


_SEATBELT_GOLDEN = """\
(version 1)
(allow default)
(deny file-read* (subpath "/srv/u/.aws"))
(deny file-link (subpath "/srv/u/.aws"))
(deny file-read* (subpath "/srv/u/.gnupg"))
(deny file-link (subpath "/srv/u/.gnupg"))
(deny file-write* (subpath "/srv/u/.kiro/crew/policy_cache"))
(deny file-link (subpath "/srv/u/.kiro/crew/policy_cache"))
(deny file-read* (subpath "/srv/u/.kiro/crew/.env"))
(deny file-write* (subpath "/srv/u/.kiro/crew/.env"))
(deny file-write* (literal "/srv/u/.kiro/crew/.env"))
(deny file-link (subpath "/srv/u/.kiro/crew/.env"))
(deny file-read* (literal "/srv/u/.kiro/crew/.env"))
(deny file-link (literal "/srv/u/.kiro/crew/.env"))
(deny file-read* (subpath "/srv/u/.kiro/crew/run/voice-runtime"))
(deny file-write* (subpath "/srv/u/.kiro/crew/run/voice-runtime"))
(deny file-link (subpath "/srv/u/.kiro/crew/run/voice-runtime"))
(deny file-write* (literal "/srv/u/.kiro/crew/run"))
(deny file-write* (subpath "/srv/u/.kiro/crew/run"))
(deny file-link (subpath "/srv/u/.kiro/crew/run"))
(deny file-write* (literal "/srv/u/.kiro/crew/run"))
(deny file-write* (literal "/srv/u/.kiro/crew"))
(deny file-write* (literal "/srv/u/.kiro"))
(deny file-write* (literal "/srv/u"))
(deny file-write* (literal "/srv"))
(deny file-write* (literal "/srv/u/.kiro/crew/security_policy.json"))
(deny file-write* (subpath "/srv/u/.kiro/crew/security_policy.json"))
(deny file-link (subpath "/srv/u/.kiro/crew/security_policy.json"))
(deny file-write* (literal "/srv/u/.kiro/crew/profiles"))
(deny file-write* (subpath "/srv/u/.kiro/crew/profiles"))
(deny file-link (subpath "/srv/u/.kiro/crew/profiles"))
(deny file-write* (literal "/srv/u/.kiro/agents"))
(deny file-write* (subpath "/srv/u/.kiro/agents"))
(deny file-link (subpath "/srv/u/.kiro/agents"))
(deny file-read* (literal "/srv/u/.netrc"))
(deny file-link (literal "/srv/u/.netrc"))
(deny file-read* (literal "/srv/u/.kiro/crew/.env"))
(deny file-link (literal "/srv/u/.kiro/crew/.env"))
(deny file-read* (require-all (subpath "/srv/u/.kiro/crew/apps") (require-not (subpath "/srv/u/.kiro/crew/apps/a/data"))))
(deny file-write* (require-all (subpath "/srv/u/.kiro/crew/apps") (require-not (subpath "/srv/u/.kiro/crew/apps/a/data"))))
(deny file-link (require-all (subpath "/srv/u/.kiro/crew/apps") (require-not (subpath "/srv/u/.kiro/crew/apps/a/data"))))
(allow file-read-metadata (literal "/srv/u/.kiro/crew/apps/a"))
(allow file-read-metadata (literal "/srv/u/.kiro/crew/apps"))
(deny file-read* (require-all (subpath "/work/relative/hidden") (require-not (literal "/work/relative/hidden/token.json"))))
(deny file-write* (subpath "/work/relative/hidden"))
(deny file-link (subpath "/work/relative/hidden"))
(deny file-read* (literal "/work/relative/hidden"))
(deny file-write* (literal "/work/relative/hidden"))
(deny file-link (literal "/work/relative/hidden"))
(allow file-read-metadata (literal "/work/relative/hidden"))
(deny file-read* (require-all (subpath "/srv/u/.ssh") (require-not (literal "/srv/u/.ssh/known_hosts"))))
(deny file-write* (subpath "/srv/u/.ssh"))
(deny file-link (subpath "/srv/u/.ssh"))
(allow file-write* (subpath "/srv/u/.kiro/crew/run/mcp-tmp/probe-1"))
"""


def test_the_seatbelt_renderer_spells_the_plan_as_rules() -> None:
    host = _host(carveout_probes=(_probe(PROBE),))
    request = dict(_REQUEST, extra_expose_files=("/work/relative/hidden/token.json",))
    plan = _plan("strict", BACKEND_SEATBELT, host=host, **request)
    assert sandbox_seatbelt.render_seatbelt_profile(plan) == _SEATBELT_GOLDEN


# --------------------------------------------------------------------------- #
# The live host adapter.
# --------------------------------------------------------------------------- #


@pytest.fixture
def live_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home under *tmp_path*, at the names the live adapter reads."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    for name in ("KIROCREW_POD", "KIROCREW_OS_HOME", "KIRO_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "config_dir", lambda: crew)
    monkeypatch.setattr(sandbox, "kiro_agents_dir", lambda: home / ".kiro" / "agents")
    run = crew / "run"
    voice = run / "voice-runtime"
    monkeypatch.setattr(
        sandbox,
        "_voice_runtime_paths_cache",
        (str(crew), str(voice), (str(voice),), (str(run),), (str(run), str(crew))),
    )
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: False)
    monkeypatch.setattr(sandbox, "_runtime_dir_is_durable", lambda _root: False)
    return home


def test_the_live_adapter_reads_the_host_the_builders_always_read(live_host: Path) -> None:
    crew = live_host / ".kiro" / "crew"
    probe = crew / "run" / "mcp-tmp" / "p"
    probe.mkdir(parents=True)
    request = SandboxRequest(tier="strict", extra_writable_dirs=(str(probe), "relative"))
    host = sandbox._live_plan_host(request)
    assert host.home == str(live_host)
    assert host.uid == os.getuid() and host.gid == os.getgid()
    assert host.tier_dirs == tuple(sandbox._sandbox_policy().strict_dirs())
    assert host.voice_runtime_roots == (str(crew / "run" / "voice-runtime"),)
    assert host.kiro_agents_targets == (str(live_host / ".kiro" / "agents"),)
    assert host.carveout_probes == (_probe(str(probe)),)
    assert host.ssh_accept_new is False
    assert host.cwd == os.sep  # every request path that needs a cwd is absolute


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_a_relative_request_path_is_planned_against_the_working_directory(
    live_host: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    plan = sandbox._spawn_plan(backend, "strict", extra_hidden_dirs=("rel/x",))
    assert str(work / "rel" / "x") in [mask.path for mask in plan.masks]


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("root", ["home", "pod home"])
def test_a_relative_home_is_compared_against_the_working_directory(
    live_host: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str, root: str
) -> None:
    """A tier mask joined against a relative ``$HOME`` -- or a pod's relative remapped
    home -- is lifted by a visible directory spelled absolutely under the working
    directory, as ``os.path.abspath`` would."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    if root == "home":
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("relhome")))
        masked = os.path.join("relhome", ".aws")
    else:
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", "relpod")
        masked = os.path.join("relpod", ".gnupg")
    plan = sandbox._spawn_plan(backend, "strict", extra_visible_dirs=(str(work / masked),))
    assert masked in [m.path for m in plan.masks if m.cancelled]


def test_the_live_adapter_asks_only_for_what_the_backend_renders(
    live_host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden() -> bool:
        raise AssertionError("the Seatbelt plan does not ask the host ssh")

    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", _forbidden)
    host = sandbox._live_plan_host(SandboxRequest(tier="cc", backend=BACKEND_SEATBELT))
    assert host.uid == 0 and host.gid == 0 and host.ssh_accept_new is False
    assert host.voice_runtime_ancestor_guards  # Seatbelt guards the rename-sensitive parents
    assert host.tier_dirs == tuple(sandbox._sandbox_policy().cc_dirs())


@pytest.mark.parametrize("durable", [True, False], ids=["manager-present", "no-manager"])
def test_the_live_adapter_carries_the_runtime_dir_durability_for_the_namespace_backend(
    live_host: Path, monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    # The namespace backend's bind-source root order depends on whether a per-user
    # systemd manager keeps /run/user/$UID coherent. The live adapter reads
    # that from _runtime_dir_is_durable and hands it to the plan.
    monkeypatch.setattr(sandbox, "_runtime_dir_is_durable", lambda _root: durable)
    host = sandbox._live_plan_host(SandboxRequest(tier="strict"))
    assert host.runtime_dir_durable is durable


def test_the_live_adapter_does_not_ask_runtime_durability_for_seatbelt(
    live_host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden(_root: str) -> bool:
        raise AssertionError("the Seatbelt plan has no bind-source stand-in root")

    monkeypatch.setattr(sandbox, "_runtime_dir_is_durable", _forbidden)
    host = sandbox._live_plan_host(SandboxRequest(tier="cc", backend=BACKEND_SEATBELT))
    assert host.runtime_dir_durable is False


def _listener(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def test_runtime_dir_is_durable_requires_a_LIVE_user_manager_socket(
    short_sock_dir: Path,
) -> None:
    # A LIVE per-user manager is one LISTENING on its private socket;
    # _runtime_dir_is_durable proves that with a non-blocking connect, so a reachable
    # socket reads durable and everything else does not -- crucially, a STALE socket
    # file a dead manager left behind (connect refused) is NOT durable, which a mere
    # stat of the file could not tell apart. short_sock_dir keeps the AF_UNIX path under
    # sun_path's ~104/108-byte cap, which a deep tmp_path on macOS would blow.
    runtime = short_sock_dir / "run-user"
    (runtime / "systemd").mkdir(parents=True)
    sock_path = runtime / "systemd" / "private"
    # A stale socket FILE with nothing listening is not durable (connect refused).
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        stale.bind(str(sock_path))  # a bound-but-not-listening socket refuses connect
        assert sandbox._runtime_dir_is_durable(str(runtime)) is False
    finally:
        stale.close()
    sock_path.unlink(missing_ok=True)
    # A manager LISTENING on that socket is durable.
    live = _listener(sock_path)
    try:
        assert sandbox._runtime_dir_is_durable(str(runtime)) is True
    finally:
        live.close()
    # No runtime dir at all -> no socket to dial -> not durable.
    assert sandbox._runtime_dir_is_durable(str(short_sock_dir / "gone")) is False


def test_a_live_session_bus_without_a_manager_is_not_durable(short_sock_dir: Path) -> None:
    # A plain ``dbus-daemon --session`` listens on <runtime>/bus with no per-user
    # systemd manager behind it: exactly the host whose /run/user/$UID logind churns.
    runtime = short_sock_dir / "run-user"
    runtime.mkdir()
    bus = _listener(runtime / "bus")
    try:
        assert sandbox._runtime_dir_is_durable(str(runtime)) is False
    finally:
        bus.close()


def test_a_manager_under_another_runtime_dir_does_not_make_the_staging_root_durable(
    live_host: Path, monkeypatch: pytest.MonkeyPatch, short_sock_dir: Path
) -> None:
    # XDG_RUNTIME_DIR names a directory with a LIVE manager, but the bind sources stage
    # under /run/user/$UID. The adapter probes the staging root itself, never the env.
    other = short_sock_dir / "xdg"
    (other / "systemd").mkdir(parents=True)
    live = _listener(other / "systemd" / "private")
    probed: list[str] = []
    real = sandbox._runtime_dir_is_durable

    def _spy(root: str) -> bool:
        probed.append(root)
        return real(root)

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(other))
    monkeypatch.setattr(sandbox, "_runtime_dir_is_durable", _spy)
    try:
        host = sandbox._live_plan_host(SandboxRequest(tier="strict"))
    finally:
        live.close()
    staging_root = sandbox_plan.user_runtime_root(host.uid)
    assert probed == [staging_root]
    assert str(other) not in probed


@pytest.mark.parametrize(
    "name, value, field, expected",
    [
        ("_CC_FILES", [".probe-file"], "files", "/.probe-file"),
        ("_STANDARD_DIRS", [".probe-dir"], "sensitive_dirs", "/.probe-dir"),
        ("_CREW_READONLY_TARGETS", [".probe-ceiling"], "readonly", "/.probe-ceiling"),
        ("_SENSITIVE_ENV_PREFIXES", ["PROBE_"], "env_scrub_prefixes", "PROBE_"),
        (
            "_CREW_UNREADABLE_MASK_LEAVES",
            frozenset({"probe.key"}),
            "unreadable_mask_leaves",
            "probe.key",
        ),
    ],
)
def test_a_table_rebound_on_the_sandbox_reaches_the_plan(
    live_host: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: object,
    field: str,
    expected: str,
) -> None:
    tier = "standard" if name == "_STANDARD_DIRS" else "strict"

    def _values() -> list[str]:
        return [str(v) for v in getattr(sandbox._spawn_plan(BACKEND_NAMESPACE, tier), field)]

    assert not any(v.endswith(expected) for v in _values())
    monkeypatch.setattr(sandbox, name, value)
    assert any(v.endswith(expected) for v in _values())


def test_the_spawn_plan_logs_each_refusal_under_the_sandbox_logger(
    live_host: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="kiro_crew.sandbox")
    sandbox._spawn_plan(BACKEND_NAMESPACE, "standard", extra_writable_dirs=("relative",))
    [record] = [r for r in caplog.records if r.name == "kiro_crew.sandbox"]
    assert record.getMessage() == (
        "SECURITY: refusing sandbox write carve-out 'relative': not an absolute path"
    )


# --------------------------------------------------------------------------- #
# Host UNIX sockets the agent must not connect to (Seatbelt only).
# --------------------------------------------------------------------------- #

_MCS = sandbox_plan.MCS_SOCKETS
_SOCKET_DENY = (_MCS, rf"^{RUN}/tunnel-sidecar-[^/]*\.sock$")


def _socket_rule(pattern: str) -> str:
    return f'(deny network-outbound (remote unix-socket (path-regex #"{pattern}")))'


def _seal_rules(pattern: str) -> list[str]:
    return [f'(deny file-write* (regex #"{pattern}"))', f'(deny file-link (regex #"{pattern}"))']


def test_seatbelt_renders_each_denied_socket_and_namespace_carries_none() -> None:
    host = _host(denied_socket_patterns=_SOCKET_DENY, sealed_socket_patterns=(_MCS,))
    macos = _plan("strict", BACKEND_SEATBELT, host=host)
    assert macos.denied_socket_patterns == _SOCKET_DENY
    assert macos.sealed_socket_patterns == (_MCS,)
    lines = sandbox_seatbelt.render_seatbelt_profile(macos).splitlines()
    for pattern in _SOCKET_DENY:
        assert _socket_rule(pattern) in lines
    # Only the temp-dir sockets are write-sealed: the sidecar's run dir already is.
    assert [line for line in lines if "(regex " in line] == _seal_rules(_MCS)
    # The namespace backend has no socket rule to render; its plan is unchanged.
    linux = _plan("strict", BACKEND_NAMESPACE, host=host)
    assert linux.denied_socket_patterns == () and linux.sealed_socket_patterns == ()
    assert linux == _plan("strict", BACKEND_NAMESPACE)


def test_a_socket_pattern_that_would_end_the_raw_string_is_refused() -> None:
    plan = _plan("strict", BACKEND_SEATBELT, host=_host(denied_socket_patterns=('^/a"b$',)))
    with pytest.raises(ValueError, match="unrenderable socket deny pattern"):
        sandbox_seatbelt.render_seatbelt_profile(plan)


def test_a_sealed_socket_pattern_must_also_be_denied() -> None:
    plan = _plan("strict", BACKEND_SEATBELT, host=_host(sealed_socket_patterns=('^/a"b$',)))
    with pytest.raises(ValueError, match="sealed socket pattern is not denied"):
        sandbox_seatbelt.render_seatbelt_profile(plan)


@pytest.mark.parametrize("path", ["/srv/u/.kiro/crew/run", "/a b/(x)+[y]$.^|{1}?*\\z", '/q"t'])
def test_a_run_dir_spelling_is_matched_literally(path: str) -> None:
    [_, sidecar] = sandbox_plan.denied_socket_patterns((path,), keep_ssh_agent=False)
    assert '"' not in sidecar
    assert re.fullmatch(sidecar, f"{path}/tunnel-sidecar-5476.sock")
    assert not re.fullmatch(sidecar, f"{path}/tunnel-sidecar-5476.sock/x")
    assert not re.fullmatch(sidecar, f"{path}/dashboard-5476.sock")


def test_the_ssh_agent_exception_admits_mcs_agent_sock_alone() -> None:
    """The lookahead-free ERE is exactly "every mcs socket but ``mcs-agent.sock``"."""
    names = ["", "a", "ag", "age", "agen", "agent", "agents", "agent2", "agent.sock", "agen.t"]
    names += ["reflex", "desktop-json", "keys", "b", "aa", "agg", "agex", "x-agent", "Agent"]
    names += [n + suffix for n in ("a", "ag", "age", "agen") for suffix in ("x", "-1", ".sock")]
    for name in names:
        for tmp in ("/tmp", "/private/tmp"):
            path = f"{tmp}/mcs-{name}.sock"
            expected = bool(re.fullmatch(_MCS, path)) and name != "agent"
            assert bool(re.fullmatch(sandbox_plan.MCS_SOCKETS_BUT_SSH_AGENT, path)) is expected
    assert not re.fullmatch(sandbox_plan.MCS_SOCKETS_BUT_SSH_AGENT, "/tmp/x/mcs-reflex.sock")


@pytest.mark.parametrize("forward", [False, True])
def test_the_live_seatbelt_host_denies_the_credential_and_sidecar_sockets_only(
    live_host: Path, forward: bool
) -> None:
    crew = live_host / ".kiro" / "crew"
    request = SandboxRequest(
        tier="standard", backend=BACKEND_SEATBELT, forward_ssh_auth_sock=forward
    )
    host = sandbox._live_plan_host(request)
    patterns = host.denied_socket_patterns

    def denied(path: str) -> bool:
        return any(re.fullmatch(p, path) for p in patterns)

    # The credential and desktop vends are denied whatever the ssh forward says.
    assert denied("/tmp/mcs-reflex.sock") and denied("/private/tmp/mcs-desktop-json.sock")
    # The ssh agent (``IdentityAgent`` in ~/.ssh/config) follows the forward verdict.
    assert denied("/tmp/mcs-agent.sock") is not forward
    assert denied("/private/tmp/mcs-agent.sock") is not forward
    assert denied(str(crew / "run" / "tunnel-sidecar-5476.sock"))
    # The gateway's API socket stays reachable: the in-sandbox MCP servers use it, and
    # a deny there is EPERM, which the client does not fall back from.
    assert not denied(str(crew / dashboard_socket_name(5476)))
    assert not denied("/tmp/mcs-keys.sock.bak") and not denied("/tmp/x/mcs-keys.sock")
    # Only the temp-dir patterns are write-sealed.
    assert host.sealed_socket_patterns == sandbox_plan.credential_agent_socket_patterns(forward)
    profile = sandbox._build_seatbelt_profile("standard", forward_ssh_auth_sock=forward)
    lines = profile.splitlines()
    for pattern in patterns:
        assert _socket_rule(pattern) in lines
    for pattern in host.sealed_socket_patterns:
        assert all(rule in lines for rule in _seal_rules(pattern))
    assert "dashboard-" not in "".join(line for line in lines if "network" in line)
    # The namespace host is not asked for them.
    namespace = sandbox._live_plan_host(SandboxRequest(tier="standard"))
    assert namespace.denied_socket_patterns == () and namespace.sealed_socket_patterns == ()


def _require_unsandboxed_seatbelt() -> None:
    """Skip unless this process can apply a Seatbelt profile (it cannot from inside one)."""
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"):
        pytest.skip("needs macOS sandbox-exec")
    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("already inside a sandbox: sandbox_apply is refused")


def test_the_kernel_refuses_every_name_of_the_sidecar_socket_and_admits_the_gateway() -> None:
    """The rendered rule holds in the kernel. The credential-agent rule is in the same
    profile, so a pattern Seatbelt could not parse would fail the whole apply here; its
    match is pinned by the regex tests above without binding a socket in ``/tmp``."""
    _require_unsandboxed_seatbelt()
    # Short paths: ``sun_path`` is 104 bytes on macOS, and pytest's tmp_path is long.
    # The run-owned short root keeps the dir attributable and reaped (tmpdir_helpers).
    base = tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "sock-", dir=short_tmp_base())
    listeners: list[socket.socket] = []
    try:
        run = os.path.join(base, "run")
        os.mkdir(run)
        sidecar = os.path.join(run, "tunnel-sidecar-5476.sock")
        gateway = os.path.join(base, dashboard_socket_name(5476))
        for path in (sidecar, gateway):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(path)
            listener.listen(4)
        # The deny must hold for every name that reaches the socket, not only the one
        # the pattern spells: a link outside ``run`` and a path relative to it.
        alias = os.path.join(base, "alias.sock")
        os.symlink(sidecar, alias)
        paths = {
            "sidecar": sidecar,
            "symlink": alias,
            "relative": os.path.basename(sidecar),
            "gateway": gateway,
        }
        spellings = tuple(dict.fromkeys((run, os.path.realpath(run))))
        host = PlanHost(
            home=base,
            denied_socket_patterns=sandbox_plan.denied_socket_patterns(
                spellings, keep_ssh_agent=True
            ),
        )
        profile = sandbox_seatbelt.render_seatbelt_profile(
            plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT), host)
        )
        child = (
            "import socket, sys\n"
            "for p in sys.argv[1:]:\n"
            "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "    try:\n"
            "        s.connect(p); print('ok')\n"
            "    except OSError as e:\n"
            "        print(e.errno)\n"
        )
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-I", "-c", child]
            + list(paths.values()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            cwd=run,
        )
        outcome = dict(zip(paths, result.stdout.split(), strict=True))
        # A refused connect() prints its errno; the gateway's socket must still connect.
        assert outcome["gateway"] == "ok"
        assert [name for name in ("sidecar", "symlink", "relative") if outcome[name] == "ok"] == []
    finally:
        for listener in listeners:
            listener.close()
        shutil.rmtree(base, ignore_errors=True)


def test_the_kernel_refuses_connecting_renaming_and_linking_a_credential_agent_socket() -> None:
    """A uniquely named ``/tmp/mcs-*.sock`` is unreachable under both ``/tmp`` spellings,
    and cannot be renamed or hard-linked to a name the connect deny does not match."""
    _require_unsandboxed_seatbelt()
    tag = f"kctest-{os.getpid()}"
    mcs = f"/tmp/mcs-{tag}.sock"
    control = f"/tmp/kc-{tag}-control.sock"
    moved = f"/tmp/kc-{tag}-moved.sock"
    linked = f"/tmp/kc-{tag}-linked.sock"
    listeners: list[socket.socket] = []
    try:
        for path in (mcs, control):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(path)
            listener.listen(4)
        host = PlanHost(
            home=tempfile.gettempdir(),
            denied_socket_patterns=sandbox_plan.denied_socket_patterns((), keep_ssh_agent=False),
            sealed_socket_patterns=sandbox_plan.credential_agent_socket_patterns(False),
        )
        profile = sandbox_seatbelt.render_seatbelt_profile(
            plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT), host)
        )
        child = (
            "import os, socket, sys\n"
            "mcs, private, control, moved, linked = sys.argv[1:]\n"
            "def connect(p):\n"
            "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "    try:\n"
            "        s.connect(p); return 'ok'\n"
            "    except OSError as e:\n"
            "        return str(e.errno)\n"
            "def attempt(f, *a):\n"
            "    try:\n"
            "        f(*a); return 'ok'\n"
            "    except OSError as e:\n"
            "        return str(e.errno)\n"
            "print(connect(mcs), connect(private), connect(control),\n"
            "      attempt(os.link, mcs, linked), attempt(os.rename, mcs, moved),\n"
            "      connect(moved), connect(linked))\n"
        )
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-I", "-c", child]
            + [mcs, "/private" + mcs, control, moved, linked],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        names = ("tmp", "private", "control", "link", "rename", "moved", "linked")
        outcome = dict(zip(names, result.stdout.split(), strict=True))
        # The control socket proves the profile does not refuse every connect.
        assert outcome["control"] == "ok"
        assert [n for n in ("tmp", "private", "link", "rename") if outcome[n] == "ok"] == []
        assert os.path.exists(mcs) and not os.path.lexists(moved)
        assert not os.path.lexists(linked)
    finally:
        for listener in listeners:
            listener.close()
        for path in (mcs, control, moved, linked):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def test_the_kernel_lets_realpath_reach_an_exposed_file_and_keeps_its_siblings_hidden(
    tmp_path: Path,
) -> None:
    """A file carved out of a Seatbelt mask canonicalizes inside the sandbox, as a Node
    CLI's ``realpath`` of its ``--mcp-config`` does at boot, while the masked directory
    stays unlistable and a sibling in it stays unreadable."""
    _require_unsandboxed_seatbelt()
    base = os.path.realpath(tmp_path)
    masked = os.path.join(base, "auth-store-staging")
    sub = os.path.join(masked, "sub")
    os.makedirs(sub)
    exposed = os.path.join(sub, "strands-mcp-1-x.json")
    sibling = os.path.join(masked, "token_signing.key")
    for path in (exposed, sibling):
        Path(path).write_text("{}", encoding="utf-8")
    request = SandboxRequest(
        tier="standard",
        backend=BACKEND_SEATBELT,
        extra_hidden_dirs=(masked,),
        extra_expose_files=(exposed,),
    )
    profile = sandbox_seatbelt.render_seatbelt_profile(
        plan_confinement(request, PlanHost(home=base))
    )
    child = (
        "import os, sys\n"
        "exposed, masked, sibling = sys.argv[1:]\n"
        "def attempt(f, *a):\n"
        "    try:\n"
        "        f(*a); return 'ok'\n"
        "    except OSError as e:\n"
        "        return str(e.errno)\n"
        "print(attempt(lambda p: os.path.realpath(p, strict=True), exposed))\n"
        "print(attempt(lambda p: open(p).read(), exposed))\n"
        "print(attempt(os.listdir, masked))\n"
        "print(attempt(lambda p: open(p).read(), sibling))\n"
    )
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-I", "-c", child]
        + [exposed, masked, sibling],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    realpath, read, listing, sibling_read = result.stdout.split()
    assert (realpath, read) == ("ok", "ok")
    assert listing != "ok" and sibling_read != "ok"
