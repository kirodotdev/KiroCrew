"""The ``browser`` MCP tool: its two gates, its post, and what it tells the model.

The tool runs in the ``kirocrew-core`` shim and posts one op to
``/api/browser/op``. Pinned here:

* governance (``capabilities.browse``) refuses OUTRIGHT, before any post, and
  nothing is offered as a fallback;
* the navigate gate refuses every non-public URL before any post, for
  ``navigate`` and for the URL a new tab opens with alike;
* a resolved session posts its ``dashboard:``-namespaced key in the
  ``X-Session-Key`` header AND in the body; an unresolved one posts a
  process-local body key and NO header (a fabricated header is refused where
  a missing one is simply absent);
* the gateway's refusal codes (``not_installed``, ``user_in_control``,
  ``password_field``, ...) are rendered as the text the model acts on, and
  no refusal ever points at another browser.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.mcp_tools import browser as mod


@pytest.fixture(autouse=True)
def _tool_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # The header encodability guard is orthogonal to what these tests exercise.
    monkeypatch.setattr(mod.mcp_core, "_session_key_header_error", lambda sk: None)
    # Default: governance permits browsing (its own deny is covered separately).
    monkeypatch.setattr(mod.mcp_core, "_vet_browse_governance", lambda s: None)
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1-2")


def _capture_post(recorder: list[tuple[str, str, dict, str, float]], status: int, payload: dict):
    def _fake(
        session_key: str, op: str, args: dict, session_header: str, timeout_s: float
    ) -> tuple[int, dict]:
        recorder.append((session_key, op, args, session_header, timeout_s))
        return status, payload

    return _fake


def _must_not_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
    raise AssertionError("must not POST")


def _ok(result: dict) -> dict:
    return {"ok": True, "result": {"ok": True, **result}}


# --- the advertisement -------------------------------------------------------


def test_schema_advertises_every_op_the_gateway_serves() -> None:
    (desc,) = mod.schemas()
    assert desc["name"] == "browser"
    assert tuple(desc["inputSchema"]["properties"]["op"]["enum"]) == harness_ops.OPS
    assert mod.BROWSER_OPS == harness_ops.OPS
    text = desc["description"]
    # The three things the model must know without reading a skill.
    assert "password" in text
    assert "waits until they hand it back" in text  # the hold, not a user_in_control refusal
    assert "Settings -> Browser" in text
    assert "playwright" not in text.lower()


def test_handlers_and_schemas_agree() -> None:
    assert set(mod.HANDLERS) == {d["name"] for d in mod.schemas()}


# --- the session identity ----------------------------------------------------


def test_resolved_session_posts_the_key_in_header_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, dict, str, float]] = []
    monkeypatch.setattr(mod, "_post_command", _capture_post(sent, 200, _ok({"title": "T"})))
    out = mod.browser("browser", {"op": "snapshot"})
    assert out.startswith("Browser snapshot")
    ((body_key, op, args, header, timeout),) = sent
    assert body_key == "dashboard:chat-1-2"
    assert header == "dashboard:chat-1-2"
    assert (op, args) == ("snapshot", {})
    assert timeout == mod.FAST_COMMAND_TIMEOUT_SECS


def test_unresolved_session_posts_a_local_key_and_no_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "")
    sent: list[tuple[str, str, dict, str, float]] = []
    monkeypatch.setattr(mod, "_post_command", _capture_post(sent, 200, _ok({})))
    out = mod.browser("browser", {"op": "console"})
    assert out == "Browser console: ok"
    ((body_key, _op, _args, header, _t),) = sent
    assert body_key.startswith("anonymous:")
    assert header == ""


def test_header_encodability_error_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.mcp_core, "_session_key_header_error", lambda sk: "bad header")
    monkeypatch.setattr(mod, "_post_command", _must_not_post)
    assert mod.browser("browser", {"op": "snapshot"}) == "Error: bad header"


def test_slow_ops_get_the_long_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, dict, str, float]] = []
    monkeypatch.setattr(mod, "_post_command", _capture_post(sent, 200, _ok({})))
    mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})
    mod.browser("browser", {"op": "wait_for", "args": {"time": 1}})
    mod.browser("browser", {"op": "click", "args": {"ref": "e1"}})
    assert [s[4] for s in sent] == [
        mod.SLOW_COMMAND_TIMEOUT_SECS,
        mod.SLOW_COMMAND_TIMEOUT_SECS,
        mod.FAST_COMMAND_TIMEOUT_SECS,
    ]


# --- governance --------------------------------------------------------------


def test_governance_deny_refuses_without_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.mcp_core, "_vet_browse_governance", lambda s: "browsing is disabled")
    monkeypatch.setattr(mod, "_post_command", _must_not_post)
    out = mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})
    assert out == "Error: browsing is disabled"


def test_governance_runs_before_argument_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied caller learns nothing about which ops exist."""
    monkeypatch.setattr(mod.mcp_core, "_vet_browse_governance", lambda s: "denied")
    assert mod.browser("browser", {"op": "nonsense"}) == "Error: denied"


