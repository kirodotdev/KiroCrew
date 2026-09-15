"""App discovery and OS-resolved identity (``computer_use/apps_macos.py``).

Two properties are pinned here, and the first is the one that makes the whole
feature work at all:

**A process-name search resolves the WRONG pid.** Reproduced live: ``pgrep -n
"Google Chrome"`` returned 47492 — a short-lived helper process that answered
``kAXErrorCannotComplete`` to every accessibility read and then exited — while the
real browser was 637. Slack's helper 1614 likewise shadowed the real 942. So
resolution must come from the CoreGraphics window list, taking the pid that owns a
visible **layer-0** window. The decoy-pid fixture below reproduces exactly that
shape, and an AST assertion pins that no name-search call is reachable from the
module's executable code.

**The identity governance is queried on is OS-resolved, never agent-supplied.**
``resolve_identity`` answers "what is this process, according to the OS"; if the
gate were fed the model's ``app`` string instead, an allow-listed
``com.apple.finder`` could be satisfied by a model claiming "finder" while the
automation drove something else entirely.

Zero native calls: ``macos_ffi.window_list`` / ``executable_path`` are
monkeypatched, so every case runs on a Linux runner.
"""

from __future__ import annotations

import ast
import inspect
import plistlib
from pathlib import Path

import pytest

from kiro_crew.computer_use import apps_macos, macos_ffi, policy
from kiro_crew.computer_use.types import AppRef, ComputerUseError, PolicyConfig
from kiro_crew.platform_compat import IS_POSIX

# The live finding, as a fixture. Both entries claim the same owner name; only the
# layer distinguishes them, and only the layer-0 one has a populated AX tree.
_REAL_CHROME_PID = 637
_DECOY_HELPER_PID = 47492
_REAL_SLACK_PID = 942
_DECOY_SLACK_PID = 1614


def _win(
    *,
    window_id: int,
    pid: int,
    owner: str,
    title: str = "",
    layer: int = 0,
) -> macos_ffi.WindowInfo:
    """Build one window-list entry."""
    return macos_ffi.WindowInfo(
        window_id=window_id, pid=pid, owner_name=owner, title=title, layer=layer
    )


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    """The identity cache is process-wide; clear it around every test.

    Without this a pid resolved under one test's fake ``executable_path`` would be
    served from cache to the next, which is exactly the pid-reuse confusion the
    cache's own TTL exists to bound.
    """
    apps_macos.reset_identity_cache()
    yield
    apps_macos.reset_identity_cache()


@pytest.fixture
def no_identity(monkeypatch: pytest.MonkeyPatch):
    """Resolve every pid to an empty identity (isolates window-list behaviour)."""
    monkeypatch.setattr(apps_macos, "resolve_identity", lambda pid: apps_macos.AppIdentity())


def _stub_windows(monkeypatch: pytest.MonkeyPatch, windows: list) -> None:
    monkeypatch.setattr(macos_ffi, "window_list", lambda: list(windows))


# ── the decoy-pid finding ──


def test_resolve_app_prefers_layer_zero_window_owner_over_decoy_helper(
    monkeypatch: pytest.MonkeyPatch, no_identity
):
    """The layer-0 window's pid wins over a same-named non-layer-0 decoy.

    THE regression test for the ``pgrep`` finding. The decoy is listed FIRST, so a
    naive "first match" would pick it; only the layer filter rejects it.
    """
    _stub_windows(
        monkeypatch,
        [
            # Layer 1 (a helper's off-screen/overlay surface) — must be ignored.
            _win(window_id=1, pid=_DECOY_HELPER_PID, owner="Google Chrome", layer=1),
            _win(
                window_id=2,
                pid=_REAL_CHROME_PID,
                owner="Google Chrome",
                title="KiroCrew — GitHub",
                layer=0,
            ),
        ],
    )
    app = apps_macos.resolve_app("Google Chrome")
    assert app.pid == _REAL_CHROME_PID
    assert app.window_id == 2
    assert app.window_title == "KiroCrew — GitHub"


