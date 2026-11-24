"""``cloud/wizard.py``: a rerun finishes the stack an interrupted launch saved.

A new launch saves its tag before provisioning, so after a Ctrl+C or a closed terminal the
record names a stack still ``CREATE_IN_PROGRESS``, with no ``InstanceId`` output yet. The
rerun must wait for that stack and resume it, never clear its pointer and create a second
billing stack. Real ``launch()`` and a real record on disk; every AWS, SSM and sign-in seam
is a stand-in, so nothing runs ``aws``.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam, login, ssm, wizard
from kiro_crew.cloud.launch_state import LaunchState

_SCOPE = "--region us-west-2 --profile dev"


@pytest.fixture
def launched(tmp_path, monkeypatch) -> list[str]:
    """A saved ``kc-old`` record and a signed-in, connectable box; returns registered ids."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    LaunchState.record(profile="dev", region="us-west-2", last_tag="kc-old")
    assert LaunchState.load().last_tag == "kc-old", "the fixture did not save a record"
    registered: list[str] = []
    monkeypatch.setattr(
        iam,
        "reachability_check",
        lambda *_a, **_k: {
            "reachable": True,
            "account": "123456789012",
            "ec2_reachable": True,
            "cloudformation_reachable": True,
            "ssm_reachable": True,
        },
    )
    monkeypatch.setattr(wizard.ssm, "session_manager_plugin_installed", lambda: True)
    monkeypatch.setattr(login, "is_logged_in", lambda *_a, **_k: True)
    monkeypatch.setattr(
        connect_mod,
        "connect",
        lambda instance_id, *_a, **_k: connect_mod.Connection(
            instance_id=instance_id,
            local_port=5599,
            remote_port=5476,
            token="tok",
            url="http://localhost:5599/?token=tok",
            ready=True,
        ),
    )
    monkeypatch.setattr(
        connect_mod,
        "register_instance",
        lambda instance_id, **_k: registered.append(instance_id) or "reg-1",
    )
    monkeypatch.setattr(wizard, "_sleep", lambda *_a: None)
    monkeypatch.setattr(ec2, "list_stack_events", lambda *_a, **_k: [])
    monkeypatch.setattr(wizard, "_fetch_bootstrap_log", lambda *_a, **_k: [])
    monkeypatch.setattr(ec2, "_instance_state", lambda *_a, **_k: "running")
    monkeypatch.setattr(ssm, "instance_is_managed", lambda *_a, **_k: True)
    return registered


def _creating_then(monkeypatch, final: dict) -> list[str]:
    """Describe reads CREATE_IN_PROGRESS until the fake wait ran, then *final*."""
    waited: list[str] = []

    def fake_describe(tag, *_a, **_k):
        if not waited:
            return {
                "tag": tag,
                "exists": True,
                "stack_name": f"kirocrew-{tag}",
                "stack_status": "CREATE_IN_PROGRESS",
                "instance_id": "",
                "region": "us-west-2",
            }
        return {"tag": tag, "exists": True, "stack_name": f"kirocrew-{tag}", **final}

    monkeypatch.setattr(ec2, "describe", fake_describe)
    monkeypatch.setattr(
        ec2,
        "list_stacks",
        lambda *_a, **_k: [
            {"tag": "kc-old", "stack_name": "kirocrew-kc-old", "stack_status": "CREATE_IN_PROGRESS"}
        ],
    )
    monkeypatch.setattr(ec2, "live_instance_tags", lambda *_a, **_k: set())
    monkeypatch.setattr(ec2, "wait_for_create", lambda tag, *_a, **_k: waited.append(tag) or True)

    def _must_not_deploy(**_k):
        raise AssertionError("a rerun created a second stack")

    monkeypatch.setattr(wizard, "_deploy_with_progress", _must_not_deploy)
    return waited


def test_a_rerun_waits_for_the_saved_stack_and_resumes_it(launched, monkeypatch, capsys):
    waited = _creating_then(
        monkeypatch,
        {"stack_status": "CREATE_COMPLETE", "instance_id": "i-old", "region": "us-west-2"},
    )

    assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 0

    assert waited == ["kc-old"]
    assert launched == ["i-old"]
    assert LaunchState.load().last_tag == "kc-old"
    out = capsys.readouterr().out
    assert "kirocrew-kc-old is still being created" in out
    assert "not ready yet" not in out


def test_a_stack_that_fails_while_waited_on_is_named_and_kept(launched, monkeypatch, capsys):
    _creating_then(monkeypatch, {"stack_status": "ROLLBACK_IN_PROGRESS", "instance_id": ""})
    monkeypatch.setattr(
        ec2, "find_stack", lambda *_a, **_k: {"StackStatus": "ROLLBACK_IN_PROGRESS"}
    )

    assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 1

    assert launched == []
    assert LaunchState.load().last_tag == "kc-old"
    out = capsys.readouterr().out
    assert "did not finish creating (ROLLBACK_IN_PROGRESS)" in out
    assert f"kirocrew cloud destroy --tag kc-old {_SCOPE}" in out


def test_ctrl_c_while_waiting_names_the_stack_and_keeps_the_record(launched, monkeypatch, capsys):
    _creating_then(monkeypatch, {})

    def _interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "_wait_for_creating_stack", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        wizard.launch(profile="dev", region="us-west-2", assume_yes=True)

    assert LaunchState.load().last_tag == "kc-old"
    out = capsys.readouterr().out
    assert "AWS may still be creating kirocrew-kc-old" in out
    assert f"kirocrew cloud status --tag kc-old {_SCOPE}" in out
    assert f"kirocrew cloud destroy --tag kc-old {_SCOPE}" in out


def test_the_wait_streams_progress_until_the_stack_settles(monkeypatch):
    """The wait runs ``ec2.wait_for_create`` for the tag and streams while it runs."""
    waited: list[tuple] = []
    streamed: list[str] = []
    monkeypatch.setattr(ec2, "wait_for_create", lambda *a: waited.append(a) or True)
    monkeypatch.setattr(
        wizard, "_stream_progress", lambda worker, tag, *_a: worker.join() or streamed.append(tag)
    )

    wizard._wait_for_creating_stack("kc-old", "dev", "us-west-2")

    assert waited == [("kc-old", "dev", "us-west-2")]
    assert streamed == ["kc-old"]
