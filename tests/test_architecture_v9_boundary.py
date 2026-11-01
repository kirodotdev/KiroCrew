"""Architecture dependency directions are checked before release."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_byo_boundary_guard', ROOT / 'scripts' / 'boundary_guard.py')
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_leaf_seams_do_not_reverse_import_runtime_owners():
    assert module.check(ROOT) == []


def test_boundary_guard_rejects_absolute_relative_and_from_package_imports():
    forbidden = [
        'import kiro_byo.turn_runner',
        'from .acp_server import AcpServer',
        'from kiro_byo import turn_runner',
        'from backend.routes import x',
    ]
    for line in forbidden:
        assert module.forbidden_imports(line, 'example'), line
    assert module.forbidden_imports('text = "from .turn_runner import x"', 'example') == []
    assert module.forbidden_imports('from .recovery_policy import RecoveryPolicy', 'example') == []