def test_list_apps_drops_every_non_zero_layer(monkeypatch: pytest.MonkeyPatch, no_identity):
    """Menu bars, the Dock, tooltips and shadows never appear as apps.

    Their owning pid is frequently a system agent whose accessibility tree is
    empty, so surfacing them would offer the model targets that cannot work.
    """
    _stub_windows(
        monkeypatch,
        [
            _win(window_id=1, pid=_REAL_SLACK_PID, owner="Slack", layer=0),
            _win(window_id=2, pid=_DECOY_SLACK_PID, owner="Slack Helper", layer=25),
            _win(window_id=3, pid=99, owner="Dock", layer=20),
            _win(window_id=4, pid=98, owner="Window Server", layer=-2147483603),
        ],
    )
    apps = apps_macos.list_apps()
    assert [a.pid for a in apps] == [_REAL_SLACK_PID]


def test_list_apps_is_one_entry_per_app_keeping_the_frontmost_window(
    monkeypatch: pytest.MonkeyPatch, no_identity
):
    """Six windows of one app collapse to one entry — the frontmost.

    Window-list order IS CoreGraphics' front-to-back z-order, so "first" means
    "frontmost", which is what a user naming an app intends.
    """
    _stub_windows(
        monkeypatch,
        [
            _win(window_id=10, pid=42, owner="Finder", title="Front"),
            _win(window_id=11, pid=42, owner="Finder", title="Behind"),
            _win(window_id=12, pid=42, owner="Finder", title="Further behind"),
        ],
    )
    apps = apps_macos.list_apps()
    assert len(apps) == 1
    assert apps[0].window_id == 10
    assert apps[0].window_title == "Front"


def test_list_apps_skips_pidless_and_nameless_entries(monkeypatch: pytest.MonkeyPatch, no_identity):
    """An entry with no usable pid or owner name is not an addressable app."""
    _stub_windows(
        monkeypatch,
        [
            _win(window_id=1, pid=0, owner="Ghost"),
            _win(window_id=2, pid=-1, owner="Negative"),
            _win(window_id=3, pid=7, owner=""),
            _win(window_id=4, pid=8, owner="Real"),
        ],
    )
    assert [a.pid for a in apps_macos.list_apps()] == [8]


def test_empty_window_list_yields_no_apps(monkeypatch: pytest.MonkeyPatch, no_identity):
    """No windows on screen is a normal state, not an error."""
    _stub_windows(monkeypatch, [])
    assert apps_macos.list_apps() == ()


# ── the ``pgrep`` prohibition, structurally ──


def _executable_code_tokens(module: object) -> set[str]:
    """Every identifier and non-docstring string literal in *module*'s source.

    Docstrings and comments are deliberately EXCLUDED. The module's own
    documentation legitimately names ``pgrep`` to explain why it is refused, and a
    check that forbade the word outright would punish documenting the decision
    — so the assertion below is about what the module *executes*, not what it
    says.
    """
    tree = ast.parse(inspect.getsource(module))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr)
        elif isinstance(node, ast.alias):
            tokens.add(node.name)
            if node.asname:
                tokens.add(node.asname)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                tokens.add(node.value)
    return tokens


def test_executable_code_never_shells_out_for_a_process_name():
    """No ``pgrep``/``ps``/``pkill``/``subprocess`` in anything the module RUNS.

    The failure this prevents is subtle: a name-based resolve returns a
    plausible-looking pid whose accessibility tree is empty, so the symptom is
    "computer use sees nothing" rather than a crash that points at the cause.
    """
    tokens = _executable_code_tokens(apps_macos)
    for forbidden in ("pgrep", "pkill", "subprocess", "popen", "NSRunningApplication", "ps"):
        assert forbidden not in tokens, f"{forbidden!r} must not be reachable from apps_macos"


def test_module_does_not_import_subprocess():
    """Structural half of the same rule: the module has no subprocess handle."""
    assert not hasattr(apps_macos, "subprocess")
    assert not hasattr(apps_macos, "shutil")


