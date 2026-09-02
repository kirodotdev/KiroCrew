"""Tests for heroImage/screenshotsDark blob proxy conversion in registry._merge_manifest."""
from kiro_crew.apps.registry import _merge_manifest


def test_merge_manifest_converts_hero_image_to_blob_proxy():
    entry = {"name": "test-app", "repo": "TestApp"}
    manifest = {"heroImage": "assets/hero-light.svg"}
    result = _merge_manifest(entry, manifest)
    assert result["heroImage"] == "/api/apps/blob?repo=TestApp&path=assets/hero-light.svg"


def test_merge_manifest_converts_hero_image_dark_to_blob_proxy():
    entry = {"name": "test-app", "repo": "TestApp"}
    manifest = {"heroImageDark": "assets/hero-dark.svg"}
    result = _merge_manifest(entry, manifest)
    assert result["heroImageDark"] == "/api/apps/blob?repo=TestApp&path=assets/hero-dark.svg"


def test_merge_manifest_converts_hero_image_detail_to_blob_proxy():
    entry = {"name": "test-app", "repo": "TestApp"}
    manifest = {"heroImageDetail": "assets/hero-detail-light.svg"}
    result = _merge_manifest(entry, manifest)
    assert result["heroImageDetail"] == "/api/apps/blob?repo=TestApp&path=assets/hero-detail-light.svg"


def test_merge_manifest_converts_hero_image_detail_dark_to_blob_proxy():
    entry = {"name": "test-app", "repo": "TestApp"}
    manifest = {"heroImageDetailDark": "assets/hero-detail-dark.svg"}
    result = _merge_manifest(entry, manifest)
    assert result["heroImageDetailDark"] == "/api/apps/blob?repo=TestApp&path=assets/hero-detail-dark.svg"


def test_merge_manifest_converts_screenshots_dark_to_blob_proxy():
    entry = {"name": "test-app", "repo": "TestApp"}
    manifest = {"screenshotsDark": ["assets/s1-dark.png", "assets/s2-dark.png"]}
    result = _merge_manifest(entry, manifest)
    assert result["screenshotsDark"] == [
        "/api/apps/blob?repo=TestApp&path=assets/s1-dark.png",
        "/api/apps/blob?repo=TestApp&path=assets/s2-dark.png",
    ]


def test_merge_manifest_skips_hero_when_no_repo():
    entry = {"name": "test-app"}
    manifest = {"heroImage": "assets/hero.svg"}
    result = _merge_manifest(entry, manifest)
    assert "heroImage" not in result or result.get("heroImage") == ""


def test_merge_manifest_skips_empty_hero():
    entry = {"name": "test-app", "repo": "TestApp"}
    manifest = {"heroImage": "", "heroImageDark": ""}
    result = _merge_manifest(entry, manifest)
    assert result.get("heroImage", "") == ""
    assert result.get("heroImageDark", "") == ""
