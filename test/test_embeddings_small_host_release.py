"""A small machine releases the embedding model sooner, and a person's arrival loads it.

``memory.embedding_idle_evict_secs`` defaults to ``"auto"``: 600 s on a machine with
16 GiB of memory or less, 3600 s elsewhere. A stored number is always the operator's,
3600 included. A person opening, typing in or sending to a chat, or a channel message,
starts the load on a small machine before the turn's build asks for a vector.
See docs/request-for-change/rfc-small-machine-embedding-release.md.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import embeddings_helpers
import pytest
from embeddings_helpers import _make_fake_llama_class, _write_model_file

import kiro_crew.embeddings as embeddings_mod
from kiro_crew import embeddings_settings, embeddings_shared
from kiro_crew.config import superseded_defaults as SD
from kiro_crew.config.section_builders import _build_memory_config, _embedding_idle_evict_secs
from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.messaging import dispatch as dispatch_facade
from kiro_crew.messaging import dispatch_admission
from kiro_crew.messaging.inbound_spool import InboundRoute
from kiro_crew.testing.wait import default_timeout

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_reset_embedding_singletons = embeddings_helpers._reset_embedding_singletons

#: Every stored shape the resolver has to answer for.
_STORED = [
    ("missing", None),
    ("auto", "auto"),
    ("junk", "soon"),
    ("bool", True),
    ("negative", -1),
    ("zero", 0),
    ("five", 5),
    ("ten minutes", 600),
    ("just under an hour", 3599),
    ("an hour", 3600),
    ("a day", 86400),
    ("a float", 1.5),
]


def _memory_section(value: object) -> dict:
    return {} if value is None else {"embedding_idle_evict_secs": value}


def _main_resolution(value: object) -> float:
    """What the resolver answered before this change, on every machine."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return 3600.0


class TestResolution:
    @pytest.mark.parametrize("label, value", _STORED)
    def test_a_larger_machine_answers_exactly_as_before(self, monkeypatch, label, value):
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: False)
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: _memory_section(value))
        assert embeddings_mod.embedding_idle_evict_secs() == _main_resolution(value), label

    @pytest.mark.parametrize("label, value", _STORED)
    def test_a_small_machine_changes_only_the_shipped_default(self, monkeypatch, label, value):
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: True)
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: _memory_section(value))
        stored_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        expected = float(value) if stored_number and value >= 0 else 600.0
        assert embeddings_mod.embedding_idle_evict_secs() == expected, label

    def test_a_stored_hour_is_kept_on_a_small_machine(self, monkeypatch):
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: True)
        monkeypatch.setattr(
            embeddings_mod, "_read_memory_config", lambda: {"embedding_idle_evict_secs": 3600}
        )
        assert embeddings_mod.embedding_idle_evict_secs() == 3600.0


class TestSmallMemoryHost:
    @pytest.mark.parametrize(
        "total_mib, small",
        [
            (8 * 1024, True),
            (16 * 1024, True),
            (16 * 1024 + 1, False),
            (32 * 1024, False),
            (0, False),
        ],
    )
    def test_the_threshold_is_sixteen_gib(self, monkeypatch, total_mib, small):
        monkeypatch.setattr(embeddings_settings, "_small_host_cache", [])
        monkeypatch.setattr("kiro_crew.platform_compat.host_total_mib", lambda: total_mib)
        assert embeddings_settings.small_memory_host() is small

    def test_an_unreadable_size_is_not_small(self, monkeypatch):
        def boom() -> int:
            raise OSError("no reading")

        monkeypatch.setattr(embeddings_settings, "_small_host_cache", [])
        monkeypatch.setattr("kiro_crew.platform_compat.host_total_mib", boom)
        assert embeddings_settings.small_memory_host() is False

    def test_it_is_read_once_per_process(self, monkeypatch):
        reads: list[int] = []

        def total() -> int:
            reads.append(1)
            return 8 * 1024

        monkeypatch.setattr(embeddings_settings, "_small_host_cache", [])
        monkeypatch.setattr("kiro_crew.platform_compat.host_total_mib", total)
        assert embeddings_settings.small_memory_host() and embeddings_settings.small_memory_host()
        assert reads == [1]