# ── match ordering ──


def test_exact_bundle_id_beats_a_substring_match(monkeypatch: pytest.MonkeyPatch):
    """A precise query is never captured by a looser candidate listed earlier."""
    _stub_windows(
        monkeypatch,
        [
            _win(window_id=1, pid=11, owner="Notes Companion"),
            _win(window_id=2, pid=12, owner="Notes"),
        ],
    )
    monkeypatch.setattr(
        apps_macos,
        "resolve_identity",
        lambda pid: apps_macos.AppIdentity(
            bundle_id="com.acme.notes.companion" if pid == 11 else "com.apple.Notes"
        ),
    )
    assert apps_macos.resolve_app("com.apple.Notes").pid == 12


def test_exact_process_name_beats_a_bundle_substring(monkeypatch: pytest.MonkeyPatch):
    """Tier 2 (exact name) outranks tier 3 (bundle substring)."""
    _stub_windows(
        monkeypatch,
        [
            _win(window_id=1, pid=11, owner="Preview Helper"),
            _win(window_id=2, pid=12, owner="Preview"),
        ],
    )
    monkeypatch.setattr(
        apps_macos,
        "resolve_identity",
        lambda pid: apps_macos.AppIdentity(
            bundle_id="com.apple.Preview.helper" if pid == 11 else "com.apple.Preview"
        ),
    )
    assert apps_macos.resolve_app("preview").pid == 12


def test_bundle_id_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch):
    """Bundle-id casing varies by macOS release, so matching must not depend on it."""
    _stub_windows(monkeypatch, [_win(window_id=1, pid=12, owner="Preview")])
    monkeypatch.setattr(
        apps_macos,
        "resolve_identity",
        lambda pid: apps_macos.AppIdentity(bundle_id="com.apple.Preview"),
    )
    assert apps_macos.resolve_app("COM.APPLE.PREVIEW").pid == 12


def test_unmatched_query_names_the_discovery_tool(monkeypatch: pytest.MonkeyPatch, no_identity):
    """The refusal must tell the model its next move."""
    _stub_windows(monkeypatch, [_win(window_id=1, pid=12, owner="Finder")])
    with pytest.raises(ComputerUseError) as excinfo:
        apps_macos.resolve_app("Photoshop")
    message = str(excinfo.value)
    assert "Photoshop" in message
    assert "computer_list_apps" in message


def test_empty_query_is_refused_without_listing(monkeypatch: pytest.MonkeyPatch):
    """An empty query cannot match anything, so it must not silently pick one."""

    def _boom():
        raise AssertionError("window_list must not be consulted for an empty query")

    monkeypatch.setattr(macos_ffi, "window_list", _boom)
    with pytest.raises(ComputerUseError):
        apps_macos.resolve_app("   ")


# ── OS-resolved identity ──


def _make_bundle(tmp_path: Path, name: str, bundle_id: str, display: str = "") -> str:
    """Build a real ``Foo.app`` on disk and return its executable path."""
    bundle = tmp_path / f"{name}.app"
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    payload: dict[str, object] = {"CFBundleIdentifier": bundle_id}
    if display:
        payload["CFBundleName"] = display
    (contents / "Info.plist").write_bytes(plistlib.dumps(payload))
    exe = contents / "MacOS" / name
    exe.write_bytes(b"\x00")
    return str(exe)


