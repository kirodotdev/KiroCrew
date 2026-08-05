"""R37 regression (round-37 Codex finding on 1a04644).

F1: profile-registration capacity must be enforced INSIDE the locked registry
    transaction — the pre-lock check alone allows two concurrent POSTs at 49
    profiles to append to 51, and load_registry()'s truncation then silently
    drops a registered profile.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HANDLERS = (REPO / "src" / "kiro_crew" / "deploy" / "handlers.py").read_text(encoding="utf-8")


class TestF1CapacityUnderLock:
    def test_capacity_check_inside_locked_registry(self):
        # extract the _post_rmw body and assert the capacity check happens
        # inside the locked_registry() context
        m = re.search(r"def _post_rmw\(\).*?(?=\n    reg, cap_err)", HANDLERS, re.DOTALL)
        assert m, "_post_rmw not found in expected shape"
        body = m.group(0)
        assert "locked_registry()" in body
        lock_idx = body.index("locked_registry()")
        cap_idx = body.index('>= 50')
        append_idx = body.index('reg["profiles"].append')
        assert lock_idx < cap_idx < append_idx, (
            "capacity must be re-checked under the lock, before the append"
        )

    def test_capacity_violation_is_audited_and_rejected(self):
        # the cap_err path must audit the denial and return 400
        idx = HANDLERS.index("reg, cap_err = ")
        block = HANDLERS[idx:idx + 400]
        assert '_audit("profile_register", name, "denied"' in block
        assert "status=400" in block
