from kiro_crew.artifacts import ArtifactStore
from kiro_crew.testing.fixtures import seeded_home


def test_artifacts_library_fixture_drives_the_production_store() -> None:
    with seeded_home("artifacts-library") as home:
        store = ArtifactStore()
        assert store.root.resolve() == (home / "artifacts").resolve()

        listed = {artifact.slug for artifact in store.list()}
        assert listed == {
            "pagination-design",
            "queue-badge",
            "release-checklist",
        }
        artifacts = {slug: store.get(slug) for slug in listed}
        assert {slug: artifact.kind for slug, artifact in artifacts.items()} == {
            "pagination-design": "markdown",
            "queue-badge": "svg",
            "release-checklist": "widget",
        }
        assert artifacts["release-checklist"].version == 2

        first_version = store.get("release-checklist", version=1)
        assert "Contributors credited" in (artifacts["release-checklist"].content or "")
        assert "Contributors credited" not in (first_version.content or "")
        assert "Changelog section written" in (first_version.content or "")
