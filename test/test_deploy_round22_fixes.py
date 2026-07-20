"""Regression tests for Round-22 findings (KiroCrew PR #6).

F1: shell deploy snapshots reject hardlinked source files BEFORE cp -a
    (the shell twin of _stage_tree_safe's st_nlink > 1 gate — R19/R20).
F2: teardown.sh verifies the stack's kirocrew:site + kirocrew:managed tags
    before delete-stack (the direct-teardown twin of the reaper gate — R21).
"""
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / (
    "src/kiro_crew/deploy/skills/artifact-deploy/scripts"
)


def test_f1_deploy_sh_rejects_source_hardlinks_before_copy():
    src = (_SCRIPTS / "deploy.sh").read_text()
    assert "-links +1" in src
    # The check must run on the SOURCE tree and precede the cp -a
    # (snapshot copies have nlink=1, so a post-copy check would be dead).
    assert src.index("-links +1") < src.index('cp -a "$APP_DIR/."')


def test_f1_deploy_backend_sh_rejects_source_hardlinks_before_copy():
    src = (_SCRIPTS / "deploy-backend.sh").read_text()
    assert "-links +1" in src
    assert src.index("-links +1") < src.index('cp -a "$SRC/."')


def test_f2_teardown_gates_stack_deletion_on_identity_tags():
    src = (_SCRIPTS / "teardown.sh").read_text()
    assert "kirocrew:site" in src
    assert "kirocrew:managed" in src
    # The tag check precedes the delete-stack call.
    assert src.index("kirocrew:site") < src.index(
        'delete-stack --stack-name "kirocrew-deploy-app-$SLUG"'
    )
    # Fail-closed: mismatch exits with an error.
    assert "refusing to delete" in src