def test_identity_comes_from_the_enclosing_bundle_info_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bundle id + display name are read from ``Info.plist``, not from AX.

    ``AXBundleIdentifier`` returns ``kAXErrorAttributeUnsupported`` for EVERY app
    on this macOS (verified across eight apps), so a design built on it resolves
    nothing while looking correct.
    """
    exe = _make_bundle(tmp_path, "Chrome", "com.google.Chrome", display="Chrome")
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    identity = apps_macos.resolve_identity(_REAL_CHROME_PID)
    assert identity.bundle_id == "com.google.Chrome"
    assert identity.display_name == "Chrome"
    assert identity.executable == exe
    assert identity.resolved is True


def test_identity_walks_up_past_a_nested_helper_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A deeply nested helper still resolves to the nearest enclosing bundle."""
    exe = _make_bundle(tmp_path, "Slack", "com.tinyspeck.slackmacgap", display="Slack")
    nested = Path(exe).parent / "Frameworks" / "Helper"
    nested.mkdir(parents=True)
    helper = nested / "Slack Helper"
    helper.write_bytes(b"\x00")
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: str(helper))
    assert apps_macos.resolve_identity(_REAL_SLACK_PID).bundle_id == "com.tinyspeck.slackmacgap"


def test_unbundled_binary_resolves_to_an_unresolved_identity(monkeypatch: pytest.MonkeyPatch):
    """A plain binary has no bundle, and the identity must NOT be invented.

    ``resolved`` is False, which is what makes the gate deny: inventing an
    identity would create a governable name the operator never approved.
    """
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: "/usr/local/bin/somebinary")
    identity = apps_macos.resolve_identity(555)
    assert identity.bundle_id == ""
    assert identity.display_name == ""
    assert identity.resolved is False


def test_dead_pid_resolves_to_an_unresolved_identity(monkeypatch: pytest.MonkeyPatch):
    """``proc_pidpath`` returning nothing is normal for an exited process."""
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: "")
    assert apps_macos.resolve_identity(_DECOY_HELPER_PID).resolved is False


def test_identity_never_raises_when_the_ffi_fails(monkeypatch: pytest.MonkeyPatch):
    """An FFI failure must degrade to "unknown", not propagate.

    Identity feeds the governance gate, and the gate's correct response to an
    unnamed target is to DENY. An exception here would instead surface as an
    opaque tool error.
    """

    def _explode(pid: int) -> str:
        raise OSError("libproc unavailable")

    monkeypatch.setattr(macos_ffi, "executable_path", _explode)
    assert apps_macos.resolve_identity(1).resolved is False


def test_nonpositive_pid_short_circuits(monkeypatch: pytest.MonkeyPatch):
    """pid <= 0 is not a process; do not call into the FFI for it."""

    def _boom(pid: int) -> str:
        raise AssertionError("executable_path must not be called for a non-pid")

    monkeypatch.setattr(macos_ffi, "executable_path", _boom)
    assert apps_macos.resolve_identity(0).resolved is False
    assert apps_macos.resolve_identity(-3).resolved is False


def test_oversized_info_plist_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A hand-crafted enormous plist must not be parsed.

    The path is derived from a process the AGENT chose to target, so the read is
    size-capped; failing yields "identity unknown", which the gate denies.
    """
    exe = _make_bundle(tmp_path, "Huge", "com.acme.huge")
    plist = Path(exe).parent.parent / "Info.plist"
    plist.write_bytes(b"x" * (apps_macos.MAX_INFO_PLIST_BYTES + 1))
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    assert apps_macos.resolve_identity(31337).resolved is False


def _redirect_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point ``os.path.expanduser("~")`` at *home* on every OS.

    Both vars are set because ``expanduser`` reads ``USERPROFILE`` on Windows and
    ``HOME`` on POSIX. Setting only ``HOME`` left the Windows shard resolving the
    real user profile, so the planted bundle was not under a sensitive dir there and
    the test asserted the opposite of what it meant.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_a_plist_on_the_sensitive_path_floor_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Reviewer finding: an OS-derived path is still a path the floor governs.

    The agent does not compute this string — it is the target process's executable
    walked up to its ``.app`` ancestor — but the agent DOES choose which process to
    target, so a bundle planted under a protected directory would otherwise have its
    ``Info.plist`` opened on a path that never consulted the floor. The floor is
    about the file being opened, not about who computed the string.

    Asserted against a plist REALLY under a sensitive directory (``~/.ssh``, with
    ``$HOME`` redirected at the tmp dir) rather than by patching
    ``is_sensitive_path``: the read now goes through ``hooks.safe_read_prefix``,
    which resolves the name through its own import, so a patch of the
    ``kiro_crew.security`` attribute would not be observed and the test would pass
    against a bypassed floor.
    """
    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    exe = _make_bundle(ssh, "Sneaky", "com.acme.sneaky")
    _redirect_home(monkeypatch, home)
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    assert apps_macos.resolve_identity(5150).resolved is False


