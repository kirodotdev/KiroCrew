"""Deploy handlers for a webapp artifact, and the payloads the dashboard keys its affordances off.

A webapp slug deploys the app's built static root through the local-dir route, and is
refused with a reason when that root is missing, outside the allowed roots or empty. A
finite TTL without the reaper stack is a keyed 409 whose remediation is a runnable
command; a successful deploy backfills the fields the deploy card reads; a refusal's
banner sentence carries no jargon; a pending confirm survives a deploy that raises.
"""

from __future__ import annotations

import json as _j2
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from deploy_web_handlers_helpers import _force_posix_shell  # noqa: F401
from deploy_web_handlers_helpers import _isolate_config  # noqa: F401
from deploy_web_handlers_helpers import _mock_base_stack  # noqa: F401
from deploy_web_handlers_helpers import _run, _set_profile

from kiro_crew.deploy import engine, handlers

# --- kind=webapp direct deploy -------------------------------------------
#
# A webapp artifact's content is an app SUMMARY, not deployable HTML, so the slug
# resolves to its built static root and deploys through the local_dir route,
# which is what carries the staging snapshot, the sensitive-path walk and the
# scan.


def _webapp_store(app_dir: Path | str, kind: str = "webapp"):
    """A store whose single artifact is a webapp pointing at ``app_dir``.

    Accepts a bare string so a test can pass "" — ``str(Path(""))`` is ".", which
    would exercise the containment branch instead of the empty-metadata one.
    """
    meta = SimpleNamespace(app_dir=str(app_dir))
    art = SimpleNamespace(kind=kind, content="app summary", name="Terrace", webapp_metadata=meta)
    return SimpleNamespace(get=lambda slug: art)


@pytest.fixture
def webapp_tree(tmp_path, monkeypatch):
    """An allow-listed workspace holding one in-contract app tree.

    ``public/`` is the built static root; ``src/`` is the app's own source,
    which must never reach a public bucket.
    """
    ws = tmp_path / "workspace"
    app_dir = ws / "terrace"
    (app_dir / "public").mkdir(parents=True)
    (app_dir / "public" / "index.html").write_text("<html>terrace</html>")
    (app_dir / "src").mkdir()
    (app_dir / "src" / "build.config.json").write_text('{"internal": "layout"}')
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [ws.resolve()])
    return app_dir


