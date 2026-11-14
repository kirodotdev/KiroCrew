"""The contract every third-party secret backend passes.

Adding a provider means: implement :class:`kiro_crew.secrets.backend.SecretBackend`,
register it, then subclass :class:`SecretBackendContract` with a ``harness``
fixture returning a :class:`Harness` for it. Nothing here names a provider.

The suite drives each backend only through the core (registry, ``call``,
``backend_view``, ``resolve_missing`` and the ``secret://`` resolver), so it
proves the guarantees the core gives every provider, not just the provider's own
methods.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew.mcp_gateway.secret_uri import resolve_secret_uris
from kiro_crew.secrets import backend as core
from kiro_crew.secrets.vault import SecretVault

_EN = Path(__file__).resolve().parents[1] / "website" / "src" / "i18n" / "locales" / "en.json"


class Harness:
    """What a provider's test double offers the suite. Subclass per provider."""

    backend: core.SecretBackend

    def good_values(self) -> dict[str, str]:
        """Connect-form values the provider accepts."""
        raise NotImplementedError

    def bad_values(self) -> dict[str, str]:
        """Connect-form values the provider refuses with a BackendError."""
        raise NotImplementedError

    def add_secret(self, name: str, value: str) -> None:
        """Put a plain ``secret`` item named *name* holding *value* at the provider."""
        raise NotImplementedError

    def fail_with(self, exc: Exception) -> None:
        """Make every later provider call raise *exc*."""
        raise NotImplementedError

    def hang_until(self, release: threading.Event) -> None:
        """Make every later provider call block until *release* is set."""
        raise NotImplementedError

    def make_unavailable(self) -> None:
        """Make this install unable to run the provider (its package gone)."""
        raise NotImplementedError


def _connect(vault: SecretVault, harness: Harness) -> core.Connection:
    backend = harness.backend
    values = core.form_values(backend, harness.good_values())
    conn = core.call(backend, lambda: backend.connect(values))
    vault._set_item_sync(core.connection_item(backend.id), "note", core.connection_fields(conn))
    return conn


def _view(vault: SecretVault, harness: Harness, local: tuple[str, ...] = ()) -> dict[str, Any]:
    views = core.backends_view(vault, list(local))
    return next(v for v in views if v["id"] == harness.backend.id)