# --- argument validation -----------------------------------------------------


def test_unknown_op_and_non_object_args_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_post_command", _must_not_post)
    assert mod.browser("browser", {"op": "evaluate"}).startswith("Error: op must be one of")
    assert (
        mod.browser("browser", {"op": "click", "args": "e1"}) == "Error: args must be a JSON object"
    )


def test_object_valued_arg_is_rejected_without_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_post_command", _must_not_post)
    out = mod.browser("browser", {"op": "type", "args": {"ref": "e1", "text": {"a": 1}}})
    assert out.startswith("Error: args values must be strings")


def test_list_of_scalars_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, dict, str, float]] = []
    monkeypatch.setattr(mod, "_post_command", _capture_post(sent, 200, _ok({"selected": ["a"]})))
    out = mod.browser("browser", {"op": "select_option", "args": {"ref": "e1", "values": ["a"]}})
    assert out == 'Browser select_option: {"selected": ["a"]}'


# --- the navigate gate -------------------------------------------------------


def test_navigate_to_non_public_target_is_refused_without_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "_post_command", _must_not_post)
    monkeypatch.setattr(mod, "_allowed_local_origins", lambda: frozenset())
    for url in ("http://localhost:5476/", "http://169.254.169.254/", "file:///etc/passwd", 5):
        out = mod.browser("browser", {"op": "navigate", "args": {"url": url}})
        assert out == mod._URL_GATE_TEXT, url
    assert "playwright" not in mod._URL_GATE_TEXT


def test_new_tab_url_runs_through_the_same_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_allowed_local_origins", lambda: frozenset())
    monkeypatch.setattr(mod, "_post_command", _must_not_post)
    out = mod.browser(
        "browser", {"op": "tabs", "args": {"action": "new", "url": "http://127.0.0.1:9/"}}
    )
    assert out == mod._URL_GATE_TEXT
    sent: list[tuple[str, str, dict, str, float]] = []
    monkeypatch.setattr(mod, "_post_command", _capture_post(sent, 200, _ok({"target_id": "T1"})))
    mod.browser("browser", {"op": "tabs", "args": {"action": "new", "url": "https://example.com"}})
    mod.browser("browser", {"op": "tabs", "args": {"action": "new"}})  # about:blank: no URL to gate
    mod.browser("browser", {"op": "tabs", "args": {"action": "list"}})
    assert [s[1] for s in sent] == ["tabs", "tabs", "tabs"]


def test_navigate_classifier_accepts_public_and_rejects_local() -> None:
    assert mod._navigate_target_is_public("https://example.com/path")
    assert mod._navigate_target_is_public("http://93.184.216.34/")
    for url in (
        "http://localhost:5476/",
        "http://127.0.0.1:5476/",
        "http://[::1]:5476/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://app.localhost/",
        "ftp://example.com/",
        "javascript:alert(1)",
        "",
    ):
        assert not mod._navigate_target_is_public(url), url