class TestConfigField:
    @pytest.mark.parametrize(
        "raw, built",
        [
            ("auto", "auto"),
            ("soon", "auto"),
            (True, "auto"),
            (-5, "auto"),
            (0, 0),
            (3600, 3600),
            (120.0, 120),
            (10**12, 365 * 24 * 3600),
        ],
    )
    def test_the_builder_matches_the_evictor_reading(self, raw, built):
        assert _embedding_idle_evict_secs(raw) == built

    def test_a_fresh_install_carries_auto(self):
        assert _build_memory_config({}).embedding_idle_evict_secs == "auto"

    def test_the_superseded_row_reports_only_on_a_small_machine(self, monkeypatch):
        (row,) = [
            e for e in SD.SUPERSEDED_DEFAULTS if e.dotted_key == "memory.embedding_idle_evict_secs"
        ]
        assert (row.old_default, row.new_default, row.auto_adopt) == (3600, "auto", False)
        assert row.applies is not None
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: True)
        assert row.applies({}) is True
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: False)
        assert row.applies({}) is False


class TestKeepWarm:
    def _loaded(self, tmp_path: Path, monkeypatch) -> LlamaCppEmbedder:
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert emb.embed("warm") is not None
        assert emb.wait_ready(timeout=default_timeout())
        assert emb._load_thread is not None and not emb._load_thread.is_alive()
        return emb

    def test_a_loaded_model_restarts_its_idle_clock(self, tmp_path: Path, monkeypatch):
        emb = self._loaded(tmp_path, monkeypatch)
        try:
            emb._last_used = 0.0
            emb.keep_warm()
            assert emb._last_used > 0.0
            assert emb.evict_if_idle(60.0) is False
        finally:
            emb.close()

    def test_an_unloaded_model_starts_loading(self, tmp_path: Path, monkeypatch):
        emb = self._loaded(tmp_path, monkeypatch)
        try:
            emb._last_used = 0.0
            assert emb.evict_if_idle(1.0) is True
            assert not emb.is_ready()
            emb.keep_warm()
            assert emb.wait_ready(timeout=default_timeout())
            # wait_ready returns on the publish; the loader then starts the evictor,
            # so join it before close() or that evictor outlives this test.
            assert emb._load_thread is not None
            emb._load_thread.join(default_timeout())
            assert not emb._load_thread.is_alive()
        finally:
            emb.close()

    def test_a_retired_backend_stays_unloaded(self, tmp_path: Path, monkeypatch):
        emb = self._loaded(tmp_path, monkeypatch)
        emb.retire()
        emb.keep_warm()
        assert not emb.is_ready()
        assert emb._load_thread is None or not emb._load_thread.is_alive()


class _Backend:
    def __init__(self) -> None:
        self.warmed = 0

    def keep_warm(self) -> None:
        self.warmed += 1

    def close(self) -> None:
        pass


class TestWarmForPerson:
    def test_a_larger_machine_does_nothing(self, monkeypatch):
        backend = _Backend()
        monkeypatch.setattr(embeddings_shared, "_shared_embedder", backend)
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: False)
        embeddings_shared.warm_embedder_for_person()
        assert backend.warmed == 0

    def test_a_small_machine_warms_the_built_embedder(self, monkeypatch):
        backend = _Backend()
        monkeypatch.setattr(embeddings_shared, "_shared_embedder", backend)
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: True)
        embeddings_shared.warm_embedder_for_person()
        assert backend.warmed == 1

    def test_no_embedder_is_built_for_a_warm_up(self, monkeypatch):
        monkeypatch.setattr(embeddings_shared, "_shared_embedder", None)
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: True)
        embeddings_shared.warm_embedder_for_person()
        assert embeddings_shared._shared_embedder is None


