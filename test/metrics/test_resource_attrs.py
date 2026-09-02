"""``_resource_attributes`` — identity, version, and environment labels.

Every attribute asserted here becomes a label on EVERY exported series, so
these tests pin four properties: the values are clamped/bounded (a raw
dev-build version, a patch-level runtime version, or a pass-through exotic
architecture would mint unbounded series), every probe fails soft (a broken
probe omits its attribute, never loses telemetry), the install id exists by
the time the FIRST resource is built (the probe itself is read-only; the one
write lives in ``_build_recorder``'s live branch, so no export can carry the
SDK's per-process substitute and no mid-life resource swap is needed), and the
deliberately-absent attributes stay absent (the distribution channel was
removed from the beacon by a data-minimization pass; re-adding it here would
quietly undo that decision).
"""

import json
import os
import platform
import re

import kiro_crew
from kiro_crew import beacon
from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics import provider
from kiro_crew.metrics.provider import get_recorder, reset_for_testing
from kiro_crew.metrics.provider import shutdown as provider_shutdown


def _patch_config(monkeypatch, **tel_kwargs):
    fake = KiroCrewConfig(telemetry=TelemetryConfig(**tel_kwargs))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake))
    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)


def _own_home(monkeypatch, tmp_path):
    """Give this test its OWN data home, so the install-id file is per-test.

    Every test that asserts the id file is absent (or that something created
    it) reads one shared piece of on-disk state, and a sibling test that mints
    would otherwise decide the outcome -- an order-dependent pass. ``KIROCREW_HOME``
    is the seam: ``config_dir()`` memoizes keyed on its raw value, so setting it
    re-resolves rather than serving the previous test's home.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    assert beacon.install_id(create=False) == "", "the home was not actually fresh"
    return home


def test_attrs_carry_identity_version_and_environment():
    # The build path is read-only for the id, so materialize it first — the
    # way every non-fresh install already has it on disk.
    beacon.install_id(create=True)
    attrs = provider._resource_attributes()

    assert attrs["service.name"] == "kirocrew"

    # Release-clamped, never a raw dev/nightly stamp: same clamp the beacon
    # ships, so the two surfaces can never disagree about one build.
    assert attrs["service.version"] == beacon.release(kiro_crew.__version__)
    version = str(attrs["service.version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", version) or version == beacon.UNKNOWN_VERSION

    assert attrs["os.type"] == platform.system().lower()
    runtime = platform.python_implementation().lower()
    assert attrs["process.runtime.name"] == (
        runtime if runtime in provider._KNOWN_RUNTIME_NAMES else provider._ATTR_OTHER
    )
    # major.minor ONLY — the patch level would add cardinality without
    # answering anything the minor does not. beacon.python_minor() is the
    # single owner of that clamp; a second spelling here could drift from it.
    assert attrs["process.runtime.version"] == beacon.python_minor()
    assert re.fullmatch(r"\d+\.\d+", str(attrs["process.runtime.version"]))

    # The core count is what lets cpu.seconds become a machine percentage
    # downstream; it exists only client-side.
    assert attrs["host.cpu.logical_count"] == os.cpu_count()
    assert isinstance(attrs["host.cpu.logical_count"], int)

    # Stable install id, not the SDK's per-process UUID — and the process
    # identity travels SEPARATELY so concurrent processes of one install
    # cannot interleave their series at a backend.
    install_id = str(attrs["service.instance.id"])
    assert len(install_id) == 32
    assert all(c in "0123456789abcdef" for c in install_id)
    assert attrs["process.pid"] == os.getpid()
    assert isinstance(attrs["process.pid"], int)


def test_arch_aliases_fold_to_one_label():
    # One architecture must not appear as two labels across OSes.
    assert provider._ARCH_BY_MACHINE["x86_64"] == "amd64"
    assert provider._ARCH_BY_MACHINE["amd64"] == "amd64"
    assert provider._ARCH_BY_MACHINE["aarch64"] == "arm64"
    assert provider._ARCH_BY_MACHINE["arm64"] == "arm64"
    machine = platform.machine().lower()
    expected = provider._ARCH_BY_MACHINE.get(machine, provider._ATTR_OTHER)
    assert provider._resource_attributes().get("host.arch") == expected


def test_unknown_environment_readings_fold_to_other(monkeypatch):
    # The docstring promises CLOSED sets: an exotic platform must fold to the
    # shared "other" bucket, never pass its own spelling through as a label.
    monkeypatch.setattr(provider.platform, "system", lambda: "SunOS")
    monkeypatch.setattr(provider.platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(provider.platform, "python_implementation", lambda: "MicroPython")
    attrs = provider._resource_attributes()
    assert attrs["os.type"] == provider._ATTR_OTHER
    assert attrs["host.arch"] == provider._ATTR_OTHER
    # process.runtime.name is in the same closed set as the other two: a
    # patched or exotic interpreter must not mint a label of its own.
    assert attrs["process.runtime.name"] == provider._ATTR_OTHER


def test_version_probe_failure_omits_only_that_attribute(monkeypatch):
    def _boom(_version):
        raise RuntimeError("release parse failed")

    monkeypatch.setattr(beacon, "release", _boom)
    attrs = provider._resource_attributes()
    assert "service.version" not in attrs
    # The other groups are untouched by the failed probe.
    assert attrs["service.name"] == "kirocrew"
    assert "os.type" in attrs


def test_id_read_failure_omits_the_attribute(monkeypatch):
    def _boom(*, create=True):
        raise OSError("disk unreadable")

    monkeypatch.setattr(beacon, "install_id", _boom)
    attrs = provider._resource_attributes()
    assert "service.instance.id" not in attrs
    assert "service.version" in attrs


def test_the_probe_is_read_only_and_the_build_path_mints(tmp_path, monkeypatch):
    # _resource_attributes itself never CREATES the id (create=False): it is a
    # pure probe, so calling it cannot have a side effect on disk.
    _own_home(monkeypatch, tmp_path)
    attrs = provider._resource_attributes()
    assert "service.instance.id" not in attrs
    assert beacon.install_id(create=False) == "", "the probe created the id"

    # The single write lives in _build_recorder's live branch, immediately
    # before the probe, so the id exists by the time the resource is built.
    beacon.install_id(create=True)
    assert "service.instance.id" in provider._resource_attributes()


def test_first_build_on_a_fresh_install_exports_the_id(tmp_path, monkeypatch):
    # The gap an earlier round named: telemetry enabled from the very first
    # boot (config pre-enabled, beacon disabled — a container image), so
    # consent never flips and the consent worker never rebuilds. Minting in
    # the build path means the FIRST export already carries the identity —
    # no id-less window, and no mid-life resource swap (which a backend reads
    # as a brand-new series set).
    reset_for_testing()
    _own_home(monkeypatch, tmp_path)
    metrics_dir = tmp_path / "m"
    _patch_config(
        monkeypatch,
        enabled=True,
        local_dir=str(metrics_dir),
        export_interval_seconds=3600,
    )
    try:
        rec = get_recorder()  # first build: mints, then labels
        assert rec.enabled is True
        minted = beacon.install_id(create=False)
        assert minted, "the build path never minted the id"

        rec.counter("kirocrew.session.idle_expired", attrs={"turn_active": False})
        provider_shutdown()
        shards = sorted(metrics_dir.glob("metrics-*.jsonl"))
        assert shards
        last = json.loads(shards[-1].read_text().splitlines()[-1])
        resource_attrs = last["resource_metrics"][0]["resource"]["attributes"]
        assert resource_attrs["service.instance.id"] == minted
    finally:
        reset_for_testing()


def test_a_failed_mint_costs_only_the_attribute(tmp_path, monkeypatch):
    # An unwritable data dir must cost the LABEL, never the recorder: the
    # build fails soft and keeps exporting. What it falls back to is NOT an
    # unlabelled resource -- the SDK substitutes its own per-process uuid4 --
    # so this pins the shape of that fallback rather than claiming absence.
    reset_for_testing()
    _own_home(monkeypatch, tmp_path)
    metrics_dir = tmp_path / "m"
    _patch_config(
        monkeypatch,
        enabled=True,
        local_dir=str(metrics_dir),
        export_interval_seconds=3600,
    )

    def _boom(*, create=False):
        if create:
            raise OSError("data dir unwritable")
        return ""

    monkeypatch.setattr(beacon, "install_id", _boom)
    try:
        rec = get_recorder()
        assert rec.enabled is True
        rec.counter("kirocrew.session.idle_expired", attrs={"turn_active": False})
        provider_shutdown()
        shards = sorted(metrics_dir.glob("metrics-*.jsonl"))
        assert shards
        last = json.loads(shards[-1].read_text().splitlines()[-1])
        resource_attrs = last["resource_metrics"][0]["resource"]["attributes"]
        # Present, but the SDK's per-process uuid4 (dashed, 36 chars) -- never
        # mistakable for our persisted 32-char lowercase-hex install id.
        substitute = resource_attrs["service.instance.id"]
        assert re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", substitute)
        assert not re.fullmatch(r"[0-9a-f]{32}", substitute)
        # The rest of the resource is untouched by the failed mint.
        assert resource_attrs["service.name"] == "kirocrew"
        assert "os.type" in resource_attrs
    finally:
        reset_for_testing()


def test_a_disabled_install_never_creates_the_id(tmp_path, monkeypatch):
    # The mint sits in the live branch only, so turning telemetry OFF creates
    # nothing on disk — enabling metrics is what consents to the id existing.
    reset_for_testing()
    _own_home(monkeypatch, tmp_path)
    _patch_config(monkeypatch, enabled=False, local_dir=str(tmp_path / "m"))
    try:
        assert get_recorder().enabled is False
        assert beacon.install_id(create=False) == "", "a disabled build minted the id"
    finally:
        reset_for_testing()


def test_channel_and_install_type_stay_absent():
    # Deliberate exclusions (see the function docstring): the distribution
    # channel narrows the anonymity crowd a stable id hides in, and there is
    # no reliable install-type detection. Their absence is a decision, so it
    # gets a regression guard.
    attrs = provider._resource_attributes()
    for key in attrs:
        lowered = key.lower()
        assert "channel" not in lowered
        assert "distribution" not in lowered
        assert "install_type" not in lowered


def test_exported_shard_carries_the_resource(tmp_path, monkeypatch):
    beacon.install_id(create=True)
    reset_for_testing()
    _patch_config(
        monkeypatch,
        enabled=True,
        local_dir=str(tmp_path),
        export_interval_seconds=3600,
    )
    try:
        rec = get_recorder()
        assert rec.enabled is True
        rec.counter("kirocrew.session.idle_expired", attrs={"turn_active": False})
        # shutdown() performs the final flush on the calling thread.
        provider_shutdown()

        shards = sorted(tmp_path.glob("metrics-*.jsonl"))
        assert shards, "no shard written by the final flush"
        record = json.loads(shards[0].read_text().splitlines()[0])
        resource_attrs = record["resource_metrics"][0]["resource"]["attributes"]
        assert resource_attrs["service.name"] == "kirocrew"
        assert resource_attrs["service.version"] == beacon.release(kiro_crew.__version__)
        assert resource_attrs["os.type"] == platform.system().lower()
        assert resource_attrs["host.cpu.logical_count"] == os.cpu_count()
        assert resource_attrs["process.pid"] == os.getpid()
        assert "service.instance.id" in resource_attrs
    finally:
        reset_for_testing()
