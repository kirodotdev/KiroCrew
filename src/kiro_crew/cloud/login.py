"""Sign the ``kiro-cli`` backend in on the EC2 instance, over SSM.

Backend auth can't be baked into user-data (it needs a human to approve in a
browser and, possibly, to buy a Kiro subscription). We drive it after the box is
up: run ``kiro-cli login`` on the instance over SSM, scrape the device-code URL
+ code, and open that URL in the user's *local* browser. KiroCrew stores **no**
Kiro credentials — they live in kiro-cli's own store on the instance.

Two remote-login shapes (see ``docs/reference/kiro-cli/authentication.md``):

- **Builder ID / IAM Identity Center → device code.** kiro-cli prints a
  verification URL + code; the user opens it locally and approves. No port
  forward needed. This is what :func:`start_device_login` targets.
- **Social (Google/GitHub)** needs a forwarded callback port; that path is
  automated with an SSM port-forward after kiro-cli prints the callback port.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass, field
from typing import Optional

from kiro_crew.cloud import ssm

logger = logging.getLogger(__name__)

# Device-code and OAuth authorization URLs kiro-cli prints. Keep this to https
# URLs so we do not mistake the loopback callback URL for something the user
# should open directly.
_URL_RE = re.compile(r"https://\S+")
# The short user code, printed as e.g. "code: WXYZ-1234" or "Code: ABCD1234".
_CODE_RE = re.compile(r"(?:code[:\s]+)([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})?)", re.IGNORECASE)
_LOCAL_CALLBACK_PORT_RE = re.compile(
    r"(?:localhost|127\.0\.0\.1):(\d{2,5})|(?:callback\s+)?port[:\s]+(\d{2,5})",
    re.IGNORECASE,
)
_LOGIN_LOG_PATH = "/tmp/kirocrew-kiro-login.log"
_LOGIN_PID_PATH = "/tmp/kirocrew-kiro-login.pid"
_LOGIN_FIFO_PATH = "/tmp/kirocrew-kiro-login.stdin"
_DEVICE_LOGIN_CAPTURE_ATTEMPTS = 20
_DEVICE_LOGIN_CAPTURE_SLEEP = 1
_CALLBACK_LOGIN_CAPTURE_ATTEMPTS = 20
_CALLBACK_LOGIN_CAPTURE_SLEEP = 1
_TOKEN_PRESENT_SENTINEL = "__KIRO_AUTH_TOKEN_PRESENT__"
_NOAUTH_SENTINEL = "__NOAUTH__"

# Resolve the kiro-cli binary to an absolute path, PATH-independent. Under SSM's
# `sudo -u <user> -i bash -lc` the login-shell PATH is sometimes unreliable
# (we saw exit 127 "command not found" intermittently), so every remote
# kiro-cli command sources this first and invokes "$KIRO" explicitly.
_KIRO_BIN_RESOLVE = (
    'KIRO="$(command -v kiro-cli 2>/dev/null || true)"; '
    '[ -n "$KIRO" ] || for c in "$HOME/.local/bin/kiro-cli" /usr/local/bin/kiro-cli '
    '/usr/bin/kiro-cli; do [ -x "$c" ] && KIRO="$c" && break; done; '
    '[ -n "$KIRO" ] || KIRO=kiro-cli'
)
_AUTH_FAILURE_MARKERS = (
    _NOAUTH_SENTINEL.lower(),
    "not logged in",
    "not signed in",
    "login required",
    "please log in",
    "please login",
    "sign in to",
    "must log in",
    "not authenticated",
    "unauthorized",
)


@dataclass
class LoginPrompt:
    """The device-code details scraped from ``kiro-cli login`` output."""

    url: str = ""
    code: str = ""
    raw: str = ""
    already_logged_in: bool = False
    browser_opened: bool = False
    ports: list[int] = field(default_factory=list)  # forwarded ports (social login)
    error: str = ""
    port_forward: Optional[subprocess.Popen] = field(default=None, repr=False, compare=False)

    @property
    def actionable(self) -> bool:
        return bool(self.url) or self.already_logged_in

    def close(self) -> None:
        """Tear down any temporary callback-port tunnel."""
        _close_process(self.port_forward)


def parse_login_output(text: str) -> LoginPrompt:
    """Extract the verification URL + code (or 'already logged in') from output."""
    low = text.lower()
    # Match the CONTIGUOUS phrase, not two unordered substrings — otherwise
    # "you are not logged in ... if you already have an account" false-positives
    # as signed-in and we'd drop the device-code URL. And only trust it when
    # there's no actionable verification URL in the same output.
    already = any(p in low for p in ("already logged in", "already signed in", "you're logged in"))
    has_verification_url = bool(_URL_RE.search(text))
    if already and not has_verification_url:
        return LoginPrompt(already_logged_in=True, raw=text)

    url = ""
    # kiro-cli prints BOTH the bare verification_uri (a generic sign-in page) and
    # the "complete" verification_uri_complete that embeds the code
    # (…?user_code=…) and deep-links straight to the approve screen. Prefer the
    # latter so the user isn't dropped on a general login with nowhere obvious to
    # type the code; fall back to the first URL when no complete one is printed.
    urls = [u.rstrip(".,)") for u in _URL_RE.findall(text)]
    if urls:
        url = next((u for u in urls if "user_code=" in u), urls[0])
    code = ""
    cm = _CODE_RE.search(text)
    if cm:
        code = cm.group(1)
    ports = _extract_callback_ports(text)
    return LoginPrompt(url=url, code=code, raw=text, ports=ports)


def _auth_probe(instance_id: str, profile: str = "", region: str = "") -> Optional[bool]:
    """Probe the box's auth state: True/False, or None when it can't be determined.

    An SSM timeout or transport error is NOT evidence of being signed out, so it
    maps to None. Callers that act on "signed out" (logout verification) must
    require a positive False; callers that only gate a sign-in may treat None as
    logged-out, since a redundant login attempt is harmless.
    """
    res = ssm.run_command(
        instance_id,
        _login_check_command(),
        profile,
        region,
        total_wait=60,
    )
    out = (res.stdout or "").strip()
    if _TOKEN_PRESENT_SENTINEL in out:
        return True
    if _NOAUTH_SENTINEL in out:
        return False
    # No sentinel: the remote script never reached its own echo (timeout, agent
    # or transport failure), so the state is unknown — not "signed out".
    if not res.ok:
        return None
    if not out:
        return None
    low = out.lower()
    return not any(marker in low for marker in _AUTH_FAILURE_MARKERS)


def is_logged_in(instance_id: str, profile: str = "", region: str = "") -> bool:
    """Best-effort check whether kiro-cli is already authenticated on the box.

    An undeterminable state answers False: this only gates whether to start a
    sign-in, and a redundant one is harmless (``kiro-cli login`` itself
    short-circuits when a session already exists).
    """
    return _auth_probe(instance_id, profile, region) is True


def logout(instance_id: str, profile: str = "", region: str = "") -> bool:
    """Sign ``kiro-cli`` out on the instance so another Kiro account can sign in.

    Returns True only on a POSITIVE signed-out reading. The command's own exit
    code can't be used — ``kiro-cli logout`` exits non-zero when there was no
    session to drop, which is still the state the caller asked for — so the
    outcome is re-probed. That probe must fail CLOSED: a timeout or transport
    error leaves the session possibly still active, and reporting success there
    would tell the user their account was dropped when it wasn't.
    """
    res = ssm.run_command(
        instance_id,
        _logout_command(),
        profile,
        region,
        total_wait=60,
    )
    # If the cleanup script itself never completed (SSM timeout / transport
    # error), the follow-up probe can't be trusted either: the background login
    # or ACP runtime it was meant to kill may still be live and about to
    # re-authenticate the old account. Fail closed rather than report a sign-out
    # that a racing process is undoing. (The script ends in `exit 0`
    # unconditionally, so `res.ok` only tells us the script ran end-to-end, not
    # that the kills landed — hence the status check, not `res.ok`.)
    if res.status != "Success":
        return False
    return _auth_probe(instance_id, profile, region) is False


def start_device_login(
    instance_id: str, profile: str = "", region: str = "", *, open_browser: bool = True
) -> LoginPrompt:
    """Kick off ``kiro-cli login`` on the instance and return the sign-in prompt.

    Prefer the device-code flow. If kiro-cli cannot produce a device-code URL,
    fall back to the social-provider callback flow by opening the required SSM
    port-forward automatically.
    """
    if is_logged_in(instance_id, profile, region):
        return LoginPrompt(already_logged_in=True)

    # `kiro-cli login --use-device-flow` prints the URL+code and then blocks
    # polling. Start it in the background first, then read the prompt from its
    # log. The same process keeps polling for the exact code shown to the user.
    res = ssm.run_command(
        instance_id,
        _device_login_command(replace_existing=True),
        profile,
        region,
        total_wait=90,
    )
    prompt = parse_login_output(res.stdout or res.stderr or "")
    if prompt.actionable:
        if open_browser and prompt.url:
            prompt.browser_opened = _open_browser(prompt.url)
        return prompt

    callback_prompt = _start_callback_login(instance_id, profile, region, open_browser=open_browser)
    if callback_prompt.raw and prompt.raw:
        callback_prompt.raw = f"{prompt.raw}\n{callback_prompt.raw}".strip()
    if callback_prompt.actionable or callback_prompt.ports or callback_prompt.error:
        return callback_prompt

    return prompt


def resume_login_daemon(instance_id: str, profile: str = "", region: str = "") -> None:
    """Ensure a background ``kiro-cli login`` exists.

    ``start_device_login`` already keeps the displayed device-code process
    alive. This helper is retained for manual fallback paths and starts a new
    background login only when the recorded process is no longer running.
    """
    ssm.run_command(
        instance_id,
        _resume_login_command(),
        profile,
        region,
        total_wait=30,
    )


def wait_until_logged_in(
    instance_id: str, profile: str = "", region: str = "", *, attempts: int = 30
) -> bool:
    """Poll :func:`is_logged_in` until true or attempts exhausted."""
    for _ in range(max(1, attempts)):
        if is_logged_in(instance_id, profile, region):
            return True
        ssm._sleep(5)
    return False


def social_login_hint(prompt: Optional[LoginPrompt]) -> str:
    """Human hint for the social-login (port-forward) fallback path."""
    if prompt and prompt.error:
        return prompt.error
    ports = ", ".join(str(p) for p in (prompt.ports if prompt else [])) or "the printed port"
    return (
        "For Google/GitHub sign-in, kiro-cli needs a forwarded callback port. "
        f"KiroCrew could not automate the SSM port-forward for {ports}; "
        "run `kirocrew cloud connect` and retry sign-in from the instance."
    )


def _login_check_command() -> str:
    """Build the remote auth check.

    ``kiro-cli`` is a TTY app: when run under SSM without a stdin it can skip
    writing its "Not logged in" message to the captured streams (leaving output
    empty), so parsing text is unreliable. The **exit code is authoritative**:
    ``kiro-cli whoami`` exits 0 when logged in and non-zero when not. We detach
    stdin (``< /dev/null``) so it never blocks on a TTY, capture the exit code,
    and emit an explicit sentinel — never guessing from a stale SSO token file
    (which caused false "Signed in" positives).
    """
    return f"""