def test_navigate_classifier_rejects_alternate_ip_encodings() -> None:
    for url in (
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",  # hex
        "http://0177.0.0.1/",  # octal
        "http://127.1/",  # short form
        "http://[::ffff:127.0.0.1]/",  # IPv6-mapped
        "http://[::ffff:7f00:1]/",
        "http://0xa9.0xfe.0xa9.0xfe/",  # IMDS in hex
        "http://2852039166/",  # IMDS decimal
    ):
        assert not mod._navigate_target_is_public(url), url


@pytest.mark.parametrize(
    "url",
    [
        "http://%31%32%37.0.0.1/",
        "http://loc%61lhost/",
        "http://%5b::1%5d/",
        "http://127.0.0.1%2e/",
    ],
)
def test_navigate_classifier_rejects_percent_encoded_hosts(url: str) -> None:
    assert not mod._navigate_target_is_public(url)


@pytest.mark.parametrize(
    "url",
    ["http://１２７.0.0.1/", "http://ｌocalhost/", "http://xn--localhost/"],
)
def test_navigate_classifier_rejects_raw_non_ascii_private_hosts(url: str) -> None:
    # xn--localhost is punycode for a name that is NOT localhost -- but the
    # fullwidth forms must be refused; the ASCII punycode spelling of a real IDN
    # site stays admitted (covered below).
    if url == "http://xn--localhost/":
        assert mod._navigate_target_is_public(url)
    else:
        assert not mod._navigate_target_is_public(url)


@pytest.mark.parametrize("url", ["http://ex%61mple.com/", "http://xn--bcher-kva.example/"])
def test_navigate_classifier_keeps_percent_encoded_public_hosts(url: str) -> None:
    assert mod._navigate_target_is_public(url)


def test_navigate_classifier_rejects_parser_differential_hosts() -> None:
    for url in (
        "http://169.254.169.254\\@example.com/",
        "http://example.com\t/",
        "http://exam ple.com/",
        "http://example.com\n/",
    ):
        assert not mod._navigate_target_is_public(url), repr(url)


# --- the gateway's answers ---------------------------------------------------


def test_not_installed_tells_the_user_where_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod,
        "_post_command",
        _capture_post([], 200, {"ok": False, "code": "not_installed", "error": "x"}),
    )
    out = mod.browser("browser", {"op": "snapshot"})
    assert out == mod._NOT_INSTALLED_TEXT
    assert "Settings -> Browser" in out
    assert "playwright" not in out.lower()


class _Clock:
    """``mcp_core.time`` stand-in: monotonic advances only when the test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


_HELD = {"ok": False, "code": "user_in_control", "error": "held", "hold": True}


def _parked(monkeypatch: pytest.MonkeyPatch, clock: _Clock, answers: list[tuple[int, dict]]):
    """Park a ``click`` behind a held browser; ``answers`` feed the hand-back polls."""
    monkeypatch.setattr(mod.mcp_core, "time", clock)
    posts: list[tuple[str, dict]] = []
    ops: list[tuple[str, dict]] = []

    def fake_handback(session_key: str, header: str, timeout_s: float) -> tuple[int, dict]:
        posts.append((mod.HANDBACK_PATH, {"session_key": session_key}))
        assert timeout_s == mod.budgets.HANDBACK_CALL_TIMEOUT_S
        return answers.pop(0) if answers else (200, {"released": False})

    monkeypatch.setattr(mod, "_post_handback", fake_handback)
    keepalives: list[str] = []
    monkeypatch.setattr(
        mod.mcp_core, "_post", lambda path, body, **kw: keepalives.append(path) or {}
    )
    first = [(200, _HELD)]

    def fake_cmd(session_key: str, op: str, args: dict, header: str, timeout_s: float):
        ops.append((op, args))
        return first.pop(0) if first else (200, _ok({"x": 1, "y": 2}))

    monkeypatch.setattr(mod, "_post_command", fake_cmd)
    result = mod.browser("browser", {"op": "click", "args": {"x": 1, "y": 2}})
    assert isinstance(result, mod.DeferredTool), "a held op parks the tool, it does not fail"
    return result, posts, ops, keepalives


def test_a_held_op_parks_polls_the_hand_back_and_then_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Manus-style flow: the person takes over, the tool waits in short slices
    (no blocking call, the one worker thread stays free), and on the hand-back the
    op is posted again and runs as if nothing happened."""
    clock = _Clock()
    parked, posts, ops, keepalives = _parked(
        monkeypatch, clock, [(200, {"released": False}), (200, {"released": True})]
    )
    assert parked.due_at() <= clock.monotonic()
    assert parked.step() is None  # still held
    assert posts == [(mod.HANDBACK_PATH, {"session_key": "dashboard:chat-1-2"})]
    assert parked.due_at() == clock.monotonic() + mod.budgets.HANDBACK_POLL_S
    clock.sleep(mod.budgets.HANDBACK_POLL_S)
    text = parked.step()  # released: the op runs
    assert text is not None and text.startswith("Browser click:")
    assert ops == [("click", {"x": 1, "y": 2}), ("click", {"x": 1, "y": 2})]
    assert keepalives == [], "no keepalive needed within the first 30 s"


