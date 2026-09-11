"""One declaration per harness is the only place an auth fact may live.

Four host controls have to name the same file for a harness to be both fenced and
able to authenticate: the read-gate floor spells the credential leaf, the sandbox
mask spells the one leaf to spare, a membership set spells whether a host logout
may retire a running child, and the panel spells the sign-in advice. While each
was hand-maintained, a harness became selectable with its live OAuth token off the
floor -- the list that was supposed to carry it was edited somewhere else.

``agent_sdk.host_auth`` makes that one table and the four controls projections of
it. These tests pin the properties that make the projection trustworthy, and each
one fails for a different reason:

* a selectable harness with no declaration is a harness with no auth answer, so
  the parity gate must fail when the known set grows without the table growing;
* the table is read from inside the credential floor's own construction, so it
  must stay a stdlib-only leaf or the floor cannot read it without a cycle;
* each projection must still equal the table it replaced, and be reachable from
  the consumer that actually reads it -- a correct table nobody reads fences
  nothing;
* a driver must not be able to unmask itself, which is the one asymmetry the
  declaration's own validation exists to enforce;
* an unnamed harness must fail CLOSED -- fence nothing extra, and never have a
  live turn ended over a store there is no evidence it reads;
* an in-product sign-in flow is optional, and its absence has to be visible as a
  shape rather than as a flag a driver sets and then no-ops; and
* the resolved-root cache key must change whenever a declared override would move
  a target, because keying on less than the builder anchors on serves targets
  computed for a stale root.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import acp_backends, security
from kiro_crew.acp import types as acp_types
from kiro_crew.agent_sdk import host_auth, tool_gate
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.security import paths as security_paths
from kiro_crew.subprocess_utf8 import UTF8_TEXT

SRC = Path(__file__).resolve().parent.parent / "src"
HOST_AUTH_PY = SRC / "kiro_crew" / "agent_sdk" / "host_auth.py"

#: Stdlib modules this table may import, named one by one.
#:
#: Enumerated rather than derived from ``sys.stdlib_module_names``: the point of
#: the gate is that a reviewer sees the whole import surface of a module the
#: credential floor depends on, and a set the interpreter computes would silently
#: widen with the interpreter. The list is deliberately larger than what the
#: module imports today, so an ordinary stdlib addition is not a test edit while
#: a third-party or heavy in-tree one still is.
_ALLOWED_STDLIB = frozenset(
    {
        "__future__",
        "abc",
        "collections",
        "collections.abc",
        "dataclasses",
        "enum",
        "functools",
        "os",
        "os.path",
        "pathlib",
        "sys",
        "types",
        "typing",
    }
)

#: The only in-tree import allowed here: itself a stdlib-only leaf, and the module
#: that supplies the backend ids this table is keyed by.
_ALLOWED_IN_TREE = frozenset({"kiro_crew.agent_sdk.backends"})

#: Packages whose arrival would close the cycle the leaf exists to avoid.
_FORBIDDEN_IN_TREE = (
    "kiro_crew.security",
    "kiro_crew.acp",
    "kiro_crew.config",
    "kiro_crew.sandbox",
)

#: The methods :class:`host_auth.AgentInteractiveLogin` requires.
_LOGIN_PROTOCOL_METHODS = frozenset({"login_flows", "begin_login", "poll_login", "logout"})


def _declaration_kwargs(**overrides: object) -> dict:
    """A minimal VALID declaration, so each validation case varies one field."""
    base: dict = {
        "backend": "probe",
        "credential_leaves": (".probe/token.json",),
        "home_override_env_vars": (),
        "adapter_own_leaves": (),
        "sign_in_remedy": "Sign in to probe.",
        "signed_out_message": "Probe is not signed in. Sign in to probe.",
        "host_logout_retires_children": False,
        "entitlement_source": host_auth.ENTITLEMENT_OWN_CREDENTIAL_FILE,
    }
    base.update(overrides)
    return base


# ── 1. Parity: a selectable harness always has an auth answer ────────────────


def test_every_known_harness_has_a_declaration() -> None:
    """A harness becomes selectable by joining the known set, and joining it
    without an auth answer is how a live OAuth token stayed off the credential
    floor once already. Names the gap rather than comparing lengths, so a failure
    says which harness is unanswered."""
    assert host_auth.missing_declarations() == ()


def test_the_parity_gate_fails_for_an_undeclared_harness(monkeypatch) -> None:
    """The gate above passes today, which proves nothing about whether it CAN
    fail. A synthetic id in the known set stands in for the next harness someone
    makes selectable, and the gate has to name it -- otherwise the parity test is
    a rubber stamp that would have missed the incident it exists for."""
    synthetic = "harness-that-forgot-to-declare"
    monkeypatch.setattr(
        host_auth,
        "ACP_BACKENDS_KNOWN",
        frozenset(host_auth.ACP_BACKENDS_KNOWN) | {synthetic},
    )
    assert host_auth.missing_declarations() == (synthetic,)


# ── 2. The table stays a stdlib-only leaf ────────────────────────────────────


def _module_scope_imports(tree: ast.Module) -> list[str]:
    """Every import reachable at module scope, including inside a top-level guard.

    A guarded import still executes at import time on the branch that is taken,
    so a ``TYPE_CHECKING``-shaped block is not an escape hatch from the gate.
    """
    found: list[str] = []

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = "." * node.level
                found.append(f"{prefix}{node.module or ''}")
            elif isinstance(node, ast.If):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for handler in node.handlers:
                    visit(handler.body)

    visit(tree.body)
    return found


def test_module_scope_imports_are_stdlib_or_the_backend_leaf() -> None:
    """The floor reads this table while building itself, so a heavier import here
    is a cycle rather than a slow start. Read from the SOURCE, not from the loaded
    module, so an import added and shadowed by a later rebinding is still caught."""
    tree = ast.parse(HOST_AUTH_PY.read_text(encoding="utf-8"))
    imports = _module_scope_imports(tree)
    assert imports, "parsed no module-scope imports; the gate would pass vacuously"
    stray = sorted(
        name for name in imports if name not in _ALLOWED_STDLIB and name not in _ALLOWED_IN_TREE
    )
    assert not stray, (
        f"host_auth imports {stray} at module scope; the credential floor reads this "
        "table during its own construction, so anything heavier than stdlib (or the "
        "stdlib-only backend-id leaf) closes an import cycle on a security path"
    )


def test_importing_the_table_pulls_in_nothing_heavy() -> None:
    """The gate that keeps the credential floor able to read this table.

    ``security.paths`` imports ``host_auth`` at module scope while assembling the
    read-gate floor. If importing this table pulled in ``kiro_crew.security``,
    ``kiro_crew.acp``, ``kiro_crew.config`` or ``kiro_crew.sandbox``, that would be
    a cycle through the very module doing the importing -- and the failure mode is
    not a clean ImportError but a partially-initialised security module whose floor
    is shorter than its author believed.

    Runs in a COLD interpreter: by the time this test executes the suite has
    already imported most of the package, so an in-process ``sys.modules`` check
    would pass no matter what this module imports. Only the modules that arrive
    AFTER the import are judged, so interpreter and site startup are not mistaken
    for this module's own dependencies.
    """
    probe = "\n".join(
        (
            "import sys",
            f"sys.path.insert(0, {str(SRC)!r})",
            "before = set(sys.modules)",
            "import kiro_crew.agent_sdk.host_auth as m",
            "assert m.credential_leaves(), 'table did not load'",
            "added = sorted(set(sys.modules) - before)",
            f"forbidden_roots = {_FORBIDDEN_IN_TREE!r}",
            "forbidden = sorted(",
            "    name for name in sys.modules",
            "    if any(name == root or name.startswith(root + '.') for root in forbidden_roots)",
            ")",
            "def _file(name):",
            "    return getattr(sys.modules.get(name), '__file__', None) or ''",
            "vendored = sorted(",
            "    name for name in added",
            "    if '.' not in name",
            "    and ('site-packages' in _file(name) or 'dist-packages' in _file(name))",
            ")",
            "print(repr({'forbidden': forbidden, 'vendored': vendored, 'added': added}))",
        )
    )
    # ``-B``: the probe imports the package from the checkout, and without it the
    # interpreter writes ``__pycache__`` into the repository as a side effect of a
    # test run.
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        cwd=str(SRC.parent),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr[-2000:]}"
    loaded = ast.literal_eval(result.stdout.strip().splitlines()[-1])

    assert loaded["forbidden"] == [], (
        f"importing host_auth loaded {loaded['forbidden']}; the credential floor "
        "imports this table while building itself, so any of these closes a cycle"
    )
    assert loaded["vendored"] == [], (
        f"importing host_auth loaded third-party module(s) {loaded['vendored']}; the "
        "table must be readable before any dependency is available"
    )
    assert "kiro_crew.agent_sdk.backends" in loaded["added"], (
        "the probe did not observe the backend-id leaf loading, so it was measuring "
        "an already-warm interpreter rather than a cold import"
    )


# ── 3. Each projection equals the table it replaced, at its real consumer ────


def test_every_declared_leaf_is_on_the_read_gate_floor() -> None:
    """A declared credential leaf that never reaches the floor is a harness whose
    token an agent ``fs_read`` can still lift. Asserted on the PUBLIC view, which
    is the same list ``is_sensitive_path`` consults, so the projection is proven at
    the consumer rather than at the splice site."""
    floor = set(security.sensitive_home_dirs())
    missing = sorted(leaf for leaf in host_auth.credential_leaves() if leaf not in floor)
    assert not missing, f"declared but unfenced: {missing}"


def test_the_mask_exclusion_is_the_declaration_projection() -> None:
    """The mask exclusion and the fence must name the same file, or a harness is
    either masked out of its own auth or excluded from a mask that never covered
    it. Compares against the projection rather than a literal, so the day a
    harness declares a second leaf the table is the only edit."""
    expected = {
        declaration.backend: declaration.adapter_own_leaves
        for declaration in host_auth.AGENT_AUTH_DECLARATIONS
        if declaration.adapter_own_leaves
    }
    assert tool_gate.ADAPTER_OWN_CREDENTIAL_LEAVES == expected
    assert expected, "no harness declares an own-leaf, so this gate would pass vacuously"


def test_every_excluded_own_leaf_is_also_on_the_floor() -> None:
    """An exclusion from a mask that never covered the leaf is a no-op at best and
    a widening at worst. The declaration refuses the local half of this (an
    own-leaf must be one of the declaration's own credential leaves); this pins the
    other half, that the leaf really did land on the floor the mask is derived
    from."""
    floor = set(security.sensitive_home_dirs())
    for backend, leaves in tool_gate.ADAPTER_OWN_CREDENTIAL_LEAVES.items():
        for leaf in leaves:
            assert leaf in floor, (
                f"{backend!r} asks the mask to spare {leaf!r}, which is not on the "
                "read-gate floor; sparing a leaf the floor never covered opens it"
            )


def test_the_logout_recycle_set_is_projected_not_copied() -> None:
    """Membership authorizes ENDING a live turn, so the two spellings consumers
    import must be one object. A copy would keep passing an equality test while
    drifting from the declarations the floor and the mask read."""
    assert host_auth.backends_retired_by_host_logout() == frozenset(
        {ACP_BACKEND_KIRO, ACP_BACKEND_KAS}
    )
    assert acp_types.backends_retired_by_host_logout() == frozenset({"", "kas"})
    assert (
        acp_types.backends_retired_by_host_logout
        is acp_backends.backends_retired_by_host_logout
        is host_auth.backends_retired_by_host_logout
    )
    # Published as a FUNCTION and nowhere as an ``ACP_BACKENDS_*`` set. That naming is
    # harness vocabulary, whose home is the backends module, and a derived answer is
    # not vocabulary -- ``scripts/check_harness_parity.py`` fails on a set defined
    # outside that module, and the set could not be defined inside it either (it
    # supplies the ids this table is keyed by). Asserted so the alias does not come
    # back as a convenience.
    for module in (host_auth, acp_types, acp_backends):
        assert not hasattr(module, "ACP_BACKENDS_KIRO_IDENTITY_STORE"), module.__name__


def test_only_a_host_store_harness_is_retired_by_a_host_logout() -> None:
    """Derived from a declared fact rather than from "not claude": a harness
    authenticated some other way must never be recycled on a store it never reads,
    and a negative list grows wrong silently as harnesses are added."""
    retired = host_auth.backends_retired_by_host_logout()
    for declaration in host_auth.AGENT_AUTH_DECLARATIONS:
        on_host_store = declaration.entitlement_source == host_auth.ENTITLEMENT_HOST_IDENTITY_STORE
        assert (declaration.backend in retired) is on_host_store


def test_the_override_pairing_is_the_declaration_projection() -> None:
    """The floor re-anchors every declared leaf under each override variable, so a
    relocated token is still fenced. The pairing has to name the same leaf the
    floor fences and the same variable the resolver reads -- it was a third
    hand-maintained copy of both."""
    assert security_paths._OVERRIDE_ANCHORED_LEAVES == host_auth.override_anchored_leaves()
    assert security._OVERRIDE_ANCHORED_LEAVES is security_paths._OVERRIDE_ANCHORED_LEAVES
    assert security_paths._OVERRIDE_ANCHORED_LEAVES, "no leaf is override-anchored; vacuous"


def test_every_anchored_variable_is_a_declared_override() -> None:
    """The resolver only resolves variables the declarations name, so a variable
    that appears in the pairing but not in that list anchors on a root nobody ever
    resolved -- an entry describing protection that does not exist."""
    resolved = set(host_auth.home_override_env_vars())
    for leaf, env_vars in security_paths._OVERRIDE_ANCHORED_LEAVES:
        for env_var in env_vars:
            assert env_var in resolved, (
                f"{leaf!r} is anchored on {env_var!r}, which no declaration names, so "
                "the resolver never resolves it and the anchor is dead"
            )


# ── 4. The declaration refuses what the host cannot honour ───────────────────


def test_an_unknown_entitlement_source_is_refused() -> None:
    """The doctor row, the panel and the logout policy all branch on this field, so
    a typo'd fourth spelling would read as a harness nobody has an answer for --
    silently, at every one of those branches."""
    with pytest.raises(ValueError, match="unknown entitlement source"):
        host_auth.AgentAuthDeclaration(**_declaration_kwargs(entitlement_source="vibes"))


@pytest.mark.parametrize("remedy", ["", "   ", "\n\t"])
def test_an_empty_remedy_is_refused(remedy: str) -> None:
    """The remedy is rendered VERBATIM wherever a harness is unauthenticated, so a
    blank one is an operator staring at an auth failure with no next step."""
    with pytest.raises(ValueError, match="no sign-in remedy"):
        host_auth.AgentAuthDeclaration(**_declaration_kwargs(sign_in_remedy=remedy))


def test_a_driver_cannot_unmask_a_leaf_it_did_not_declare() -> None:
    """A driver must not be able to unmask itself.

    ``adapter_own_leaves`` is the one field that asks the host to OPEN something,
    so it may only name a leaf this same declaration already put ON the floor. Were
    it free-form, a driver could name any fenced path -- another harness's token,
    ``.ssh`` -- and the sandbox mask would spare it.
    """
    with pytest.raises(ValueError, match="may only ask the mask to spare"):
        host_auth.AgentAuthDeclaration(
            **_declaration_kwargs(
                credential_leaves=(".probe/token.json",),
                adapter_own_leaves=(".ssh/id_rsa",),
            )
        )


def test_an_own_leaf_from_its_own_declaration_is_accepted() -> None:
    """The refusal above must not be a blanket ban: a harness that authenticates
    ITSELF has to keep reading its own token, which is the whole reason the
    exclusion exists."""
    declaration = host_auth.AgentAuthDeclaration(
        **_declaration_kwargs(
            credential_leaves=(".probe/token.json",),
            adapter_own_leaves=(".probe/token.json",),
        )
    )
    assert declaration.adapter_own_leaves == (".probe/token.json",)


@pytest.mark.parametrize(
    "source",
    [host_auth.ENTITLEMENT_OWN_CREDENTIAL_FILE],
)
def test_logout_recycling_is_refused_off_the_host_store(source: str) -> None:
    """A host logout says nothing about a store the harness never reads, so
    retiring its running child on that signal would end a live turn for no reason.
    Refused at declaration time because the consumer is a membership test that
    cannot tell a deliberate opt-in from a mistaken one."""
    with pytest.raises(ValueError, match="says nothing about"):
        host_auth.AgentAuthDeclaration(
            **_declaration_kwargs(host_logout_retires_children=True, entitlement_source=source)
        )


def test_a_host_store_harness_that_survives_a_host_logout_is_refused() -> None:
    """The reverse of the rule above, and the same failure in the other direction.

    A harness resolving its tokens from the host identity store that is NOT retired
    when that store changes account keeps serving turns on the previous account's
    credentials -- the exact failure the recycle set exists to prevent. Refusing
    only retire-without-host-store would leave this half to a table-data test that
    checks today's four rows and nothing a fifth author writes.
    """
    with pytest.raises(ValueError, match="survive a host logout"):
        host_auth.AgentAuthDeclaration(
            **_declaration_kwargs(
                host_logout_retires_children=False,
                entitlement_source=host_auth.ENTITLEMENT_HOST_IDENTITY_STORE,
            )
        )


def test_logout_recycling_is_accepted_on_the_host_store() -> None:
    """The pair above is a constraint, not a ban: a harness that DOES resolve its
    tokens from the host store must be retired, or it keeps serving turns on the
    previous account's credentials."""
    declaration = host_auth.AgentAuthDeclaration(
        **_declaration_kwargs(
            host_logout_retires_children=True,
            entitlement_source=host_auth.ENTITLEMENT_HOST_IDENTITY_STORE,
        )
    )
    assert declaration.host_logout_retires_children is True


# ── 5. An unnamed harness fails closed ───────────────────────────────────────


def test_an_unnamed_harness_gets_the_fail_closed_declaration() -> None:
    """Every consumer here is a security control reading the table during its own
    construction, so the lookup is TOTAL: an unrecognised id gets an answer that
    fences nothing extra and retires nothing, rather than an exception on a
    security path."""
    declaration = host_auth.declaration_for("nonexistent")
    assert declaration is host_auth.UNKNOWN_AGENT_AUTH
    assert declaration.credential_leaves == ()
    assert declaration.home_override_env_vars == ()
    assert declaration.adapter_own_leaves == ()
    assert declaration.host_logout_retires_children is False
    assert declaration.sign_in_remedy.strip()


def test_the_unknown_answer_contributes_nothing_to_any_projection() -> None:
    """Fail-closed has to hold at the projections too. An UNKNOWN that leaked a
    leaf would fence a path on behalf of a harness nobody can name; one that
    leaked into the logout set would end live turns for it."""
    assert host_auth.UNKNOWN_AGENT_AUTH not in host_auth.AGENT_AUTH_DECLARATIONS
    assert "nonexistent" not in host_auth.backends_retired_by_host_logout()


# ── 6. The in-product sign-in flow is OPTIONAL ───────────────────────────────


class _FullLogin:
    """A driver that offers every part of an in-product sign-in."""

    def login_flows(self):
        return ("device_code",)

    async def begin_login(self, flow):
        return {}

    async def poll_login(self, flow):
        return {}

    async def logout(self):
        return True


class _MissingPoll:
    """A driver that starts a flow it can never report the completion of."""

    def login_flows(self):
        return ("device_code",)

    async def begin_login(self, flow):
        return {}

    async def logout(self):
        return True


def test_the_login_protocol_is_satisfied_structurally() -> None:
    """A driver satisfies the flow with no base class to inherit and no
    registration step to forget, so the capability cannot be claimed by a driver
    that only registered and never implemented."""
    assert isinstance(_FullLogin(), host_auth.AgentInteractiveLogin)


def test_a_partial_login_implementation_does_not_satisfy_the_protocol() -> None:
    """Half a flow is worse than none: a consumer that found ``begin_login`` and
    called it would strand the operator at a started sign-in it can never poll.
    ``runtime_checkable`` checks method PRESENCE, so this is the check that a
    missing member is actually observed."""
    assert not isinstance(_MissingPoll(), host_auth.AgentInteractiveLogin)


def test_no_current_driver_implements_the_login_protocol() -> None:
    """Asserted POSITIVELY, so the day a harness gains an in-product sign-in this
    test is the thing that notices.

    Every harness shipped today brings its own sign-in, which is the reason the
    protocol is optional rather than a method on the base class. When that stops
    being true, the panel, the doctor row and the remedy string all need an answer
    for a harness whose login Kiro Crew drives -- and none of them has one yet.
    """
    implementers: list[str] = []
    for source in sorted(SRC.rglob("kiro_crew/**/*.py")):
        if source == HOST_AUTH_PY:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if _LOGIN_PROTOCOL_METHODS <= methods:
                implementers.append(f"{source.relative_to(SRC)}::{node.name}")
    assert implementers == [], (
        f"{implementers} now implement AgentInteractiveLogin. That is a real "
        "capability change, not a refactor: the panel, the doctor row and the "
        "sign-in remedy each need an answer for a harness Kiro Crew logs in itself"
    )


# ── 7. The resolved-root cache key covers every declared override ────────────


@pytest.fixture()
def _no_adapter_overrides(monkeypatch):
    """Clear every declared override so a developer machine that exports one
    cannot make a default-location assertion pass for the wrong reason."""
    for env_var in host_auth.home_override_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    security._home_targets_cache.clear()
    yield
    security._home_targets_cache.clear()


def test_the_resolved_root_key_is_still_hashable(_no_adapter_overrides, tmp_path) -> None:
    """The resolved roots ARE the TTL cache key, so they have to hash. Holding the
    declared overrides as a tuple of pairs rather than a mapping is what keeps that
    true, and the fallback -- dropping them from the key -- is the fail-open shape
    the key exists to prevent."""
    roots = security._resolve_root_anchors(str(tmp_path))
    assert isinstance(hash(roots), int)
    assert isinstance(roots.adapter_roots, tuple)
    assert {env for env, _root in roots.adapter_roots} == set(host_auth.home_override_env_vars())


def test_a_changed_declared_override_changes_the_key(monkeypatch, tmp_path) -> None:
    """The fail-OPEN guard. The builder anchors a declared leaf under each override
    root, so a key that omits those roots would serve targets computed for the
    PREVIOUS value -- leaving a token an operator just relocated outside the gate
    while the cache still reports it covered."""
    assert "CODEX_HOME" in host_auth.home_override_env_vars(), (
        "premise gone: CODEX_HOME is no longer a declared override, so this case "
        "no longer exercises an adapter root"
    )
    first_root = tmp_path / "codex-a"
    second_root = tmp_path / "codex-b"
    first_root.mkdir()
    second_root.mkdir()

    monkeypatch.setenv("CODEX_HOME", str(first_root))
    first = security._resolve_root_anchors(str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(second_root))
    second = security._resolve_root_anchors(str(tmp_path))

    assert first != second, (
        "the resolved-root key did not change when a declared override moved; the "
        "cached target set would keep naming the old location"
    )
    assert dict(first.adapter_roots)["CODEX_HOME"] != dict(second.adapter_roots)["CODEX_HOME"]


def test_an_unset_declared_override_resolves_to_none(_no_adapter_overrides, tmp_path) -> None:
    """Every declared variable is present in the key with a ``None`` value rather
    than absent, so setting one for the first time changes the key instead of
    merely adding a field the previous key never had."""
    roots = security._resolve_root_anchors(str(tmp_path))
    assert all(root is None for _env, root in roots.adapter_roots)


# ---------------------------------------------------------------------------
# A re-exposure must be a NARROWING of the mask, never a hole in the floor
# ---------------------------------------------------------------------------


def test_every_declared_expose_leaf_sits_under_a_masked_directory() -> None:
    """A re-exposed file must be inside something the mask hides, for EVERY harness.

    A re-exposure asks the host to OPEN a path, and the mask is the only thing that
    makes the request a narrowing. A leaf sitting under nothing the mask hides is a
    fresh hole in the read-gate floor rather than a carve-out.

    Iterates the whole table rather than one harness on purpose: the containment
    check that existed covered only the harness that happened to need a
    re-exposure, so a second entry would have gone unchecked at both layers.

    This table is HOST-owned and stays that way -- containment is necessary but not
    sufficient, because a private key under a masked directory satisfies it exactly
    as the one legitimate case does. Which is why there is no declared field for it.
    """
    from kiro_crew.agent_sdk import tool_gate as gate

    for backend, leaves in gate.ADAPTER_EXPOSED_CREDENTIAL_LEAVES.items():
        if not leaves or not gate.is_enforced(backend):
            continue
        hidden = gate.adapter_hidden_credential_dirs(backend)
        exposed = gate.adapter_expose_files(backend, hidden)
        assert exposed, f"{backend!r} names {leaves!r} and none survived containment"
        for path in exposed:
            assert gate._sits_under_any(path, hidden), (
                f"{path!r} is re-exposed for {backend!r} but sits under no directory "
                "the mask hides"
            )


def test_an_uncontained_expose_leaf_is_dropped_not_honoured(monkeypatch) -> None:
    """An expose leaf the mask does not cover is refused, and fails CLOSED.

    Dropping it costs a session that names its own failure; honouring it exposes a
    file with no signal at all. Checked at the point of use, against the mask
    actually being applied, rather than where the table is written: the floor is not
    readable from a leaf this early.
    """
    from kiro_crew.agent_sdk import tool_gate as gate

    enforced = next(
        b
        for b, leaves in gate.ADAPTER_EXPOSED_CREDENTIAL_LEAVES.items()
        if leaves and gate.is_enforced(b)
    )
    # A leaf under nothing this mask hides. Deliberately NOT a private key: that one
    # IS under a masked directory, so containment honours it -- which is exactly why
    # a re-exposure stays a HOST decision rather than a declared one.
    monkeypatch.setitem(gate.ADAPTER_EXPOSED_CREDENTIAL_LEAVES, enforced, ("Documents/notes.txt",))
    hidden = gate.adapter_hidden_credential_dirs(enforced)
    assert gate.adapter_expose_files(enforced, hidden) == ()


def test_a_sibling_of_a_masked_directory_is_not_treated_as_inside_it() -> None:
    """``~/.awsconfig`` is not inside ``~/.aws``, and a prefix test must not say so.

    A bare ``startswith`` on the root reports a sibling whose name merely begins
    with the root's as contained, which would re-expose a different file from the
    one the declaration named. A root that IS the target is not inside itself
    either.
    """
    from kiro_crew.agent_sdk import tool_gate as gate

    root = os.path.join(os.path.sep, "tmp", "authseam-root", ".aws")
    assert gate._sits_under_any(os.path.join(root, "config"), (root,))
    assert not gate._sits_under_any(root + "config", (root,))
    assert not gate._sits_under_any(root, (root,))


# ---------------------------------------------------------------------------
# Two registers, because only one of them has evidence behind it
# ---------------------------------------------------------------------------


def test_the_unprobed_remedy_never_asserts_the_sign_in_state() -> None:
    """A string rendered without a measurement states the ACTION, not the STATE.

    The backend panel shows it as a standing caveat and the doctor row prints it
    under an explicit "not checked here", so a sentence claiming the harness is not
    signed in tells an operator who IS signed in something false -- which is how a
    standing line teaches its reader to skip it. ``signed_out_message`` is the one
    allowed to assert the state, and it is reached only on real evidence.
    """
    for declaration in host_auth.AGENT_AUTH_DECLARATIONS:
        remedy = declaration.sign_in_remedy.lower()
        for claim in ("is not signed in", "is not logged in"):
            assert claim not in remedy, (
                f"{declaration.backend!r} asserts {claim!r} in a remedy that is "
                "rendered with no measurement behind it"
            )


def test_the_kiro_signed_out_message_is_unchanged() -> None:
    """The auth-required path raises exactly the string it always raised.

    That path reaches the message on the harness's own stderr evidence, so moving
    it into a declaration must not reword what an operator already reads when
    kiro-cli's login has lapsed.
    """
    expected = (
        "kiro-cli is not logged in. Run `kiro-cli login` in your terminal, "
        "then start a new chat."
    )
    assert host_auth.signed_out_message("") == expected
    # KAS may draw its access token from Crew's own vault (the dashboard's Kiro
    # sign-in card, under Developer > Agent Backend) OR from kiro-cli's store,
    # and the formatter cannot see which one a process had, so its sentence
    # names both sign-ins rather than repeating kiro's. The kiro-cli remedy
    # stays in it verbatim.
    kas = host_auth.signed_out_message("kas")
    assert kas != expected
    assert "`kiro-cli login`" in kas
    assert "Developer → Agent Backend → Kiro sign-in" in kas
    assert "start a new chat" in kas
    # The standing (unprobed) remedy names the same two paths, in plain prose.
    remedy = host_auth.declaration_for("kas").sign_in_remedy
    assert "Developer → Agent Backend → Kiro sign-in" in remedy and "kiro-cli login" in remedy


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_an_empty_signed_out_message_is_refused(blank: str) -> None:
    """A declaration with no signed-out message would raise a blank auth error."""
    with pytest.raises(ValueError, match="signed-out message"):
        host_auth.AgentAuthDeclaration(**_declaration_kwargs(signed_out_message=blank))


def test_the_panel_remedy_reads_as_prose_not_as_terminal_output() -> None:
    """``sign_in_remedy`` is rendered as PLAIN TEXT, so it must not carry markup.

    The dashboard prints it inside a div with no markdown pass. A ``--`` written the
    way this codebase writes comments therefore reaches the reader as two hyphens,
    and a markdown backtick as a backtick -- one line below sibling copy in the same
    card that uses a real em dash. ``signed_out_message`` is exempt: it goes to a
    terminal row and to the chat error card, both of which render them.
    """
    for declaration in (*host_auth.AGENT_AUTH_DECLARATIONS, host_auth.UNKNOWN_AGENT_AUTH):
        remedy = declaration.sign_in_remedy
        assert "--" not in remedy, (
            f"{declaration.backend!r} writes '--' in a string rendered as plain text; "
            "use an em dash"
        )
        assert "`" not in remedy, (
            f"{declaration.backend!r} writes a markdown backtick in a string rendered "
            "as plain text"
        )


def test_every_entitlement_source_has_an_operator_facing_label() -> None:
    """A triage row an operator reads must not print a code identifier.

    Doctor printed ``own_credential_file`` raw, and the KAS token row read
    ``host_identity_store`` where it had said "owned by kiro-cli". A source with no
    label falls back to the identifier, so the gate is that every source HAS one.
    """
    assert set(host_auth.ENTITLEMENT_LABELS) == host_auth.ENTITLEMENT_SOURCES
    for source, label in host_auth.ENTITLEMENT_LABELS.items():
        assert label.strip(), source
        assert "_" not in label, f"{source!r} labels itself with a code identifier"
    for declaration in host_auth.AGENT_AUTH_DECLARATIONS:
        assert "_" not in host_auth.entitlement_label(declaration.backend)
