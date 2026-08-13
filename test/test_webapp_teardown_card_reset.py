"""FU-6 regression (pod field-test 2026-07-20): teardown tombstone must not
leave a live-looking card.

mark_webapp_expired() must clear lifecycle.expires_at (no phantom countdown
next to the Expired badge) and deploy_target.public_url (no dead public link).
"""
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.deploy.webapp_types import (
    WebAppDeployTarget,
    WebAppLifecycle,
    WebAppMetadata,
)


def test_mark_webapp_expired_clears_countdown_and_url(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    art = store.create(name="Site", content="<h1>app</h1>", kind="webapp")
    art.webapp_metadata = WebAppMetadata(
        deploy_target=WebAppDeployTarget(
            public_url="https://d123.cloudfront.net/site/",
            profile="personal",
            region="us-west-2",
        ),
        lifecycle=WebAppLifecycle(
            status="live",
            expires_at="2026-07-22T13:00:00+00:00",
            persistent=False,
            ttl_hours=72,
        ),
    )
    store._write_meta(art)

    out = store.mark_webapp_expired(art.slug)

    assert out.webapp_metadata.lifecycle.status == "expired"
    assert out.webapp_metadata.lifecycle.expires_at is None, (
        "phantom TTL countdown must not survive teardown"
    )
    assert out.webapp_metadata.deploy_target.public_url == "", (
        "dead public link must not survive teardown"
    )
    # identity fields for deploy history stay intact
    assert out.webapp_metadata.deploy_target.profile == "personal"
