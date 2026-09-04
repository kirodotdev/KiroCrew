"""Compatibility tests for the GitLab client facade boundaries."""

from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl


def test_api_facade_injects_current_patch_bindings(monkeypatch):
    patched_run = object()
    captured = {}

    def api_helper(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(gl, "_glab_run", patched_run)
    monkeypatch.setattr(gl, "_PAGE_SIZE", 17)
    monkeypatch.setattr(gl, "_MAX_PAGES", 9)
    monkeypatch.setattr(gl._transport, "glab_api", api_helper)

    assert gl._glab_api("user", host="gitlab.com", paginate=True) == {"ok": True}
    assert captured["run"] is patched_run
    assert captured["page_size"] == 17
    assert captured["max_pages"] == 9