@pytest.mark.skipif(
    not IS_POSIX,
    reason="creating a symlink needs elevation on Windows; the resolved-target check "
    "it exercises is platform-independent and covered by the sensitive-dir case above",
)
def test_a_plist_that_is_a_SYMLINK_into_a_protected_dir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """GPT 5.6 BLOCKING, confirmed: the read was check-then-open.

    The previous implementation called ``is_sensitive_path`` and then opened the
    path in a separate step. The agent chooses which process to target and can
    therefore arrange the bundle, so a final-component symlink swapped between
    those two steps read a protected file on a path that never touched the hardened
    gate. ``hooks.safe_read_prefix`` closes it: canonicalize with ``realpath``,
    re-check the RESOLVED target, then open with ``O_NOFOLLOW``.

    Staged as the settled form of that race — the link already in place — because a
    real interleaving is not reproducible in a unit test. It is the resolved-target
    check that this pins; the ``O_NOFOLLOW`` on the canonical path is what covers
    the swap that lands after it.
    """
    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")

    exe = _make_bundle(tmp_path, "Linked", "com.acme.linked")
    plist = Path(exe).parent.parent / "Info.plist"
    plist.unlink()
    plist.symlink_to(ssh / "id_rsa")

    _redirect_home(monkeypatch, home)
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    # Unresolved identity, which the gate treats as a DENY — the correct posture for
    # a target we could not safely name.
    assert apps_macos.resolve_identity(5152).resolved is False


def test_an_oversized_plist_is_refused_on_the_BYTES_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The size cap moved from a ``getsize`` stat onto the bytes actually read.

    Statting the path and then opening it is the same check-then-open shape as the
    floor check was: the file measured is not necessarily the file read. Capping the
    read itself cannot be raced, and reading ``MAX_INFO_PLIST_BYTES + 1`` is what
    distinguishes "exactly at the limit" from "over it" without a second stat.
    """
    exe = _make_bundle(tmp_path, "Huge", "com.acme.huge")
    plist = Path(exe).parent.parent / "Info.plist"
    plist.write_bytes(b"x" * (apps_macos.MAX_INFO_PLIST_BYTES + 10))
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    assert apps_macos.resolve_identity(5153).resolved is False


def test_the_plist_read_goes_through_the_HARDENED_helper(tmp_path: Path):
    """Structural, because the hazard is a bare ``open`` reappearing.

    A behavioural test cannot distinguish "read safely" from "read with a
    hand-rolled check that happens to agree today" — the original bug passed every
    behavioural test in this file. This asserts the call site itself.
    """
    import inspect

    source = inspect.getsource(apps_macos._read_info_plist)
    assert "safe_read_prefix" in source
    assert "open(" not in source, "a bare open() here is the check-then-open bug"


def test_an_ordinary_bundle_plist_is_still_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The inverse guard: the floor check must not break normal identity resolution."""
    exe = _make_bundle(tmp_path, "Normal", "com.acme.normal")
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    identity = apps_macos.resolve_identity(5151)
    assert identity.resolved is True
    assert identity.bundle_id == "com.acme.normal"


def test_malformed_info_plist_degrades_to_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A corrupt plist is tolerated (the bundle may be hostile or truncated)."""
    exe = _make_bundle(tmp_path, "Broken", "com.acme.broken")
    (Path(exe).parent.parent / "Info.plist").write_bytes(b"not a plist at all")
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    assert apps_macos.resolve_identity(4242).resolved is False


def test_identity_is_cached_per_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A pid's bundle identity cannot change while the process lives."""
    exe = _make_bundle(tmp_path, "Cached", "com.acme.cached")
    calls: list[int] = []

    def _path(pid: int) -> str:
        calls.append(pid)
        return exe

    monkeypatch.setattr(macos_ffi, "executable_path", _path)
    first = apps_macos.resolve_identity(1234)
    second = apps_macos.resolve_identity(1234)
    assert first == second
    assert calls == [1234]


