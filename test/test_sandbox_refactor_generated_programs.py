"""The two programs the sandbox writes out are the bytes they were before the split.

The Linux namespace launcher (``_build_launcher_script``) and the macOS Seatbelt profile
(``_build_seatbelt_profile``) are generated text, and a mask, seal or carve-out lives in
that text: one reordered block or one dropped path changes what the agent can reach, and
nothing else in the gateway would notice. So each builder is pinned here byte for byte,
as a SHA-256 of its output for a matrix of inputs that covers every tier and every kind
of extra path the callers pass, with every host input pinned so the digest does not
depend on the machine that computes it.

The digests were recorded from the builders as they stood before they moved into
``kiro_crew.sandbox_launcher`` and ``kiro_crew.sandbox_seatbelt``. The move changed
exactly one line of the generated launcher: its docstring spelled the product name as
one word, which the brand gate refuses on a line a change adds, and it now reads
``Kiro Crew``. Every digest is therefore taken with that one line restored, and a
separate case pins that the new spelling is present exactly once.

The host inputs are pinned at names that stay in ``kiro_crew.sandbox`` and are read
there or through it at call time: ``config_dir``, ``kiro_agents_dir``,
``carveout_chain_has_planted_link``, the voice-runtime path cache and
``_ssh_supports_accept_new``, plus ``Path.home``, ``HOME``, the pod variables and the
process ids. The run root is ``tmp_path`` and is folded to ``<ROOT>`` before hashing.
Every table whose entries reach either program -- the tier lists, the crew-home leaf
tables, the cc files, the pod sub-leaves and the environment lists -- is replaced by the
small stand-in in ``_PINNED_TABLES``, so adding a leaf or a directory to a real table
leaves these digests alone. A change to either program's text does move them; it
updates them in the same commit, from the ``actual`` digest each failing case reports.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew import sandbox

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="both programs are POSIX-only: the launcher reads os.getuid and the paths are POSIX",
)

#: The launcher docstring line as it stood before the split, and as it reads now.
_OLD_DOC_LINE = '"""Namespace sandbox launcher — spawned by KiroCrew.'  # brand-ok: pre-split text
_NEW_DOC_LINE = '"""Namespace sandbox launcher — spawned by Kiro Crew.'

_UID, _GID = 4242, 4343

#: Small stand-ins for every table whose entries reach either program, so a leaf, a
#: directory or an environment name added to the real tables leaves these digests alone.
#: Each keeps the shapes the real one has: nested leaves, both data-home spellings, the
#: policy cache and voice-runtime directories, a tier-only directory, an exposed file.
_HIDDEN_LEAVES = (
    ".env",
    "diag",
    "apps/aws-control/data",
    "workspace/md-notebook/pat",
    "workspace/md-notebook/vaults.json",
    "live_target.json",
    "crew-panels",
)
_READONLY_LEAVES = (
    "subagents",
    "security_policy.json",
    "profiles",
    "apps/.dev-grants.json",
    "mcp-launch-approvals",
    "mcp/resolved",
)
_CREW_SPELLINGS = (".kiro/crew", ".kirocrew")
_TIER_ONLY = {"strict": (".aws", ".config/gh", ".kube"), "cc": (".aws", ".kube")}
_SHARED_DIRS = (
    ".kiro/crew-auth-staging",
    ".gnupg",
    ".config/gcloud",
    *(f"{home}/{leaf}" for home in _CREW_SPELLINGS for leaf in (".vault", "policy_cache")),
    *(f"{home}/run/voice-runtime" for home in _CREW_SPELLINGS),
    *(f"{home}/{leaf}" for home in _CREW_SPELLINGS for leaf in _HIDDEN_LEAVES),
)
_PINNED_TABLES: dict[str, object] = {
    "_CREW_HIDDEN_LEAVES": _HIDDEN_LEAVES,
    "_CREW_READONLY_LEAVES": _READONLY_LEAVES,
    "_CREW_READONLY_TARGETS": [
        f"{home}/{leaf}" for home in _CREW_SPELLINGS for leaf in _READONLY_LEAVES
    ],
    "_CREW_UNREADABLE_MASK_LEAVES": frozenset({"live_target.json"}),
    "_STRICT_DIRS": [*_SHARED_DIRS, *_TIER_ONLY["strict"]],
    "_STANDARD_DIRS": list(_SHARED_DIRS),
    "_CC_DIRS": [*_SHARED_DIRS, *_TIER_ONLY["cc"]],
    "_CC_FILES": [".npmrc", ".netrc", ".kiro/crew/.env", ".kirocrew/.env"],
    "_CC_EXPOSE_FILES": [".aws/config"],
    "_POD_OS_HOME_MASKED_SUBLEAVES": (".aws/config", ".aws/credentials"),
    "_AGENT_DENIED_ENV_KEYS": ["SLACK_BOT_TOKEN", "JIRA_TOKEN_", "KIROCREW_POLICY_URL"],
    "_SENSITIVE_ENV_PREFIXES": ["AWS_SECRET", "SSH_AUTH_SOCK", "GIT_ASKPASS"],
    "_PYTHON_ENV_PREFIXES": ["PYTHONPATH", "PYTHONHOME"],
}