def test_a_long_hold_pings_the_session_keepalive_and_gives_up_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each slice past ``HOLD_KEEPALIVE_SECS`` pings the keepalive so the ACP
    tool-stall watchdog (600 s of silence) never ends the turn; at
    ``HOLD_TIMEOUT_S`` the agent is told the person still has the browser."""
    clock = _Clock()
    parked, posts, ops, keepalives = _parked(monkeypatch, clock, [])
    steps = 0
    while (text := parked.step()) is None:
        steps += 1
        clock.sleep(mod.budgets.HANDBACK_POLL_S)
        assert steps < 10_000
    assert "user_in_control" in text and "10 minutes" in text and "end your turn" in text
    assert clock.monotonic() - 1000.0 >= mod.budgets.HOLD_TIMEOUT_S
    assert len(keepalives) >= int(mod.budgets.HOLD_TIMEOUT_S // mod.HOLD_KEEPALIVE_SECS) - 1
    assert all(path == mod.KEEPALIVE_PATH for path in keepalives)
    assert ops == [("click", {"x": 1, "y": 2})], "the op is not re-posted while held"


def test_an_interrupted_op_is_not_re_run_after_the_hand_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person took over mid-op: whether it landed is unknown, so after the
    hand-back the agent is told to look, never handed a blind repeat."""
    clock = _Clock()
    monkeypatch.setattr(mod.mcp_core, "time", clock)
    monkeypatch.setattr(mod.mcp_core, "_post", lambda *a, **k: {})
    monkeypatch.setattr(mod, "_post_handback", lambda *a, **k: (200, {"released": True}))
    ops: list[str] = []

    def fake_cmd(session_key, op, args, header, timeout_s):
        ops.append(op)
        return 200, {
            "ok": False,
            "code": "interrupted",
            "error": "took over mid `type`",
            "hold": True,
        }

    monkeypatch.setattr(mod, "_post_command", fake_cmd)
    parked = mod.browser("browser", {"op": "type", "args": {"text": "hi"}})
    assert isinstance(parked, mod.DeferredTool)
    text = parked.step()
    assert text is not None and text.startswith("Error (interrupted)") and "mid `type`" in text
    assert ops == ["type"], "posted once; never repeated"


def test_user_in_control_without_hold_is_the_bounded_wait_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain ``user_in_control`` (no ``hold``) is the end of the wait: the text
    tells the agent to stop and wait for the person's word, not to retry."""
    monkeypatch.setattr(
        mod,
        "_post_command",
        _capture_post([], 200, {"ok": False, "code": "user_in_control", "error": "held"}),
    )
    out = mod.browser("browser", {"op": "click", "args": {"x": 1, "y": 1}})
    assert isinstance(out, str) and out.startswith("Error: user_in_control")
    assert "Do not retry in a loop" in out and "end your turn" in out