def test_webapp_slug_previews_instead_of_being_refused(monkeypatch, webapp_tree):
    """The confirm gate is reached, so the page has something to confirm."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(webapp_tree), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(
        handlers._do_deploy({"site_id": "terrace", "artifact_slug": "terrace-app"})
    )
    assert status == 200
    assert payload["requires_confirm"] is True
    assert payload["scan"] == "clean"


def test_webapp_deploy_publishes_only_the_built_public_root(monkeypatch, webapp_tree):
    """The app's own source must not travel to a world-readable bucket.

    This is the reason ``app_dir`` is not a fallback web root: publishing it
    whole would upload build config and sources beside the built page.
    """
    _set_profile(monkeypatch)
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(webapp_tree), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    published: dict = {}

    def fake_deploy(sid, src, p, r):
        published["names"] = sorted(q.name for q in Path(src).rglob("*") if q.is_file())
        return {
            "site_id": sid,
            "url": "https://d/",
            "reused": False,
            "bucket": "b",
            "distribution_id": "D",
            "status": "InProgress",
        }

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    status, _payload = _run(
        handlers._do_deploy(
            {"site_id": "terrace", "artifact_slug": "terrace-app", "confirm": True, "ttl_hours": 0}
        )
    )
    assert status == 200
    assert published["names"] == ["index.html"]


def test_webapp_without_built_public_dir_is_refused_with_a_reason(monkeypatch, tmp_path):
    """No built root yet: a named precondition, not a blank failure.

    The dashboard turns this into the "Deploy via agent" affordance, so the
    reason has to say which precondition failed.
    """
    _set_profile(monkeypatch)
    ws = tmp_path / "workspace"
    app_dir = ws / "unbuilt"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text("<html>loose</html>")
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [ws.resolve()])
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(app_dir), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(
        handlers._do_deploy({"site_id": "unbuilt", "artifact_slug": "unbuilt-app"})
    )
    assert status == 400
    assert payload["code"] == "webapp_root_unavailable"
    assert "public/" in payload["details"]


def test_webapp_app_dir_outside_allowed_roots_is_refused(monkeypatch, tmp_path):
    """An app_dir is LLM-written, so containment is re-checked at deploy time."""
    _set_profile(monkeypatch)
    outside = tmp_path / "elsewhere" / "app"
    (outside / "public").mkdir(parents=True)
    (outside / "public" / "index.html").write_text("<html>x</html>")
    monkeypatch.setattr(
        handlers, "_allowed_local_roots", lambda: [(tmp_path / "workspace").resolve()]
    )
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(outside), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy({"site_id": "x", "artifact_slug": "x-app"}))
    assert status == 400
    assert payload["code"] == "webapp_root_unavailable"


def test_webapp_with_empty_app_dir_is_refused_with_a_reason(monkeypatch):
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _webapp_store(""), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy({"site_id": "x", "artifact_slug": "x-app"}))
    assert status == 400
    assert payload["code"] == "webapp_root_unavailable"


def test_non_webapp_artifact_still_renders_its_content(monkeypatch, webapp_tree):
    """Regression guard: only kind=webapp takes the new route.

    Every other kind keeps staging its own content as standalone HTML, so the
    resolver answers None rather than claiming the artifact has a static root.
    """
    monkeypatch.setattr(
        handlers,
        "get_default_store",
        lambda: _webapp_store(webapp_tree, kind="html"),
        raising=False,
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    assert handlers.resolve_webapp_public_dir("any-slug") is None


def test_resolve_webapp_public_dir_uses_a_supplied_artifact_without_reading_again(
    webapp_tree, monkeypatch
):
    """A caller that already read the artifact gets the root from THAT read.

    The deploy path needs the content root and the artifact's generation to
    describe the same artifact. Two adjacent reads of one slug can straddle a
    delete-and-recreate, which would pair a root built from one generation with
    an identity read from another, so the resolver takes the artifact the caller
    already holds and does not read the store again.
    """
    reads = []

    def _counting_store():
        store = _webapp_store(webapp_tree)
        inner = store.get

        def get(slug):
            reads.append(slug)
            return inner(slug)

        return SimpleNamespace(get=get)

    monkeypatch.setattr(handlers, "get_default_store", _counting_store, raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    art = handlers.get_default_store().get("terrace")
    assert reads == ["terrace"]
    root = handlers.resolve_webapp_public_dir("terrace", art)
    assert root is not None and root.name == "public"
    # Still one read: the resolver used the artifact it was handed.
    assert reads == ["terrace"]


# --- reaper precondition affordances --------------------------------------
#
# A finite TTL requires auto-cleanup infrastructure. When the account lacks it,
# the 409 payload has to be enough for the dashboard to offer a way forward
# rather than only showing the wall.


def test_reaper_remediation_is_a_runnable_absolute_command(monkeypatch, tmp_path):
    """The bare script name was not runnable: it is on nobody's PATH.

    The path is also not a constant — installs rooted at ``~/.kirocrew`` and at
    ``~/.kiro/crew`` both occur — so it is resolved from the live skills dir
    rather than spelled out.
    """
    import kiro_crew.skills as skills_mod

    fake_skills = tmp_path / "skills"
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: fake_skills)
    cmd = handlers._reaper_remediation("myprofile", "us-west-2")
    assert cmd.startswith(str(fake_skills))
    assert cmd.endswith("--profile myprofile --region us-west-2")
    assert "artifact-deploy/scripts/install-reaper.sh" in cmd.replace(os.sep, "/")


def test_reaper_remediation_survives_an_unresolvable_skills_root(monkeypatch):
    """Remediation text must never be what breaks the 409."""
    import kiro_crew.skills as skills_mod

    def boom():
        raise RuntimeError("no skills root")

    monkeypatch.setattr(skills_mod, "skills_dir", boom)
    cmd = handlers._reaper_remediation("p", "r")
    assert cmd == "install-reaper.sh --profile p --region r"


def test_base_remediation_names_the_template_by_absolute_path(monkeypatch, tmp_path):
    import kiro_crew.skills as skills_mod

    fake_skills = tmp_path / "skills"
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: fake_skills)
    cmd = handlers._base_remediation("myprofile", "us-west-2")
    template = str(fake_skills / "artifact-deploy" / "templates" / "base-stack.yaml")
    assert f"--template-file {template} " in cmd
    assert cmd.endswith("install-reaper.sh --profile myprofile --region us-west-2")


def test_base_remediation_survives_an_unresolvable_skills_root(monkeypatch):
    import kiro_crew.skills as skills_mod

    def boom():
        raise RuntimeError("no skills root")

    monkeypatch.setattr(skills_mod, "skills_dir", boom)
    cmd = handlers._base_remediation("", "")
    assert cmd == (
        "aws cloudformation deploy --stack-name kirocrew-deploy-base "
        "--template-file templates/base-stack.yaml --no-fail-on-empty-changeset "
        "--tags kirocrew:managed=true && install-reaper.sh"
    )


def test_base_remediation_matches_deploy_sh_base_step():
    """The 409 hint repeats deploy.sh's base-stack step; keep the two in sync."""
    import re

    script = (
        Path(handlers.__file__).parent / "skills" / "artifact-deploy" / "scripts" / "deploy.sh"
    ).read_text(encoding="utf-8")
    block = script.split('"${AWS[@]}" cloudformation deploy', 1)[1].split("\n\n", 1)[0]
    script_flags = re.findall(r"--[a-z-]+", block)
    hint = handlers._base_remediation("", "").split(" && ")[0]
    hint_flags = re.findall(r"--[a-z-]+", hint)
    assert hint_flags == script_flags
    assert "'kirocrew:managed=true'" in block and "kirocrew:managed=true" in hint
    assert '--template-file "$TEMPLATE"' in block
    assert 'TEMPLATE="$SCRIPT_DIR/../templates/base-stack.yaml"' in script