class _Reservation:
    def release(self) -> None:
        pass


class _Sessions:
    def __init__(self, grant: bool) -> None:
        self.grant = grant

    def reserve_inbound_callback(self) -> _Reservation | None:
        return _Reservation() if self.grant else None


_ROUTE = InboundRoute(conversation_id="c1", text="hello", user_id="u1")


class TestChannelAdmission:
    def _warmed(self, monkeypatch) -> list[int]:
        warmed: list[int] = []
        monkeypatch.setattr(
            dispatch_admission, "warm_embedder_for_person_soon", lambda: warmed.append(1)
        )

        async def spool(**_kwargs) -> None:
            return None

        monkeypatch.setattr(dispatch_facade, "spool_refused_turn", spool)
        return warmed

    def _admit(self, monkeypatch, *, grant: bool, human_input: bool, route) -> list[int]:
        warmed = self._warmed(monkeypatch)

        async def run() -> bool:
            return await dispatch_admission.admit_inbound_callback(
                _Sessions(grant), channel_type="telegram", route=route, human_input=human_input
            )

        assert asyncio.run(run()) is grant
        return warmed

    def test_a_persons_admitted_message_warms_the_model(self, monkeypatch):
        assert self._admit(monkeypatch, grant=True, human_input=True, route=_ROUTE) == [1]

    def test_a_raw_envelope_does_not(self, monkeypatch):
        assert self._admit(monkeypatch, grant=True, human_input=False, route=_ROUTE) == []

    def test_an_unrouted_monitor_completion_does_not(self, monkeypatch):
        assert self._admit(monkeypatch, grant=True, human_input=True, route=None) == []

    def test_a_refused_admission_does_not(self, monkeypatch):
        assert self._admit(monkeypatch, grant=False, human_input=True, route=_ROUTE) == []

    @pytest.mark.parametrize("route, expected", [(_ROUTE, [1]), (None, [])])
    def test_the_held_admission_warms_only_a_routed_message(self, monkeypatch, route, expected):
        warmed = self._warmed(monkeypatch)

        async def run() -> bool:
            async with dispatch_admission.hold_inbound_callback(
                _Sessions(True), channel_type="weixin", route=route
            ) as admitted:
                return admitted

        assert asyncio.run(run()) is True
        assert warmed == expected


class _FakeSysctlLibc:
    """A ``sysctlbyname`` that answers ``hw.memsize`` the way the macOS kernel does."""

    def __init__(self, memsize: int, result: int = 0) -> None:
        self.memsize = memsize
        self.result = result
        self.names: list[bytes] = []

    def sysctlbyname(self, name, value_ref, size_ref, _new, _newlen) -> int:
        self.names.append(name)
        value_ref._obj.value = self.memsize
        return self.result


class TestHostTotalOnMacos:
    """Some macOS builds have no ``SC_PHYS_PAGES``; the kernel's ``hw.memsize`` answers."""

    def _no_sysconf(self, monkeypatch) -> None:
        import os

        def sysconf(_name: str) -> int:
            raise ValueError("unrecognized configuration name")

        monkeypatch.setattr(os, "sysconf", sysconf, raising=False)  # Windows has none

    def test_a_mac_without_phys_pages_reads_hw_memsize(self, monkeypatch):
        import kiro_crew.platform_compat as pc

        self._no_sysconf(monkeypatch)
        libc = _FakeSysctlLibc(16 * 1024**3)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: libc)
        assert pc.host_total_mib() == 16 * 1024
        assert libc.names == [b"hw.memsize"]

    def test_a_failed_sysctl_reads_as_unknown(self, monkeypatch):
        import kiro_crew.platform_compat as pc

        self._no_sysconf(monkeypatch)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: _FakeSysctlLibc(1, result=-1))
        assert pc.host_total_mib() == 0
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: None)
        assert pc.host_total_mib() == 0