class SecretBackendContract:
    """Run against a ``harness`` fixture; see the module docstring."""

    @pytest.fixture(autouse=True)
    def _preview_on(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(core, "preview_on", lambda: True)

    @pytest.fixture
    def harness(self) -> Harness:  # pragma: no cover - each provider overrides it
        raise NotImplementedError

    # ── declaration ──

    def test_is_registered_and_valid(self, harness: Harness) -> None:
        core._validate(harness.backend)
        assert isinstance(core.get(harness.backend.id), type(harness.backend))

    def test_every_declared_key_is_in_the_english_catalog(self, harness: Harness) -> None:
        catalog = json.loads(_EN.read_text(encoding="utf-8"))
        for key in core.i18n_keys(harness.backend):
            node: Any = catalog
            for part in key.split("."):
                assert isinstance(node, dict) and part in node, key
                node = node[part]
            assert isinstance(node, str) and node, key

    def test_the_token_field_is_marked_secret(self, harness: Harness) -> None:
        assert any(f.secret for f in harness.backend.connect_form)

    # ── connect ──

    def test_connect_returns_a_public_account_and_keeps_secrets_out_of_it(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        conn = _connect(SecretVault(tmp_path), harness)
        assert conn.account
        for f in harness.backend.connect_form:
            if f.secret:
                assert harness.good_values()[f.name] not in conn.account

    def test_connect_refuses_bad_values(self, harness: Harness) -> None:
        backend = harness.backend
        with pytest.raises(core.BackendError):
            values = core.form_values(backend, harness.bad_values())
            core.call(backend, lambda: backend.connect(values))

    def test_a_missing_required_field_is_refused_before_the_provider(
        self, harness: Harness
    ) -> None:
        required = [f.name for f in harness.backend.connect_form if f.required]
        values = {**harness.good_values(), required[0]: ""}
        with pytest.raises(core.BackendError) as excinfo:
            core.form_values(harness.backend, values)
        assert excinfo.value.code == "invalid_input"

    # ── listing ──

    def test_listing_shows_items_and_no_value_or_token(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        vault = SecretVault(tmp_path)
        harness.add_secret("CONTRACT_KEY", "contract-value-1")
        _connect(vault, harness)
        view = _view(vault, harness)
        assert view["status"] == "connected" and view["connected"] is True
        assert "CONTRACT_KEY" in [row["name"] for row in view["items"]]
        assert all(row["source"] == harness.backend.id for row in view["items"])
        text = json.dumps(view)
        assert "contract-value-1" not in text
        for f in harness.backend.connect_form:
            if f.secret:
                assert harness.good_values()[f.name] not in text

    def test_a_name_the_local_vault_holds_is_marked_shadowed(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        vault = SecretVault(tmp_path)
        harness.add_secret("BOTH", "remote")
        harness.add_secret("WAKATIME_OAUTH_TOKENS", "remote")
        _connect(vault, harness)
        rows = {r["name"]: r for r in _view(vault, harness, ("BOTH",))["items"]}
        assert rows["BOTH"]["shadowed"] is True
        assert rows["WAKATIME_OAUTH_TOKENS"]["shadowed"] is True

    def test_not_connected_offers_the_form(self, harness: Harness, tmp_path: Path) -> None:
        view = _view(SecretVault(tmp_path), harness)
        assert view["connected"] is False and view["status"] == "not_connected"
        assert [f["name"] for f in view["form"]] == [f.name for f in harness.backend.connect_form]

    @pytest.mark.parametrize("kind", ["error", "timeout"])
    def test_a_failing_provider_degrades_to_its_own_row(
        self,
        harness: Harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
    ) -> None:
        vault = SecretVault(tmp_path)
        _connect(vault, harness)
        vault.set_sync("LOCAL_ONE", "local-value")
        release = threading.Event()
        if kind == "error":
            harness.fail_with(RuntimeError("provider exploded"))
        else:
            # The bound under test; the provider waits on an event, never a clock.
            monkeypatch.setattr(
                core, "CALL_TIMEOUT_SECS", 0.05
            )  # flake-ok: the timeout is the subject
            harness.hang_until(release)
        try:
            view = _view(vault, harness)
            assert view["status"] == "unavailable" and view["connected"] is True
            assert view["error"]["code"] == ("unreachable" if kind == "error" else "timeout")
            assert "provider exploded" not in json.dumps(view)
            # The local item still resolves; the provider is not even asked.
            resolved, _ = resolve_secret_uris({"K": "secret://LOCAL_ONE"}, tmp_path)
            assert resolved == {"K": "local-value"}
            # A name only the provider could answer fails closed.
            with pytest.raises(ValueError, match=harness.backend.display_name):
                resolve_secret_uris({"K": "secret://REMOTE_ONLY"}, tmp_path)
        finally:
            release.set()

    def test_without_its_package_it_is_unavailable_and_local_items_work(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        vault = SecretVault(tmp_path)
        _connect(vault, harness)
        harness.make_unavailable()
        vault.set_sync("LOCAL_ONE", "local-value")
        view = _view(vault, harness)
        assert view["status"] == "unavailable" and view["connected"] is True
        assert core.read_connection(vault, harness.backend) is not None
        assert resolve_secret_uris({"K": "secret://LOCAL_ONE"}, tmp_path)[0] == {"K": "local-value"}
        with pytest.raises(ValueError):
            resolve_secret_uris({"K": "secret://REMOTE_ONLY"}, tmp_path)

    # ── resolution ──

    def test_resolves_a_name_the_local_vault_lacks_and_local_wins(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        vault = SecretVault(tmp_path)
        harness.add_secret("REMOTE_KEY", "remote-value")
        harness.add_secret("SAME", "remote-same")
        _connect(vault, harness)
        vault.set_sync("SAME", "local-same")
        resolved, keys = resolve_secret_uris(
            {"A": "secret://REMOTE_KEY", "B": "secret://SAME"}, tmp_path
        )
        assert resolved == {"A": "remote-value", "B": "local-same"}
        assert keys == {"A", "B"}

    def test_a_protected_name_is_never_asked(
        self, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = SecretVault(tmp_path)
        _connect(vault, harness)
        for name in ("WAKATIME_OAUTH_TOKENS", "CONNECTIONS_GITHUB_OAUTH_TOKENS"):
            harness.add_secret(name, "remote")
        asked: list[list[str]] = []
        real = core.call

        def spy(backend: core.SecretBackend, work: Callable[[], Any], *a: Any) -> Any:
            asked.append([backend.id])
            return real(backend, work, *a)

        monkeypatch.setattr(core, "call", spy)
        for name in (
            "WAKATIME_OAUTH_TOKENS",
            "CONNECTIONS_GITHUB_OAUTH_TOKENS",
            core.connection_item(harness.backend.id),
        ):
            with pytest.raises(ValueError, match="does not exist"):
                resolve_secret_uris({"K": f"secret://{name}"}, tmp_path)
        assert asked == []

    def test_preview_off_asks_nothing(
        self, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = SecretVault(tmp_path)
        harness.add_secret("REMOTE_KEY", "remote-value")
        _connect(vault, harness)
        monkeypatch.setattr(core, "preview_on", lambda: False)
        with pytest.raises(ValueError, match="does not exist"):
            resolve_secret_uris({"K": "secret://REMOTE_KEY"}, tmp_path)

    def test_the_stored_connection_round_trips(self, harness: Harness, tmp_path: Path) -> None:
        vault = SecretVault(tmp_path)
        conn = _connect(vault, harness)
        assert core.read_connection(vault, harness.backend) == conn