class _Host:
    """The pinned host a case renders against."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.crew = root / "crew"
        self.home.mkdir()
        self.crew.mkdir()

    def voice_cache(
        self, crew: Path
    ) -> tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        run = crew / "run"
        root = run / "voice-runtime"
        return (str(crew), str(root), (str(root),), (str(run),), (str(run), str(crew)))


def _pinned_host(root: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Pin every host input either builder reads under ``root``."""
    pinned = _Host(root.resolve())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pinned.home))
    monkeypatch.setenv("HOME", str(pinned.home))
    for name in ("KIROCREW_POD", "KIROCREW_OS_HOME", "KIRO_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "config_dir", lambda: pinned.crew)
    monkeypatch.setattr(sandbox, "kiro_agents_dir", lambda: pinned.home / ".kiro" / "agents")
    monkeypatch.setattr(sandbox, "carveout_chain_has_planted_link", lambda _path: False)
    monkeypatch.setattr(sandbox, "_voice_runtime_paths_cache", pinned.voice_cache(pinned.crew))
    monkeypatch.setattr(os, "getuid", lambda: _UID, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: _GID, raising=False)
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
    for name, table in _PINNED_TABLES.items():
        monkeypatch.setattr(sandbox, name, table)
    assert type(sandbox._sandbox_policy()).__name__ == "DefaultSandboxPolicy"
    return pinned


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    return _pinned_host(tmp_path, monkeypatch)


def _default_home(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    crew = host.home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: crew)
    monkeypatch.setattr(sandbox, "_voice_runtime_paths_cache", host.voice_cache(crew))


def _symlinked_home(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    lexical = host.root / "crew-link"
    lexical.symlink_to(host.crew, target_is_directory=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: lexical)
    lex_run, can_run = lexical / "run", host.crew / "run"
    lex_vr, can_vr = lex_run / "voice-runtime", can_run / "voice-runtime"
    monkeypatch.setattr(
        sandbox,
        "_voice_runtime_paths_cache",
        (
            str(lexical),
            str(can_vr),
            (str(lex_vr), str(can_vr)),
            (str(lex_run), str(can_run)),
            (str(lex_run), str(lexical), str(can_run), str(host.crew)),
        ),
    )


def _pod_home(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.setenv("KIROCREW_OS_HOME", str(host.root / "podhome"))


def _no_accept_new(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: False)


def _planted_notebook_chain(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    crew = str(host.crew)
    monkeypatch.setattr(
        sandbox, "carveout_chain_has_planted_link", lambda p: str(p).startswith(crew)
    )


def _carveout(host: _Host) -> dict[str, Any]:
    probe = host.crew / "run" / "mcp-tmp" / "probe-x"
    probe.mkdir(parents=True)
    return {"extra_writable_dirs": (str(probe),)}


def _refused_carveouts(host: _Host) -> dict[str, Any]:
    return {
        "extra_writable_dirs": (
            str(host.crew / "run"),
            str(host.crew / "run" / "voice-runtime"),
            "relative/dir",
            str(host.root / "missing"),
        )
    }


def _hidden_and_visible(host: _Host) -> dict[str, Any]:
    exposed = host.crew / "run" / "exposed"
    inside = exposed / "inside"
    inside.mkdir(parents=True)
    return {
        "extra_hidden_dirs": (str(exposed),),
        "extra_visible_dirs": (str(exposed), str(host.crew / "policy_cache")),
        "extra_writable_dirs": (str(inside),),
    }


def _private_windows(host: _Host) -> dict[str, Any]:
    apps = host.crew / "apps"
    window = apps / "alpha" / "data"
    window.mkdir(parents=True)
    return {
        "extra_hidden_dirs": (str(apps),),
        "extra_private_dirs": (str(window), str(apps), str(host.crew / "elsewhere")),
        "extra_private_dir_ids": ((str(window), 11, 12),),
        "extra_hidden_dir_ids": ((str(apps), 13, 14),),
    }


def _identities(host: _Host) -> dict[str, Any]:
    masked = host.crew / "apps"
    return {
        "extra_hidden_dirs": (str(masked),),
        "extra_alias_credential_ids": ((7, 99), (7, 99), (8, 1)),
        "fail_closed_file_masks": (
            (str(host.home / ".npmrc"), 3, 4),
            (str(host.home / ".npmrc"), 3, 4),
        ),
        "required_mask_targets": (
            str(masked / "alpha"),
            str(host.root / "outside"),
            str(masked / "alpha"),
        ),
        "mask_occupants": {
            str(masked): (21, 22, 0),
            str(host.home / ".aws"): (23, 24, 1, 1, 25, 26),
        },
    }


def _crew_home_alias(host: _Host) -> dict[str, Any]:
    """``$HOME/.kiro/crew`` is the data home reached through a link: every path under
    it is folded onto the resolved spelling, and the alias travels with its identity."""
    return {"crew_home_aliases": ((str(host.home / ".kiro" / "crew"), str(host.crew), 31, 32),)}


def _expose(host: _Host) -> dict[str, Any]:
    return {"extra_expose_files": (str(host.home / ".aws" / "sso" / "cache" / "token.json"),)}


_Setup = Callable[[_Host, pytest.MonkeyPatch], None]
_Kwargs = Callable[[_Host], dict[str, Any]]

#: case -> (tier, host setup or None, builder keyword arguments or None).
_LAUNCHER_CASES: dict[str, tuple[str, _Setup | None, _Kwargs | None]] = {
    "strict": ("strict", None, None),
    "standard": ("standard", None, None),
    "cc": ("cc", None, None),
    "strict-default-home": ("strict", _default_home, None),
    "standard-default-home": ("standard", _default_home, None),
    "strict-symlinked-home": ("strict", _symlinked_home, None),
    "strict-no-accept-new": ("strict", _no_accept_new, None),
    "strict-python-env-ssh-sock": (
        "strict",
        None,
        lambda h: {"strip_python_env": True, "forward_ssh_auth_sock": True},
    ),
    "cc-python-env-ssh-sock": (
        "cc",
        None,
        lambda h: {"strip_python_env": True, "forward_ssh_auth_sock": True},
    ),
    "strict-pod": ("strict", _pod_home, None),
    "standard-pod": ("standard", _pod_home, None),
    "standard-notebook-chain": ("standard", _planted_notebook_chain, None),
    "standard-carveout": ("standard", None, _carveout),
    "strict-carveout": ("strict", None, _carveout),
    "cc-carveout": ("cc", None, _carveout),
    "standard-refused-carveouts": ("standard", None, _refused_carveouts),
    "standard-hidden-and-visible": ("standard", None, _hidden_and_visible),
    "cc-private-windows": ("cc", None, _private_windows),
    "strict-identities": ("strict", None, _identities),
    "strict-crew-home-alias": ("strict", None, _crew_home_alias),
    "cc-expose": ("cc", None, _expose),
    "strict-expose": ("strict", None, _expose),
}

#: Seatbelt cases. Never two exposed files under one tier directory: the profile
#: orders those clauses by iterating a set, so their order follows the string hash.
_PROFILE_CASES: dict[str, tuple[str, _Setup | None, _Kwargs | None]] = {
    "strict": ("strict", None, None),
    "standard": ("standard", None, None),
    "cc": ("cc", None, None),
    "strict-default-home": ("strict", _default_home, None),
    "strict-symlinked-home": ("strict", _symlinked_home, None),
    "strict-pod": ("strict", _pod_home, None),
    "standard-notebook-chain": ("standard", _planted_notebook_chain, None),
    "standard-carveout": ("standard", None, _carveout),
    "standard-refused-carveouts": ("standard", None, _refused_carveouts),
    "standard-hidden-and-visible": ("standard", None, _hidden_and_visible),
    "cc-private-windows": (
        "cc",
        None,
        lambda h: {k: v for k, v in _private_windows(h).items() if not k.endswith("_ids")},
    ),
    "strict-expose": ("strict", None, _expose),
}

_LAUNCHER_DIGESTS: dict[str, str] = {
    "cc": "17e38235a1ec1a10eefe133a7fc78a931a7bdc0524d134e070dd76036a0c93f7",
    "cc-carveout": "dd09d10bf3f57690946869e7b625e77b5c23fe19170161c2dd219c1f99ad8265",
    "cc-expose": "ef5094d3e25af43b51cd9f9ad12cbfd3e9b9012f032920dfa8ea1e44ed7ab607",
    "cc-private-windows": "e819912153ed873385febcf62cdd2ae6f35211bd972c8f9986085261523cd9ef",
    "cc-python-env-ssh-sock": "ae907e8484a7b1208ff3d9eaabc8d1bfec1f60d43ada3ed3ee9df1e465bf52ea",
    "standard": "61f3f7e63cc7b86d4c0de2cd793fb251fae83c5efb8eab3cee3afcf326f6b53a",
    "standard-carveout": "e4bd416fb54d89f417f4d55584da8bd02d34020eae8bf165f11c062108600687",
    "standard-default-home": "fbd2a34a4125f4b91b54d9fa770e873e12eaa7bd715c430b1f173940a8b7d081",
    "standard-hidden-and-visible": "642f919317a435c6e4c1de23562242fe180fbdd7102e756ae99ac25a9f9a4305",
    "standard-notebook-chain": "d0d8b7c85fbe9d9d6b3fe93571a35737e845a256e5a3a645b06f746b39156f83",
    "standard-pod": "5d063a24c633f5aa29006aca4a556492867738a497df74fecda4a2f9fb765974",
    "standard-refused-carveouts": "61f3f7e63cc7b86d4c0de2cd793fb251fae83c5efb8eab3cee3afcf326f6b53a",
    "strict": "f033639b6b1b27c8501d9d36a46a600bd086009aa6bb76f85dcfc55ef9ac231d",
    "strict-carveout": "286d2d13b13008b51076d854be7c16c267341277e20b8706026f01452f65c2b8",
    "strict-crew-home-alias": "5f3f239a17b0cff8a457f128f3cc6f049092b2f2d2ca4059e4e3ee96747dcf1c",
    "strict-default-home": "e4355e3bd7cc5dfb7f59d24eea3a08e8ea286832fee35629c0c76866bc53650c",
    "strict-expose": "9cf67920d331c57cd335e50c98fc5965fa29cc3d01f9aba0965fb11ae2774796",
    "strict-identities": "35699a92929e74a82ecd7962f05192c5be8708315c207aa0f07ac2f80475918f",
    "strict-no-accept-new": "301ea1cb15af6188d321d1a674a49d36032c951905f0cd9b145030faffbf7177",
    "strict-pod": "9dbb4bd57ba5f9c438052ad8dd87448bb7b2f64e3154242e35214fe9497b1b46",
    "strict-python-env-ssh-sock": "f0385266ea4394f95a2f40dbeff19c14b59379d6bf5dcfd64cef501334f177e4",
    "strict-symlinked-home": "5c1a03c9754c1cd6a773b1630f9f20029b06d3046ea530e5caeb2967b849524b",
}

_PROFILE_DIGESTS: dict[str, str] = {
    "cc": "ff0989ba3f35397dfe5e6c724ea13195e59a4d4f20e7358ecb9d88de3688d5d1",
    "cc-private-windows": "b17f2c3c9a92af197cd21703742755c7b7c904a935c9e7ef68d61044568a5647",
    "standard": "7ff81e62faba059966ec6643c7bfdbb71985d2eacc03b03c07e46a110e250669",
    "standard-carveout": "2afae4a5c908df82ca0f8abc638bad8564b733d260baf08a750b12dcf427d6f2",
    "standard-hidden-and-visible": "532b032826e530f431ce4e0fc498b0ed2f489f22af97eb7c541a171d57b5a532",
    "standard-notebook-chain": "1f086f06f4fb14366ed2e3d5759e6e9781c3a21011d1c66f0127cef9a7a7bea3",
    "standard-refused-carveouts": "7ff81e62faba059966ec6643c7bfdbb71985d2eacc03b03c07e46a110e250669",
    "strict": "50052d925aa857ae41952ae26c26225d41ff0168d49bc8891917b8da85ac4575",
    "strict-default-home": "3c51ebb93ba728e96e14bc9101a7f7e78b06f0792aafbf126e4589d58143f6b9",
    "strict-expose": "0deb9f6ec67d200bedcfea797020ff534b8d8113ee0b13f2efb39fc50ee94296",
    "strict-pod": "5fb789a232c2b277c22c9d79fc1b76e8d357925ee3ecd7d0dd947c228f5dd001",
    "strict-symlinked-home": "d74d60d65019a1cddf8aa9add915c24a394c6bd88149733760e452527cf1c010",
}


def _render(
    builder: Callable[..., str],
    case: tuple[str, _Setup | None, _Kwargs | None],
    host: _Host,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    tier, setup, kwargs = case
    if setup is not None:
        setup(host, monkeypatch)
    return builder(tier, **(kwargs(host) if kwargs else {}))


def _digest(text: str, host: _Host) -> str:
    folded = text.replace(str(host.root), "<ROOT>")
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()


def _launcher_digest(case: str, host: _Host, monkeypatch: pytest.MonkeyPatch) -> str:
    script = _render(sandbox._build_launcher_script, _LAUNCHER_CASES[case], host, monkeypatch)
    return _digest(script.replace(_NEW_DOC_LINE, _OLD_DOC_LINE), host)


def _profile_digest(case: str, host: _Host, monkeypatch: pytest.MonkeyPatch) -> str:
    profile = _render(sandbox._build_seatbelt_profile, _PROFILE_CASES[case], host, monkeypatch)
    return _digest(profile, host)


@pytest.mark.parametrize("case", sorted(_LAUNCHER_CASES))
def test_the_launcher_is_the_pre_split_bytes(
    case: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = _launcher_digest(case, host, monkeypatch)
    assert actual == _LAUNCHER_DIGESTS[case], f"{case}: actual {actual}"


@pytest.mark.parametrize("case", sorted(_PROFILE_CASES))
def test_the_seatbelt_profile_is_the_pre_split_bytes(
    case: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = _profile_digest(case, host, monkeypatch)
    assert actual == _PROFILE_DIGESTS[case], f"{case}: actual {actual}"


def test_the_digests_cover_every_case() -> None:
    assert set(_LAUNCHER_DIGESTS) == set(_LAUNCHER_CASES)
    assert set(_PROFILE_DIGESTS) == set(_PROFILE_CASES)


def test_the_digests_see_the_inputs_they_pin(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned input that stopped reaching the output would leave its cases passing
    on a builder that ignores it, so each family must move the digest."""
    digests = {case: _launcher_digest(case, host, monkeypatch) for case in ("strict",)}
    for other in (
        "standard",
        "cc",
        "strict-no-accept-new",
        "strict-identities",
        "strict-crew-home-alias",
    ):
        with pytest.MonkeyPatch.context() as scoped:
            digests[other] = _launcher_digest(other, host, scoped)
    assert len(set(digests.values())) == len(digests)


@pytest.mark.parametrize("tier", ["strict", "standard", "cc"])
def test_the_launcher_stages_seals_hides_then_carves(tier: str, host: _Host) -> None:
    """Private windows are staged, READONLY dirs sealed, SENSITIVE dirs hidden and the
    write carve-outs applied, in that order, in every tier."""
    script = sandbox._build_launcher_script(tier)
    order = [
        "for p in PRIVATE_DIRS:",
        "for d in READONLY_DIRS:",
        "for d in SENSITIVE_DIRS:",
        "for d in WRITABLE_DIRS:",
    ]
    positions = [script.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert all(script.count(marker) == 1 for marker in order)


def test_the_launcher_names_the_product_once_in_two_words(host: _Host) -> None:
    script = sandbox._build_launcher_script("strict")
    assert script.count(_NEW_DOC_LINE) == 1
    assert _OLD_DOC_LINE not in script


# --------------------------------------------------------------------------- #
# Each builder reads the plan it renders from kiro_crew.sandbox when it runs.
# --------------------------------------------------------------------------- #


class _Reached(BaseException):
    """Raised by a stub to prove the builder called the name it replaced.

    A ``BaseException`` because several of the helpers are best-effort and swallow
    ``Exception``, which would let a patch that MISSED read as one that landed.
    """


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


def _visible_policy_cache(host: _Host) -> dict[str, Any]:
    cache = host.crew / "policy_cache"
    return {"extra_hidden_dirs": (str(cache),), "extra_visible_dirs": (str(cache),)}


#: Helpers the launcher builder calls, with the arguments that make it reach each.
_LAUNCHER_CALLS: dict[str, Callable[[_Host], dict[str, Any]] | None] = {
    "_agent_scrub_prefixes": None,
    "_fold_crew_home_alias": None,
    "_hidden_path_contains_visible_path": None,
    "_is_policy_cache_dir": _visible_policy_cache,
    "_md_notebook_degraded_mask_dirs": None,
    "_pod_os_home_targets": None,
    "_private_window_spellings": None,
    "_relocated_crew_targets": None,
    "_relocated_policy_cache_dirs": None,
    "_resolved_kiro_agents_targets": None,
    "_sandbox_policy": None,
    "_ssh_supports_accept_new": None,
    "_voice_runtime_parent_paths": None,
    "_voice_runtime_sandbox_paths": None,
    "_writable_carveout_spellings": None,
}

#: Helpers the Seatbelt builder calls.
_PROFILE_CALLS: dict[str, Callable[[_Host], dict[str, Any]] | None] = {
    "_crew_hidden_sandbox_targets": None,
    "_hidden_path_contains_visible_path": None,
    "_is_policy_cache_dir": None,
    "_is_voice_runtime_dir": None,
    "_md_notebook_degraded_mask_dirs": None,
    "_pod_os_home_targets": None,
    "_private_window_spellings": None,
    "_relocated_crew_targets": None,
    "_relocated_policy_cache_dirs": None,
    "_resolved_kiro_agents_targets": None,
    "_sandbox_policy": None,
    "_voice_runtime_ancestor_guards": None,
    "_voice_runtime_parent_paths": None,
    "_voice_runtime_sandbox_paths": None,
    "_writable_carveout_spellings": None,
}


@pytest.mark.parametrize("name", sorted(_LAUNCHER_CALLS))
def test_a_helper_patched_on_the_sandbox_reaches_the_launcher(
    name: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _LAUNCHER_CALLS[name]
    arguments = kwargs(host) if kwargs else {}
    monkeypatch.setattr(sandbox, name, _raiser(name))
    with pytest.raises(_Reached, match=name):
        sandbox._build_launcher_script("strict", **arguments)


@pytest.mark.parametrize("name", sorted(_PROFILE_CALLS))
def test_a_helper_patched_on_the_sandbox_reaches_the_seatbelt_profile(
    name: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _PROFILE_CALLS[name]
    arguments = kwargs(host) if kwargs else {}
    monkeypatch.setattr(sandbox, name, _raiser(name))
    with pytest.raises(_Reached, match=name):
        sandbox._build_seatbelt_profile("strict", **arguments)


#: Tables each builder renders, as (name, tier, builder keyword arguments, the probe
#: value, and the text the probe must put into the output).
_TABLES: list[tuple[str, str, dict[str, Any], object, str]] = [
    ("_STANDARD_DIRS", "standard", {}, [".b10-probe-dir"], "/.b10-probe-dir"),
    ("_CC_FILES", "cc", {}, [".b10-probe-file"], "/.b10-probe-file"),
    ("_CREW_HIDDEN_LEAVES", "strict", {}, ("b10-probe-hidden",), "/crew/b10-probe-hidden"),
    ("_CREW_READONLY_LEAVES", "strict", {}, ("b10-probe-ro",), "/crew/b10-probe-ro"),
    ("_CREW_READONLY_TARGETS", "strict", {}, [".b10-probe-ceiling"], "/.b10-probe-ceiling"),
    ("_CC_EXPOSE_FILES", "cc", {}, [".gnupg/b10-probe-expose"], "/.gnupg/b10-probe-expose"),
]

#: Tables only the launcher renders into its output: the environment scrub lists and the
#: leaves whose Linux mask refuses the read.
_LAUNCHER_ONLY_TABLES: list[tuple[str, str, dict[str, Any], object, str]] = [
    ("_SENSITIVE_ENV_PREFIXES", "standard", {}, ("B10_PROBE_SENSITIVE_",), "B10_PROBE_SENSITIVE_"),
    ("_AGENT_DENIED_ENV_KEYS", "strict", {}, ("B10_PROBE_DENIED",), "B10_PROBE_DENIED"),
    (
        "_CREW_UNREADABLE_MASK_LEAVES",
        "strict",
        {},
        frozenset({"b10-probe-unreadable"}),
        "b10-probe-unreadable",
    ),
    (
        "_PYTHON_ENV_PREFIXES",
        "standard",
        {"strip_python_env": True},
        ("B10_PROBE_PY_",),
        "B10_PROBE_PY_",
    ),
]


@pytest.mark.parametrize(
    ("name", "tier", "kwargs", "value", "needle"),
    _TABLES + _LAUNCHER_ONLY_TABLES,
    ids=[row[0] for row in _TABLES + _LAUNCHER_ONLY_TABLES],
)
def test_a_table_patched_on_the_sandbox_reaches_the_launcher(
    name: str,
    tier: str,
    kwargs: dict[str, Any],
    value: object,
    needle: str,
    host: _Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert needle not in sandbox._build_launcher_script(tier, **kwargs)
    monkeypatch.setattr(sandbox, name, value)
    assert needle in sandbox._build_launcher_script(tier, **kwargs)


@pytest.mark.parametrize(
    ("name", "tier", "kwargs", "value", "needle"), _TABLES, ids=[row[0] for row in _TABLES]
)
def test_a_table_patched_on_the_sandbox_reaches_the_seatbelt_profile(
    name: str,
    tier: str,
    kwargs: dict[str, Any],
    value: object,
    needle: str,
    host: _Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert needle not in sandbox._build_seatbelt_profile(tier, **kwargs)
    monkeypatch.setattr(sandbox, name, value)
    assert needle in sandbox._build_seatbelt_profile(tier, **kwargs)


@pytest.mark.parametrize("tier", ["strict", "standard", "cc"])
def test_namespace_argv_writes_exactly_what_the_builder_renders(
    tier: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file the child runs is the builder's text, and the argv invokes it isolated."""
    rendered: list[str] = []
    real = sandbox._build_launcher_script

    def spy(*args: Any, **kwargs: Any) -> str:
        rendered.append(real(*args, **kwargs))
        return rendered[-1]

    monkeypatch.setattr(sandbox, "_build_launcher_script", spy)
    monkeypatch.setattr(sandbox, "_resolve_agent_executable", lambda executable: executable)
    argv = sandbox.namespace_argv(["/bin/true", "--flag"], tier)
    assert len(rendered) == 1
    assert argv[:3] == [sys.executable, "-I", "-S"]
    assert argv[4:] == ["/bin/true", "--flag"]
    launcher = Path(argv[3])
    assert launcher.parent == host.crew / "run"
    assert launcher.name.startswith(f"kirocrew_sandbox_{os.getpid()}_")
    assert launcher.read_bytes() == rendered[0].encode("utf-8")
