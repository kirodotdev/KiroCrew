"""Upstream interface experiment: fake-host allow tests are NOT Crew E2E."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import os
import tempfile
from pathlib import Path
import pytest

# experiments/ holds local-only upstream-interface prototypes and is excluded
# from the public repo (see .gitignore). When it is absent, skip this whole
# module rather than erroring on import.
pytest.importorskip("experiments.crew_external_harness_registry")
from experiments.crew_external_harness_registry import (
    AdmissionRefused, ExternalHarnessDescriptor, HostEvidence, ReferenceRegistry,
)


def _can_create_symlink() -> bool:
    """Probe real symlink-creation support. On Windows this needs Developer Mode
    or an elevated process (WinError 1314 otherwise); POSIX allows it normally."""
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "t"
        target.write_text("x", encoding="utf-8")
        try:
            (Path(d) / "link").symlink_to(target)
            return True
        except (OSError, NotImplementedError):
            return False


_SYMLINK_SUPPORTED = _can_create_symlink()


@pytest.fixture
def scenario(tmp_path):
    root = tmp_path / "trusted-app"
    root.mkdir()
    binary = root / "byo-acp"
    binary.write_bytes(b"fixture-only-not executable")
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    descriptor = ExternalHarnessDescriptor("kiro-byo", "Kiro BYO", binary,
                                           ("acp",), sha)
    grant = HostEvidence(True, True, True, True)
    def registry(evidence=grant):
        return ReferenceRegistry(install_root=root, approved_hashes={"kiro-byo": sha},
                                 host_evidence=lambda _: evidence,
                                 reserved_ids=frozenset({"kas", "codex"}))
    return registry, descriptor, root


def test_positive_is_only_mock_host_contract(scenario):
    make, desc, _ = scenario
    reg = make()
    reg.register(desc)
    assert reg.accepted_ids() == frozenset({"kiro-byo"})
    # No production Crew registry was changed by this experiment.


@pytest.mark.parametrize("missing", [
    "launch_adapter_wired", "executable_confined", "per_tool_gate_enforced",
    "worker_policy_wired",
])
def test_missing_host_enforcement_fails_closed(scenario, missing):
    make, desc, _ = scenario
    evidence = replace(HostEvidence(True,True,True,True), **{missing: False})
    reg = make(evidence)
    with pytest.raises(AdmissionRefused, match="host lacks"):
        reg.register(desc)
    assert not reg.accepted_ids()


def test_operator_digest_cannot_be_forged_in_manifest(scenario):
    make, desc, _ = scenario
    with pytest.raises(AdmissionRefused, match="digest"):
        make().register(replace(desc, executable_sha256="f" * 64))


def test_binary_changed_after_approval_fails(scenario):
    make, desc, _ = scenario
    desc.executable.write_bytes(b"changed")
    with pytest.raises(AdmissionRefused, match="changed"):
        make().register(desc)


@pytest.mark.skipif(not _SYMLINK_SUPPORTED,
                    reason="creating a symlink needs elevation/Developer Mode on this host (WinError 1314)")
def test_symlink_bypass_rejected(scenario):
    make, desc, root = scenario
    alias = root / "alias"
    alias.symlink_to(desc.executable)
    with pytest.raises(AdmissionRefused, match="non-symlink"):
        make().register(replace(desc, executable=alias))


def test_reserved_backend_impersonation_rejected(scenario):
    make, desc, _ = scenario
    with pytest.raises(AdmissionRefused, match="override"):
        make().register(replace(desc, backend_id="kas"))


def test_unknown_unapproved_identity_rejected(scenario):
    make, desc, _ = scenario
    with pytest.raises(AdmissionRefused, match="operator-approved"):
        make().register(replace(desc, backend_id="another-agent"))


def test_case_alias_and_whitespace_rejected(scenario):
    make, desc, _ = scenario
    for backend_id in ("Kiro-Byo", "kiro byo", " kiro-byo", "../kiro-byo"):
        with pytest.raises(AdmissionRefused, match="non-canonical"):
            make().register(replace(desc, backend_id=backend_id))


def test_duplicate_registration_is_refused(scenario):
    make, desc, _ = scenario
    reg = make()
    reg.register(desc)
    with pytest.raises(AdmissionRefused, match="duplicate"):
        reg.register(desc)


def test_no_host_mutation_or_auto_activation(scenario):
    import inspect
    from experiments import crew_external_harness_registry as ref
    source = inspect.getsource(ref)
    assert "register_selectable_backend(" not in source
    assert "subprocess.Popen(" not in source
    assert "os.system(" not in source