def test_other_refusals_carry_their_code_and_redacted_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "ok": False,
        "code": "password_field",
        "error": "e3 is a password field; AKIAIOSFODNN7EXAMPLE leaked",
    }
    monkeypatch.setattr(mod, "_post_command", _capture_post([], 200, payload))
    out = mod.browser("browser", {"op": "type", "args": {"ref": "e3", "text": "x"}})
    assert out.startswith("Error (password_field): e3 is a password field")
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_transport_failure_names_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_post_command", lambda *a, **k: (None, {}))
    out = mod.browser("browser", {"op": "snapshot"})
    assert out.startswith("Error: the gateway did not answer")


def test_hard_non_200_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod, "_post_command", _capture_post([], 403, {"code": "loopback_only", "error": "no"})
    )
    out = mod.browser("browser", {"op": "snapshot"})
    assert out == "Error: browser op failed (HTTP 403, loopback_only): no"


# --- result rendering --------------------------------------------------------


def test_screenshot_result_is_the_path_and_how_to_show_it() -> None:
    out = mod._result_text("screenshot", {"ok": True, "path": "/tmp/shots/a.png"})
    assert out.splitlines() == [
        "Browser screenshot saved: /tmp/shots/a.png",
        "Show it with ![screenshot](/tmp/shots/a.png)",
    ]


def test_snapshot_result_renders_the_outline_and_redacts() -> None:
    result = {
        "ok": True,
        "url": "https://example.com/",
        "title": "Example",
        "snapshot": '- link "Home" [ref=e1]\n- textbox "AKIAIOSFODNN7EXAMPLE" [ref=e2]',
    }
    out = mod._result_text("snapshot", result)
    assert out.startswith("Browser snapshot of https://example.com/ -- Example\n- link")
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_generic_result_drops_ok_and_nulls() -> None:
    assert mod._result_text("click", {"ok": True, "x": 1.0, "y": 2.0, "ref": None}) == (
        'Browser click: {"x": 1.0, "y": 2.0}'
    )
    assert mod._result_text("back", {"ok": True}) == "Browser back: ok"
    assert mod._result_text("back", None) == "Browser back: ok"


# --- the post itself ---------------------------------------------------------


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_post_command_sends_the_header_pair_only_when_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def _urlopen(req: Any, timeout: float) -> _Resp:
        seen.append((req, timeout))
        return _Resp(b'{"ok": true, "result": {"ok": true}}')

    monkeypatch.setattr(mod.mcp_core, "_api_base", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(mod.mcp_core, "_internal_secret", lambda: "s3cret")
    monkeypatch.setattr(mod.mcp_core, "_session_token_header", lambda: {"X-Session-Token": "tok"})
    monkeypatch.setattr(mod.mcp_core, "_api_urlopen", _urlopen)

    status, body = mod._post_command(
        "dashboard:chat-1-2", "snapshot", {}, "dashboard:chat-1-2", 7.0
    )
    assert (status, body) == (200, {"ok": True, "result": {"ok": True}})
    req, timeout = seen[-1]
    assert timeout == 7.0
    assert req.full_url == "http://127.0.0.1:1/api/browser/op"
    assert req.get_header("X-internal-secret") == "s3cret"
    assert req.get_header("X-session-key") == "dashboard:chat-1-2"
    assert req.get_header("X-session-token") == "tok"

    mod._post_command("anonymous:7", "snapshot", {}, "", 7.0)
    req, _ = seen[-1]
    assert req.get_header("X-session-key") is None
    assert req.get_header("X-session-token") is None


def test_post_command_maps_transport_errors_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def _refused(req: Any, timeout: float) -> _Resp:
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(mod.mcp_core, "_api_base", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(mod.mcp_core, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mod.mcp_core, "_api_urlopen", _refused)
    assert mod._post_command("k", "snapshot", {}, "", 1.0) == (None, {})

    def _http_error(req: Any, timeout: float) -> _Resp:
        raise urllib.error.HTTPError(
            req.full_url, 403, "forbidden", {}, io.BytesIO(b'{"code":"x"}')
        )

    monkeypatch.setattr(mod.mcp_core, "_api_urlopen", _http_error)
    assert mod._post_command("k", "snapshot", {}, "", 1.0) == (403, {"code": "x"})