set +e
{_KIRO_BIN_RESOLVE}
out="$("$KIRO" whoami < /dev/null 2>&1)"
rc=$?
echo "$out"
if [ "$rc" -eq 0 ]; then
  echo "{_TOKEN_PRESENT_SENTINEL}"
  exit 0
fi
echo "{_NOAUTH_SENTINEL}"
exit 1
""".strip()


def _logout_command() -> str:
    """Build the remote sign-out: stop the box's kiro-cli processes, drop the session, wipe its log.

    Two process shapes must die BEFORE the logout, not after:

    - a background ``kiro-cli login`` still polling would re-authenticate the
      old account right after the session is dropped;
    - a live ``kiro-cli acp`` runtime holds the OLD account's credential in
      memory and won't notice the on-disk logout until its next request 401s —
      leaving it running means chats keep being served as the account the
      operator just signed out. The gateway spawns a fresh runtime on the next
      turn, which picks up the new login.

    The log/PID/FIFO hold the previous device-code URL + code, so they are
    removed too — a stale prompt must never be shown as if it were a fresh one.
    """
    return f"""
set +e
{_KIRO_BIN_RESOLVE}
if command -v pkill >/dev/null 2>&1; then
  pkill -u "$(id -u)" -f "kiro-cli login" 2>/dev/null || true
  pkill -u "$(id -u)" -f "kiro-cli acp" 2>/dev/null || true
