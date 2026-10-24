"""The post-bind mint of the mediated-secret policy-signing root degrades, never fails."""

from __future__ import annotations


def test_mint_mediation_signing_root_degrades_on_invalid_signing_key(tmp_path, monkeypatch) -> None:
    """An occupied-but-uncertifiable mediated-secret signing key must DEGRADE the
    gateway (run with mediation disabled-closed), never raise out of the mint task.

    The bad state is reachable with no tampering by the fenced agent: a
    pre-upgrade plant under a then-unmasked leaf, a partial data-home restore, or
    an operator deleting token_signing.key (whose recreation yields a token root
    that does not certify the existing key.json). Halting buys no security,
    because _secret_policy_key already converts the same failure to None so policy
    verification -- and thus all mediation -- already fails closed with the root
    unloaded. So the mint must swallow the RuntimeError, and mediation must be
    verifiably refusing.

    Mutation guard: dropping the try/except around initialize_secret_host_key in
    mint_mediation_signing_root re-raises here and fails the "survives" assertion.
    """
    import asyncio
    import threading

    import kiro_crew.secrets_mediation.signing_root as _ta
    from kiro_crew.secrets_mediation import provenance
    from kiro_crew.secrets_mediation.provenance import verify_secret_authorizations

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    # mint_mediation_signing_root calls initialize_secret_host_key() with no argument, so
    # it resolves the config dir through provenance.resolve_config_dir (the
    # module-level ``from kiro_crew.config.paths import config_dir as
    # resolve_config_dir`` binding). Patch THAT binding -- patching
    # kiro_crew.config.paths.config_dir alone does not reach it, so the host-key
    # read would hit the REAL config dir (where a valid key exists), the
    # RuntimeError never fires, and the degradation branch is never exercised.
    monkeypatch.setattr(provenance, "resolve_config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    # Plant an occupied-but-uncertifiable key.json: present, parseable enough to
    # occupy the leaf, but not gateway-certified -> initialize_secret_host_key raises.
    key_dir = tmp_path / provenance.HOST_KEY_DIRNAME
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / "key.json").write_text("{}", encoding="utf-8")

    # Record the thread the degradation audit runs on: its first SEL access does
    # bounded filesystem init, so it must NOT run on the event loop thread.
    audit_threads: list[int] = []

    class _RecordingSel:
        def log_api_access(self, **_k: object) -> None:
            audit_threads.append(threading.get_ident())

    monkeypatch.setattr(_ta, "_sel_fn", lambda: _RecordingSel())

    async def _run() -> int:
        loop_thread = threading.get_ident()
        # SURVIVES: the mint completes rather than raising the RuntimeError out.
        await _ta.mint_mediation_signing_root()
        return loop_thread

    loop_thread = asyncio.run(_run())

    # DISABLED-CLOSED: with the root uninitialized, mediation refuses every
    # policy signature -- the safest state, and the whole point of degrading.
    assert verify_secret_authorizations({"K": {"origin": "https://x"}}, "0" * 64, tmp_path) is False
    # The degradation audit ran, and OFF the event loop thread (F3).
    assert audit_threads and loop_thread not in audit_threads


def test_mint_mediation_signing_root_survives_a_failing_degradation_audit(
    tmp_path, monkeypatch
) -> None:
    """The degradation audit is best-effort: if the audit's OWN SEL init raises,
    the mint task must still complete (mediation disabled-closed), never raise.

    The trigger for degrading is often an unwritable or full data home, so SEL's
    own first-access filesystem init hits the SAME failing disk and raises. If
    that raise escaped, degrading rather than halting would be defeated by the
    very condition it exists to survive. So the audit must swallow its error and
    fall back to a local warning.

    Mutation guard: dropping the try/except INSIDE _audit_degradation re-raises
    the SEL failure through asyncio.to_thread and fails the "survives" assertion.
    """
    import asyncio

    import kiro_crew.secrets_mediation.signing_root as _ta
    from kiro_crew.secrets_mediation import provenance
    from kiro_crew.secrets_mediation.provenance import verify_secret_authorizations

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    # Patch the binding initialize_secret_host_key resolves through (see the sibling
    # degradation test): patching kiro_crew.config.paths.config_dir alone does not
    # reach provenance.resolve_config_dir.
    monkeypatch.setattr(provenance, "resolve_config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    # Occupied-but-uncertifiable key.json -> initialize_secret_host_key raises -> the
    # degradation branch runs the audit.
    key_dir = tmp_path / provenance.HOST_KEY_DIRNAME
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / "key.json").write_text("{}", encoding="utf-8")

    # SEL is unavailable on this data home: its very first access raises, exactly
    # as an unwritable/full data home makes SecurityEventLog's key-dir mkdir raise.
    class _RaisingSel:
        def log_api_access(self, **_k: object) -> None:
            raise OSError("SEL storage unavailable on a read-only data home")

    monkeypatch.setattr(_ta, "_sel_fn", lambda: _RaisingSel())

    # SURVIVES: mint_mediation_signing_root completes rather than re-raising.
    asyncio.run(_ta.mint_mediation_signing_root())

    # DISABLED-CLOSED: with the root uninitialized, mediation still refuses.
    assert verify_secret_authorizations({"K": {"origin": "https://x"}}, "0" * 64, tmp_path) is False