def test_finite_ttl_without_base_stack_returns_a_keyed_409(monkeypatch, webapp_tree):
    """The dashboard keys its two affordances off `code`, not the sentence."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(webapp_tree), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    def no_stacks(args, profile, timeout):
        return 1, "", "does not exist"

    monkeypatch.setattr(engine, "run_aws", no_stacks)
    status, payload = _run(
        handlers._do_deploy(
            {"site_id": "terrace", "artifact_slug": "terrace-app", "confirm": True, "ttl_hours": 72}
        )
    )
    assert status == 409
    assert payload["code"] == "reaper_required"
    # The banner sentence names no stack and no parameter: it says what happened
    # and what the user can do. The technical form moves to `details`, which the
    # UI shows behind a toggle.
    assert "kirocrew-deploy-base" not in payload["error"]
    assert "ttl_hours" not in payload["error"]
    assert "72 hours" in payload["error"]
    assert "kirocrew-deploy-base" in payload["details"]
    assert payload["remediation"].endswith("--profile p --region us-west-2")


def test_persistent_ttl_needs_no_reaper_stack(monkeypatch, webapp_tree):
    """ttl_hours=0 is the escape the 409's first button re-runs with."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(webapp_tree), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    def no_stacks(args, profile, timeout):
        return 1, "", "does not exist"

    monkeypatch.setattr(engine, "run_aws", no_stacks)
    monkeypatch.setattr(
        engine,
        "deploy",
        lambda sid, src, p, r: {
            "site_id": sid,
            "url": "https://d/",
            "reused": False,
            "bucket": "b",
            "distribution_id": "D",
            "status": "InProgress",
        },
    )
    status, payload = _run(
        handlers._do_deploy(
            {"site_id": "terrace", "artifact_slug": "terrace-app", "confirm": True, "ttl_hours": 0}
        )
    )
    assert status == 200
    assert payload.get("code") != "reaper_required"