fi
"$KIRO" logout < /dev/null 2>&1
rm -f "{_LOGIN_LOG_PATH}" "{_LOGIN_PID_PATH}" "{_LOGIN_FIFO_PATH}"
exit 0
""".strip()


def _device_login_command(*, replace_existing: bool) -> str:
    """Build the remote command that starts login and captures its prompt."""
    replace = ""
    if replace_existing:
        replace = """
if command -v pkill >/dev/null 2>&1; then
  pkill -u "$(id -u)" -f "kiro-cli login --use-device-flow" 2>/dev/null || true
fi
""".strip()
    return f"""
set +e
{_KIRO_BIN_RESOLVE}
{replace}
rm -f "{_LOGIN_LOG_PATH}" "{_LOGIN_PID_PATH}" "{_LOGIN_FIFO_PATH}"
# Restrict the login log/pid to the owner: it captures the device-code
# verification URL + code, so a second local user must not be able to read it
# from world-readable /tmp (default umask 0022 -> 0644). umask 077 makes the
# files below 0600.
umask 077
if command -v stdbuf >/dev/null 2>&1; then
  nohup stdbuf -oL -eL "$KIRO" login --use-device-flow >"{_LOGIN_LOG_PATH}" 2>&1 </dev/null &
else
  nohup "$KIRO" login --use-device-flow >"{_LOGIN_LOG_PATH}" 2>&1 </dev/null &