def test_identity_cache_expires_so_a_reused_pid_is_re_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The TTL bounds pid reuse.

    A recycled pid belonging to a DIFFERENT program must not inherit the previous
    occupant's identity — that identity is what governance is queried on, so a
    stale hit would authorize the wrong application.
    """
    first_exe = _make_bundle(tmp_path / "a", "First", "com.acme.first")
    second_exe = _make_bundle(tmp_path / "b", "Second", "com.acme.second")
    current = {"exe": first_exe}
    clock = {"now": 1000.0}
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: current["exe"])
    monkeypatch.setattr(apps_macos.time, "monotonic", lambda: clock["now"])

    assert apps_macos.resolve_identity(777).bundle_id == "com.acme.first"
    current["exe"] = second_exe
    # Still inside the TTL: the cached identity is correct to serve.
    assert apps_macos.resolve_identity(777).bundle_id == "com.acme.first"
    clock["now"] += apps_macos.IDENTITY_CACHE_TTL_SECS + 1
    assert apps_macos.resolve_identity(777).bundle_id == "com.acme.second"


def test_identity_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A long-lived process must not accumulate an entry per pid it ever saw."""
    exe = _make_bundle(tmp_path, "Bounded", "com.acme.bounded")
    monkeypatch.setattr(macos_ffi, "executable_path", lambda pid: exe)
    for pid in range(1, apps_macos.MAX_CACHED_IDENTITIES * 2):
        apps_macos.resolve_identity(pid)
    assert len(apps_macos._identity_cache) <= apps_macos.MAX_CACHED_IDENTITIES


def test_identity_for_falls_back_to_the_process_name(monkeypatch: pytest.MonkeyPatch):
    """An unbundled app stays governable by its process name.

    ``computer_use.app_names`` is a separate governance axis precisely so a target
    with no bundle id is still nameable — and therefore still deniable.
    """
    monkeypatch.setattr(apps_macos, "resolve_identity", lambda pid: apps_macos.AppIdentity())
    from kiro_crew.computer_use.types import AppRef

    identity = apps_macos.identity_for(AppRef(name="somebinary", pid=99))
    assert identity.display_name == "somebinary"
    assert identity.resolved is True


def test_identity_for_prefers_the_freshly_resolved_display_name(monkeypatch: pytest.MonkeyPatch):
    """The OS-resolved name wins over whatever a cached ``AppRef`` carried."""
    monkeypatch.setattr(
        apps_macos,
        "resolve_identity",
        lambda pid: apps_macos.AppIdentity(bundle_id="com.apple.Preview", display_name="Preview"),
    )
    from kiro_crew.computer_use.types import AppRef

    identity = apps_macos.identity_for(AppRef(name="stale name", pid=99))
    assert identity.display_name == "Preview"
    assert identity.bundle_id == "com.apple.Preview"


def test_app_ref_key_prefers_the_bundle_id(monkeypatch: pytest.MonkeyPatch, no_identity):
    """The cache/denylist key is the bundle id when one is known.

    Bundle ids are stable and unspoofable-by-renaming; process names are neither,
    which is why the key prefers the former.
    """
    _stub_windows(monkeypatch, [_win(window_id=1, pid=12, owner="Preview")])
    monkeypatch.setattr(
        apps_macos,
        "resolve_identity",
        lambda pid: apps_macos.AppIdentity(bundle_id="com.apple.Preview"),
    )
    app = apps_macos.resolve_app("Preview")
    assert app.key == "com.apple.preview"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])