def test_successful_deploy_backfills_the_fields_the_card_reads(monkeypatch, webapp_tree):
    """A dashboard deploy must flip the card itself, with no agent involved.

    The card's "not deployed" hero is keyed off deploy_target.public_url, so a
    deploy that persisted only distribution_id succeeded while still advertising
    the app as undeployed.
    """
    _set_profile(monkeypatch)
    written: dict = {}

    class _Store:
        def get(self, _slug):
            return SimpleNamespace(
                kind="webapp",
                content="app summary",
                name="Terrace",
                webapp_metadata=SimpleNamespace(
                    app_dir=str(webapp_tree),
                    deploy_target=SimpleNamespace(
                        public_url="", profile="", region="", distribution_id=""
                    ),
                    lifecycle=SimpleNamespace(
                        status="draft",
                        created_at="",
                        expires_at=None,
                        persistent=False,
                        ttl_hours=72,
                    ),
                ),
            )

        def update(self, slug, **kwargs):
            written["slug"] = slug
            written["meta"] = kwargs.get("webapp_metadata")
            written["event_type"] = kwargs.get("event_type")

    monkeypatch.setattr(handlers, "get_default_store", lambda: _Store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    monkeypatch.setattr(engine, "run_aws", lambda args, profile, timeout: (1, "", "no stack"))
    monkeypatch.setattr(
        engine,
        "deploy",
        lambda sid, src, p, r: {
            "site_id": sid,
            "url": "https://d111.cloudfront.net/",
            "reused": False,
            "bucket": "b",
            "distribution_id": "DIST123",
            "status": "InProgress",
        },
    )

    status, _payload = _run(
        handlers._do_deploy(
            {"site_id": "terrace", "artifact_slug": "terrace-app", "confirm": True, "ttl_hours": 0}
        )
    )
    assert status == 200
    meta = written["meta"]
    assert meta.deploy_target.public_url == "https://d111.cloudfront.net/"
    assert meta.deploy_target.distribution_id == "DIST123"
    assert meta.deploy_target.profile == "p"
    assert meta.lifecycle.status == "live"
    # ttl_hours=0 is persistent, and this field's persistent value is None.
    assert meta.lifecycle.persistent is True
    assert meta.lifecycle.expires_at is None
    assert written["event_type"] == "edited"


def test_backfill_failure_does_not_fail_a_successful_deploy(monkeypatch, webapp_tree):
    """The deploy already happened; a metadata write is not allowed to undo it."""
    _set_profile(monkeypatch)

    class _Store:
        def get(self, _slug):
            return SimpleNamespace(
                kind="webapp",
                content="c",
                name="n",
                webapp_metadata=SimpleNamespace(
                    app_dir=str(webapp_tree),
                    deploy_target=SimpleNamespace(
                        public_url="", profile="", region="", distribution_id=""
                    ),
                    lifecycle=SimpleNamespace(
                        status="draft",
                        created_at="",
                        expires_at=None,
                        persistent=False,
                        ttl_hours=72,
                    ),
                ),
            )

        def update(self, *_a, **_k):
            raise RuntimeError("store is read-only right now")

    monkeypatch.setattr(handlers, "get_default_store", lambda: _Store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    monkeypatch.setattr(engine, "run_aws", lambda args, profile, timeout: (1, "", "no stack"))
    monkeypatch.setattr(
        engine,
        "deploy",
        lambda sid, src, p, r: {
            "site_id": sid,
            "url": "https://d/",
            "reused": False,
            "bucket": "b",
            "distribution_id": "D",
            "status": "InProgress",
        },
    )

    status, payload = _run(
        handlers._do_deploy(
            {"site_id": "terrace", "artifact_slug": "terrace-app", "confirm": True, "ttl_hours": 0}
        )
    )
    assert status == 200
    assert payload["site_id"] == "terrace"


# --- plain banner / technical details split --------------------------------
#
# Each field has one audience: `error` is the sentence in the red banner,
# `details` holds the stack and parameter names behind the UI's Details toggle,
# `remediation` is the runnable command, `code` is what the buttons key off.

_JARGON = (
    "kirocrew-deploy-base",
    "kirocrew-deploy-reaper",
    "install-reaper.sh",
    "ttl_hours",
    "reaper",
    "webapp_metadata",
    "app_dir",
)


def test_reaper_banner_sentence_carries_no_jargon(monkeypatch, webapp_tree):
    _set_profile(monkeypatch)
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(webapp_tree), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    monkeypatch.setattr(engine, "run_aws", lambda a, p, t: (1, "", "no stack"))
    status, payload = _run(
        handlers._do_deploy(
            {"site_id": "terrace", "artifact_slug": "terrace-app", "confirm": True, "ttl_hours": 72}
        )
    )
    assert status == 409
    banner = payload["error"].lower()
    for term in _JARGON:
        assert term.lower() not in banner, f"banner leaked {term!r}: {payload['error']}"
    # It still has to say what happened and offer a way out.
    assert "72 hours" in payload["error"]
    assert "permanent" in payload["error"]
    # And nothing technical is LOST — it moved.
    assert "kirocrew-deploy-base" in payload["details"]


def test_unbuilt_app_banner_sentence_carries_no_jargon(monkeypatch, tmp_path):
    _set_profile(monkeypatch)
    ws = tmp_path / "workspace"
    app_dir = ws / "unbuilt"
    app_dir.mkdir(parents=True)
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [ws.resolve()])
    monkeypatch.setattr(
        handlers, "get_default_store", lambda: _webapp_store(app_dir), raising=False
    )
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(
        handlers._do_deploy({"site_id": "unbuilt", "artifact_slug": "unbuilt-app"})
    )
    assert status == 400
    banner = payload["error"].lower()
    for term in _JARGON:
        assert term.lower() not in banner, f"banner leaked {term!r}: {payload['error']}"
    assert "public/" in payload["details"]


def test_empty_app_dir_banner_sentence_carries_no_jargon(monkeypatch):
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _webapp_store(""), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy({"site_id": "x", "artifact_slug": "x-app"}))
    assert status == 400
    banner = payload["error"].lower()
    for term in _JARGON:
        assert term.lower() not in banner, f"banner leaked {term!r}: {payload['error']}"
    assert "app_dir" in payload["details"]


