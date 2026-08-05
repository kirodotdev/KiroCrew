"""Regression tests for Round-20 findings (KiroCrew PR #6).

F1: deploy staging goes through _stage_tree_safe (hook-gated, hardlink-rejecting)
    instead of bare shutil.copytree.
F2: the reaper authorizes against the S3 prefix slug only — a manifest whose
    own "slug" field disagrees is treated as forged and nothing is deleted.
F3: the reaper IAM role grants cloudfront:ListTagsForResource and
    s3:GetBucketTagging (required by the R19 tag-verify gate).
F4: profiles PUT/DELETE responses redact the default profile name and error
    echoes, matching GET/POST (R18 F1).
"""
import re
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / (
    "src/kiro_crew/deploy/skills/artifact-deploy"
)


# --- F1: staging is wired through _stage_tree_safe ---


def test_f1_staging_uses_stage_tree_safe_not_copytree():
    """The local-dir staging branch must call _stage_tree_safe; a bare
    shutil.copytree of the source tree bypasses the per-file hook gate and
    hardlink rejection."""
    src = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/deploy/handlers.py"
    ).read_text(encoding="utf-8")
    # Both the fd-pinned branch and the non-Linux fallback stage via the helper.
    assert src.count("_stage_tree_safe(") >= 3  # def + 2 call sites
    # No staging call site copies the source tree with copytree anymore.
    assert 'shutil.copytree(\n                            f"/proc/self/fd/' not in src
    assert "staged_copy = shutil.copytree(" not in src


# --- F2: reaper authorizes against the prefix slug only ---


def _reaper_lambda_src() -> str:
    return (_SCRIPTS / "scripts/reaper_lambda/index.py").read_text(encoding="utf-8")


def test_f2_reaper_never_trusts_manifest_slug_for_authorization():
    src = _reaper_lambda_src()
    assert 'man.get("slug", slug)' not in src


def test_f2_forged_manifest_slug_is_refused():
    """A manifest whose slug field disagrees with the S3 prefix is forged:
    _reap_engine_arch must return without deleting anything."""
    src = _reaper_lambda_src()
    assert "engine_reap_slug_mismatch" in src
    # The gate sits at the top of _reap_engine_arch, before any AWS calls.
    fn = src.split("def _reap_engine_arch", 1)[1]
    gate_pos = fn.index("engine_reap_slug_mismatch")
    first_destructive = min(
        fn.index("delete_origin_access_control"),
        fn.index("delete_object"),
    )
    assert gate_pos < first_destructive


# --- F3: IAM role grants the tag-read permissions the R19 gate needs ---


def test_f3_reaper_role_grants_tag_read_permissions():
    yaml_src = (_SCRIPTS / "templates/reaper.yaml").read_text(encoding="utf-8")
    assert "cloudfront:ListTagsForResource" in yaml_src
    assert "s3:GetBucketTagging" in yaml_src


def test_f3_lambda_tag_calls_are_covered():
    """Every tag API the lambda calls must appear in the role policy."""
    lam = _reaper_lambda_src()
    yaml_src = (_SCRIPTS / "templates/reaper.yaml").read_text(encoding="utf-8")
    if "list_tags_for_resource" in lam:
        assert "cloudfront:ListTagsForResource" in yaml_src
    if "get_bucket_tagging" in lam:
        assert "s3:GetBucketTagging" in yaml_src


# --- F4: profiles PUT/DELETE responses are redacted ---


def test_f4_profile_update_delete_redact_default_and_errors():
    src = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/deploy/handlers.py"
    ).read_text(encoding="utf-8")
    # No handler returns the raw registry default anymore.
    assert '"default": result["default"],' not in src
    assert src.count('"default": _redact_text(str(result["default"]))') == 2
    # Error echoes that embed LLM-influenced values pass through _redact_text.
    assert re.search(
        r'_redact_text\(f"invalid profile name: \{e\}"\)', src
    )
    assert re.search(
        r"_redact_text\(f\"profile '\{name\}' not registered\"\)", src
    )