class TestBrowserHostedDashboardIsRefused:
    """The self-target rule must survive the dashboard being a BROWSER TAB.

    KiroCrew's own Settings page is where the computer-use enable lives, and that
    enable sits on the keystone precisely so the agent cannot reach it. Driving our
    own window would route around it — which is why ``kirocrew_self`` is the one
    denylist entry the scope change kept.

    A bundle/name rule cannot see the browser case: the dashboard served at
    ``localhost`` into Chrome presents Chrome's bundle id and Chrome's process name.
    The window TITLE is the only surviving signal, so the rule carries
    ``title_substrings`` too (reviewer finding).
    """

    @staticmethod
    def _chrome(title: str) -> AppRef:
        return AppRef(
            name="Google Chrome", pid=99, bundle_id="com.google.Chrome", window_title=title
        )

    @pytest.mark.parametrize(
        "title",
        [
            "Kiro Crew",  # the plain tab title
            "(3) Kiro Crew",  # the unread-badge prefix (App.tsx)
            "Artifacts — Kiro Crew",  # a popout frame's suffix
            "kirocrew",  # the no-space spelling
            "KIRO CREW",  # case must not matter
        ],
    )
    def test_a_browser_window_titled_like_the_dashboard_is_refused(self, title):
        assert policy.check_app(self._chrome(title), PolicyConfig()) is not None, title

    def test_an_ordinary_browser_window_is_still_allowed(self):
        """The rule must not make browsers undrivable — that is the whole point of
        matching the title rather than the browser."""
        assert policy.check_app(self._chrome("Hacker News"), PolicyConfig()) is None

    def test_the_native_app_is_still_refused_by_IDENTITY(self):
        """The title rule ADDS to the bundle rule; it must not replace it.

        The packaged app's window title is whatever page is open ("Settings"), so a
        title-only rule would have let the native app through.
        """
        native = AppRef(
            name="Kiro Crew", pid=1, bundle_id="dev.kiro.crew", window_title="Settings"
        )
        assert policy.check_app(native, PolicyConfig()) is not None


class TestMultiWindowHostPrefersTheDeniedTitle:
    """A second Chrome window hosting the dashboard must not slip past.

    ``list_apps`` keeps ONE ``AppRef`` per pid, and input is delivered per-PID
    (``CGEventPostToPid``) — so if ANY window of a process is our dashboard, the
    whole process has to refuse. Keeping whichever window the window server listed
    first would have left the bypass open in exactly the common case: a browser with
    the dashboard in a background tab.
    """

    @staticmethod
    def _chrome_windows(titles):
        return [
            macos_ffi.WindowInfo(
                window_id=i + 1,
                pid=99,
                owner_name="Google Chrome",
                title=t,
                layer=macos_ffi.CG_WINDOW_LAYER_NORMAL,
                bounds=None,
            )
            for i, t in enumerate(titles)
        ]

    def _resolved(self, monkeypatch, titles):
        monkeypatch.setattr(macos_ffi, "window_list", lambda: self._chrome_windows(titles))
        monkeypatch.setattr(
            apps_macos,
            "resolve_identity",
            lambda pid: apps_macos.AppIdentity(
                bundle_id="com.google.Chrome", display_name="Chrome"
            ),
        )
        apps = apps_macos.list_apps()
        assert len(apps) == 1, "one AppRef per pid"
        return apps[0]

    @pytest.mark.parametrize(
        "titles",
        [
            ("Hacker News", "Kiro Crew"),  # dashboard in a BACKGROUND window
            ("Kiro Crew", "Hacker News"),  # dashboard first
            ("", "Kiro Crew"),  # untitled window listed first
            ("Hacker News", "GitHub", "Kiro Crew"),  # third of three
        ],
    )
    def test_any_dashboard_window_refuses_the_whole_process(self, titles, monkeypatch):
        app = self._resolved(monkeypatch, titles)
        assert policy.check_app(app, PolicyConfig()) is not None, titles

    def test_a_browser_with_no_dashboard_window_is_untouched(self, monkeypatch):
        app = self._resolved(monkeypatch, ("Hacker News", "GitHub"))
        assert policy.check_app(app, PolicyConfig()) is None