fi
echo $! > "{_LOGIN_PID_PATH}"
for _ in $(seq 1 {_DEVICE_LOGIN_CAPTURE_ATTEMPTS}); do
  if [ -s "{_LOGIN_LOG_PATH}" ] && grep -Eiq "https://|verification code|user_code=|already .*logged in|already .*signed in" "{_LOGIN_LOG_PATH}"; then
    break
  fi
  if ! kill -0 "$(cat "{_LOGIN_PID_PATH}" 2>/dev/null)" 2>/dev/null; then
    break
  fi
  sleep {_DEVICE_LOGIN_CAPTURE_SLEEP}
done
cat "{_LOGIN_LOG_PATH}" 2>/dev/null || true
""".strip()


def _start_callback_login(
    instance_id: str,
    profile: str = "",
    region: str = "",
    *,
    open_browser: bool = True,
) -> LoginPrompt:
    """Start social login, automate its callback port, and return the auth URL."""
    res = ssm.run_command(
        instance_id,
        _callback_login_command(),
        profile,
        region,
        total_wait=90,
    )
    prompt = parse_login_output(res.stdout or res.stderr or "")
    if prompt.already_logged_in:
        return prompt
    if not prompt.ports:
        return prompt

    port = prompt.ports[0]
    proc: Optional[subprocess.Popen] = None
    # Refuse if the callback port is already taken: otherwise the SSM child
    # fails to bind, wait_for_local_port succeeds against the FOREIGN listener,
    # and kiro-cli's social-login callback (carrying the OAuth authorization
    # code) is routed to that stranger process. Same guard as connect.connect().
    if not ssm.port_is_free(port):
        prompt.error = (
            f"local callback port {port} is already in use — close whatever is "
            f"using it and retry the sign-in."
        )
        return prompt
    try:
        proc = ssm.open_port_forward(instance_id, port, port, profile, region)
        # Pass proc so the wait bails if the SSM child dies rather than latching
        # onto an unrelated listener that later appears on the same port.
        ready = ssm.wait_for_local_port(port, proc=proc)
        # Final ownership recheck (same rationale as connect.connect): only one
        # process can bind the port, so a listener answering while our SSM child
        # has exited is a foreign process that won the free-check->bind race —
        # refuse rather than route the OAuth authorization code to it.
        if ready and proc.poll() is not None:
            ready = False
        if not ready:
            prompt.error = f"SSM callback port-forward did not become ready on local port {port}."
            _close_process(proc)
            return prompt

        continued = ssm.run_command(
            instance_id,
            _continue_callback_login_command(),
            profile,
            region,
            total_wait=90,
        )
        combined = "\n".join(
            part for part in (prompt.raw, continued.stdout, continued.stderr) if part
        )
        out = parse_login_output(combined)
        if not out.ports:
            out.ports = prompt.ports
        if not out.url:
            # The continued social-login step produced no usable callback URL, so
            # this prompt is dead on arrival — do NOT hand back a live tunnel
            # attached to a url-less prompt (callers on the no-url path may return
            # without calling prompt.close(), orphaning the SSM child + loopback
            # port). Reap the tunnel here and leave out.port_forward unset.
            _close_process(proc)
            out.error = out.error or (
                "Kiro social sign-in did not return a callback URL — retry "
                "`kirocrew cloud login`."
            )
            return out
        out.port_forward = proc
        if open_browser and out.url:
            out.browser_opened = _open_browser(out.url)
        return out
    except Exception as exc:
        _close_process(proc)
        prompt.error = f"Could not automate Kiro callback sign-in: {exc}"
        return prompt


def _callback_login_command() -> str:
    """Build the remote social-login command with a FIFO-backed stdin."""
    return f"""
set +e
{_KIRO_BIN_RESOLVE}
if command -v pkill >/dev/null 2>&1; then
  pkill -u "$(id -u)" -f "kiro-cli login" 2>/dev/null || true
