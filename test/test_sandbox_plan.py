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
import sys
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import sandbox, sandbox_plan, sandbox_seatbelt
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


# --------------------------------------------------------------------------- #
# The places the two backends differ, as declared capabilities.
# --------------------------------------------------------------------------- #


def test_the_capabilities_are_declared_per_renderer() -> None:
    caps = sandbox_plan.CAPABILITIES
    assert set(caps) == {BACKEND_NAMESPACE, BACKEND_SEATBELT}
    assert caps[BACKEND_SEATBELT].cc_unmaskable_dirs == frozenset({".aws"})
    assert caps[BACKEND_NAMESPACE].cc_unmaskable_dirs == frozenset()


def test_seatbelt_cannot_partially_expose_aws_so_cc_leaves_it_visible() -> None:
    linux = _plan("cc", BACKEND_NAMESPACE)
    macos = _plan("cc", BACKEND_SEATBELT)
    assert f"{HOME}/.aws" in linux.sensitive_dirs
    assert f"{HOME}/.aws" not in [m.path for m in macos.masks]
    assert _plan("strict", BACKEND_SEATBELT).masks[0].path == f"{HOME}/.aws"


#: For each declared capability: the tier and request that exercise it, and a value it
#: takes on each backend that is not that backend's own.
_CAPABILITY_FLIPS: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {
    "cc_unmaskable_dirs": (
        "cc",
        {},
        {BACKEND_NAMESPACE: frozenset({".aws"}), BACKEND_SEATBELT: frozenset()},
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
    assert payload["stand_in_roots"] == ["/run/user/1000", "/dev/shm"]


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
# The gateway-side question: may a sandboxed child be denied this path?
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("layout", ["default", "pod home", "relocated home", "degraded notebook"])
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("tier", ["standard", "cc", "strict"])
def test_sandbox_may_hide_path_covers_every_tree_a_plan_masks(
    live_host: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    layout: str,
    backend: str,
    tier: str,
) -> None:
    """A gateway read that stands in for a sandboxed child's (the native persona
    snapshot) must never call visible what a plan on this host denies: each live mask,
    masked file and ``~/.ssh``, and a file inside each, for every tier, renderer and
    layout the planner re-anchors."""
    if layout == "pod home":
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(tmp_path / "pod-home"))
    elif layout == "relocated home":
        relocated = tmp_path / "srv" / "crew"
        relocated.mkdir(parents=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
    elif layout == "degraded notebook":
        degraded = str(live_host / ".kiro" / "crew" / "notebook-state")
        monkeypatch.setattr(sandbox, "_md_notebook_degraded_mask_dirs", lambda: [degraded])
    plan = sandbox._spawn_plan(backend, tier)
    denied = [*plan.sensitive_files, *([plan.ssh_dir] if plan.hide_ssh else [])]
    assert denied
    for tree in denied:
        assert sandbox.sandbox_may_hide_path(tree), tree
        assert sandbox.sandbox_may_hide_path(os.path.join(tree, "persona.md")), tree


def test_sandbox_may_hide_path_leaves_readable_trees_visible(live_host: Path) -> None:
    """An ordinary file, the kiro agents tree and a crew app's own data are readable in
    every tier (the agents tree is sealed read-only, not masked)."""
    for path in (
        live_host / "notes" / "persona.md",
        live_host / ".kiro" / "agents" / "prompts" / "persona.md",
        live_host / ".kiro" / "crew" / "apps" / "demo" / "data" / "persona.md",
    ):
        assert not sandbox.sandbox_may_hide_path(str(path)), path


def test_sandbox_may_hide_path_resolves_links_on_both_sides(
    live_host: Path, tmp_path: Path
) -> None:
    """A mask over a directory hides it under every name: a visible link into a masked
    tree, and the real spelling of a masked tree that is itself a link."""
    secret = live_host / ".gnupg" / "persona.md"
    secret.parent.mkdir()
    secret.write_text("persona", encoding="utf-8")
    link = live_host / "notes" / "persona.md"
    link.parent.mkdir()
    link.symlink_to(secret)
    assert sandbox.sandbox_may_hide_path(str(link))
    volume = tmp_path / "volume" / "aws"
    volume.mkdir(parents=True)
    (live_host / ".aws").symlink_to(volume)
    assert sandbox.sandbox_may_hide_path(str(volume / "persona.md"))


def test_sandbox_may_hide_path_reports_hidden_when_the_host_cannot_be_read(
    live_host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never raises, and an unreadable host answers "hidden": the caller then sends
    its block, which costs a repeat where the other answer could cost the persona."""

    def _broken() -> object:
        raise RuntimeError("no platform context")

    monkeypatch.setattr(sandbox, "_sandbox_policy", _broken)
    assert sandbox.sandbox_may_hide_path(str(live_host / "notes" / "persona.md"))


def test_sandbox_may_hide_path_changes_nothing_and_asks_no_ssh(
    live_host: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It reads no more than the mask builders: the voice runtime is named from the data
    home -- here relocated, so no ``$HOME`` entry covers it -- and never primed (priming
    creates it), and the host ssh is never asked."""

    def _forbidden() -> bool:
        raise AssertionError("the question needs no ssh probe")

    relocated = tmp_path / "srv" / "crew"
    relocated.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", _forbidden)
    monkeypatch.setattr(sandbox, "_voice_runtime_paths_cache", None)
    voice = relocated / "run" / "voice-runtime"
    assert sandbox.sandbox_may_hide_path(str(voice / "socket"))
    assert not sandbox.sandbox_may_hide_path(str(live_host / "notes" / "persona.md"))
    assert sandbox._voice_runtime_paths_cache is None
    assert not (relocated / "run").exists()