# --- pending confirm survives a raising deploy ------------------------------
#
# The confirm handler CLAIMS the entry atomically before deploying, so an
# exception that escapes `_do_deploy` takes the entry with it: the row vanishes
# from the card and the response carries no usable body. `_do_deploy` turns an
# engine.AWSError into a 502 itself; everything else reaches the handler.


def test_pending_confirm_readds_entry_when_deploy_raises(monkeypatch, tmp_path):
    """A TimeoutExpired mid-deploy must leave a retryable entry and a reason."""
    import subprocess

    from kiro_crew.deploy import pending as pending_mod

    # The handler refuses with 409 when the resolved profile/region drifted from
    # the previewed one, so the stored entry and the live config have to agree
    # before the deploy is reached at all.
    _set_profile(monkeypatch, profile="p", region="us-west-2")
    monkeypatch.setattr(pending_mod, "_store_path", lambda: tmp_path / "pending.json")
    entry = pending_mod.add_pending(
        {
            "site_id": "shopfront",
            "artifact_slug": "shopfront-app",
            "profile": "p",
            "region": "us-west-2",
            "ttl_hours": 0,
        }
    )

    async def boom(_params, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="aws s3api create-bucket", timeout=60)

    monkeypatch.setattr(handlers, "_do_deploy", boom)

    class _Req:
        match_info = {"id": entry["id"]}
        headers: dict = {}
        cookies: dict = {}

        async def json(self):
            return {}

    monkeypatch.setattr(handlers, "_is_internal_secret_request", lambda _r: False)
    monkeypatch.setattr(handlers, "_deny_restricted", lambda _r, _a: None)

    resp = _run(handlers._handle_pending_confirm(_Req()))
    assert resp.status == 502
    body = _j2.loads(resp.body.decode())
    assert body["code"] == "deploy_interrupted"
    # A plain sentence for the banner, the exception class behind Details. The
    # sentence must not claim nothing was published: the exception can fire after
    # the S3 sync succeeded, so content may already be live.
    assert "not known whether anything was published" in body["error"]
    assert "nothing was published" not in body["error"]
    assert "TimeoutExpired" in body["details"]
    # And the entry is retryable rather than lost.
    assert [e["site_id"] for e in pending_mod.list_pending()] == ["shopfront"]