fi
rm -f "{_LOGIN_LOG_PATH}" "{_LOGIN_PID_PATH}" "{_LOGIN_FIFO_PATH}"
# Owner-only for the log + FIFO: the social-login callback details (auth code)
# flow through them, so a second local user must not read them from /tmp
# (default umask 0022 -> 0644). umask 077 makes the log 0600; chmod hardens the
# FIFO too (mkfifo honors umask, but be explicit).
umask 077
mkfifo "{_LOGIN_FIFO_PATH}"
chmod 600 "{_LOGIN_FIFO_PATH}" 2>/dev/null || true
(
  exec 3<>"{_LOGIN_FIFO_PATH}"
  if command -v stdbuf >/dev/null 2>&1; then
    stdbuf -oL -eL "$KIRO" login <&3 >"{_LOGIN_LOG_PATH}" 2>&1
  else
    "$KIRO" login <&3 >"{_LOGIN_LOG_PATH}" 2>&1
  fi
) &
echo $! > "{_LOGIN_PID_PATH}"
for _ in $(seq 1 {_CALLBACK_LOGIN_CAPTURE_ATTEMPTS}); do
  if [ -s "{_LOGIN_LOG_PATH}" ] && grep -Eiq "localhost:[0-9]{{2,5}}|127\\.0\\.0\\.1:[0-9]{{2,5}}|port[: ]+[0-9]{{2,5}}|https://|already .*logged in|already .*signed in" "{_LOGIN_LOG_PATH}"; then
    break
  fi
  if ! kill -0 "$(cat "{_LOGIN_PID_PATH}" 2>/dev/null)" 2>/dev/null; then
    break
  fi
  sleep {_CALLBACK_LOGIN_CAPTURE_SLEEP}
done
cat "{_LOGIN_LOG_PATH}" 2>/dev/null || true
""".strip()


def _continue_callback_login_command() -> str:
    """Send Enter to the waiting remote login and capture the authorization URL."""
    return f"""
set +e
if [ ! -p "{_LOGIN_FIFO_PATH}" ]; then
  cat "{_LOGIN_LOG_PATH}" 2>/dev/null || true
  exit 1
fi
exec 4<>"{_LOGIN_FIFO_PATH}"
printf '\\n' >&4
exec 4>&-
for _ in $(seq 1 {_CALLBACK_LOGIN_CAPTURE_ATTEMPTS}); do
  if [ -s "{_LOGIN_LOG_PATH}" ] && grep -Eiq "https://|already .*logged in|already .*signed in" "{_LOGIN_LOG_PATH}"; then
    break
  fi
  if ! kill -0 "$(cat "{_LOGIN_PID_PATH}" 2>/dev/null)" 2>/dev/null; then
    break
  fi
  sleep {_CALLBACK_LOGIN_CAPTURE_SLEEP}
done
cat "{_LOGIN_LOG_PATH}" 2>/dev/null || true
""".strip()


def _resume_login_command() -> str:
    """Build a daemon-only login command for fallback use."""
    return f"""
set +e
if [ -s "{_LOGIN_PID_PATH}" ] && kill -0 "$(cat "{_LOGIN_PID_PATH}")" 2>/dev/null; then
  exit 0
fi
{_device_login_command(replace_existing=False)}
""".strip()


def _extract_callback_ports(text: str) -> list[int]:
    """Extract unique valid loopback callback ports from kiro-cli output."""
    ports: list[int] = []
    for match in _LOCAL_CALLBACK_PORT_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        port = int(raw)
        if 0 < port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _browser_open_supported() -> bool:
    """Return false in headless Linux shells where webbrowser would call gio/xdg-open."""
    if os.environ.get("KIROCREW_NO_BROWSER"):
        return False
    if sys.platform.startswith("linux"):
        return bool(
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("BROWSER")
        )
    return True


def _open_browser(url: str) -> bool:
    """Best-effort local browser open; return whether it appeared to work."""
    if not _browser_open_supported():
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # pragma: no cover - headless/no browser
        # Redact any token-like query value before the persisted log ring;
        # the full URL is already printed on the terminal for the user.
        from kiro_crew.cloud.connect import redact_token

        logger.info(
            "could not auto-open browser; user must open the printed URL manually (%s)",
            redact_token(url),
        )
        return False


def _close_process(proc: Optional[subprocess.Popen]) -> None:
    """Best-effort teardown for a temporary SSM callback tunnel.

    Delegates to ``ssm.kill_port_forward`` so the whole process group is reaped
    (the callback tunnel is started via ``open_port_forward`` with
    ``start_new_session=True``) — a plain ``proc.terminate()`` would leave the
    ``session-manager-plugin`` child alive holding the OAuth callback port.
    """
    ssm.kill_port_forward(proc)
