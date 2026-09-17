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
import shlex
import subprocess
import sys
import webbrowser
from dataclasses import dataclass, field
from typing import Optional

from kiro_crew.cloud import ssm
from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.login_target import (
    KiroLoginTarget,
    identity_matches_target,
    parse_whoami_output,
)

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
# Printed (and the launch skipped) when the pty driver could not be staged in a
# fresh private directory on the instance -- the caller reports it instead of
# guessing at a missing device-code prompt.
_DRIVER_SETUP_FAILED_SENTINEL = "__KIRO_LOGIN_DRIVER_SETUP_FAILED__"
_DEVICE_LOGIN_CAPTURE_ATTEMPTS = 20
_DEVICE_LOGIN_CAPTURE_SLEEP = 1

# Runs `kiro-cli login ...` under a pseudo-terminal on the instance and presses
# Enter on each prefilled prompt (Identity Center asks for the start URL and the
# region even when both are on the command line). Everything kiro-cli prints is
# forwarded to stdout -- which the launcher redirects to the login log -- EXCEPT
# the prompt lines themselves: they echo the start URL, and a URL in the log is
# what the capture loop and `parse_login_output` read as the verification URL.
# Plain Python 3 stdlib only; the instance template installs python3. The
# driver exits with the child's status once the login completes or fails, so
# the pid file (its pid) keeps meaning "the login is still running".
_LOGIN_PTY_DRIVER = r"""
import os, pty, re, select, sys, time
cmd = sys.argv[1:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
PROMPT_CARET = "\u203a".encode("utf-8")
PROMPT_MARKERS = (b"Enter Start URL", b"Enter Region")
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\r")
buf = b""
answered = 0
out = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)
while True:
    try:
        r, _, _ = select.select([fd], [], [], 1.0)
    except InterruptedError:
        continue
    if not r:
        continue
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    if PROMPT_CARET in chunk and answered < len(PROMPT_MARKERS):
        time.sleep(0.2)
        os.write(fd, b"\r")
        answered += 1
    buf += ANSI.sub(b"", chunk)
    # Emit whole lines; drop the ones that are the prompt echo.
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        if line.strip() and not any(m in line for m in PROMPT_MARKERS):
            out.write(line + b"\n")
if buf.strip() and not any(m in buf for m in PROMPT_MARKERS):
    out.write(buf + b"\n")
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8))
""".strip()
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
    #: True when ``error`` is a VERIFIED identity mismatch: the box holds a valid
    #: session for a different identity than the pinned target. Structured so
    #: callers decide on the flag, not on the error text; a mismatch is a
    #: failed sign-in that only ``cloud logout`` can clear, never a warning.
    identity_mismatch: bool = False
    port_forward: Optional[subprocess.Popen] = field(default=None, repr=False, compare=False)

    @property
    def actionable(self) -> bool:
        return bool(self.url) or self.already_logged_in

    def close(self) -> None:
        """Tear down any temporary callback-port tunnel."""
        _close_process(self.port_forward)


def parse_login_output(text: str, *, ignore_url: str = "") -> LoginPrompt:
    """Extract the verification URL + code (or 'already logged in') from output.

    *ignore_url* is the login target's own start URL: a pinned Identity Center
    login echoes it (prompt prefill), and it must never be mistaken for the
    verification URL the user has to open.
    """
    low = text.lower()
    # Match the CONTIGUOUS phrase, not two unordered substrings — otherwise
    # "you are not logged in ... if you already have an account" false-positives
    # as signed-in and we'd drop the device-code URL. And only trust it when
    # there's no actionable verification URL in the same output.
    already = any(p in low for p in ("already logged in", "already signed in", "you're logged in"))
    ignore = ignore_url.rstrip("/").lower()
    urls = [
        u
        for u in (u.rstrip(".,)") for u in _URL_RE.findall(text))
        if not ignore or u.rstrip("/").lower() != ignore
    ]
    has_verification_url = bool(urls)
    if already and not has_verification_url:
        return LoginPrompt(already_logged_in=True, raw=text)

    url = ""
    # kiro-cli prints BOTH the bare verification_uri (a generic sign-in page) and
    # the "complete" verification_uri_complete that embeds the code
    # (…?user_code=…) and deep-links straight to the approve screen. Prefer the
    # latter so the user isn't dropped on a general login with nowhere obvious to
    # type the code; fall back to the first URL when no complete one is printed.
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


def is_logged_in(
    instance_id: str,
    profile: str = "",
    region: str = "",
    *,
    target: Optional[KiroLoginTarget] = None,
) -> bool:
    """Best-effort check whether kiro-cli is already authenticated on the box.

    An undeterminable state answers False: this only gates whether to start a
    sign-in, and a redundant one is harmless (``kiro-cli login`` itself
    short-circuits when a session already exists).

    With *target*, "authenticated" means authenticated AS THAT IDENTITY: a
    valid session for the wrong account family (Builder ID where the launch
    asked for the organization's Identity Center, or vice versa) answers
    False, so the caller starts the correct sign-in instead of adopting a
    session that happens to exist. That is the difference between "some Kiro
    session" and "my Kiro" — see :mod:`kiro_crew.cloud.login_target`. EVERY
    supplied target is compared, the default Builder ID one included: a
    reused instance carrying an Identity Center session must not be adopted by
    a launch that asked for Builder ID (wrong license, wrong models). Only a
    caller that passes no target at all asks the legacy question "is there
    some session".
    """
    if _auth_probe(instance_id, profile, region) is not True:
        return False
    if target is None:
        return True
    return remote_identity_state(instance_id, profile, region, target=target) == "match"


def remote_identity(
    instance_id: str, profile: str = "", region: str = ""
) -> Optional[dict[str, str]]:
    """Read the remote ``kiro-cli whoami --format json`` identity.

    ``None`` when it cannot be read at all (SSM transport failure, no output);
    ``{}`` when kiro-cli ran but reported no identity (signed out); otherwise
    any of ``email`` / ``account_type`` / ``start_url``. Keeping "unreadable"
    distinct from "signed out" matters: a transient SSM fault must never be
    reported as an identity mismatch.
    """
    try:
        res = ssm.run_command(instance_id, _whoami_json_command(), profile, region, total_wait=60)
    except AWSError as exc:
        # send-command itself failed (throttled, denied, unreachable): the
        # identity was not read. Callers classify this as "unknown", never as a
        # verdict on the session.
        logger.debug("remote whoami could not be sent: %s", exc)
        return None
    out = (res.stdout or "").strip()
    if not res.ok and not out:
        return None
    if _NOAUTH_SENTINEL in out:
        return {}
    return parse_whoami_output(out)


def remote_identity_state(
    instance_id: str, profile: str = "", region: str = "", *, target: KiroLoginTarget
) -> str:
    """Classify the remote session against *target*.

    One of ``"match"``, ``"mismatch"``, ``"absent"`` or ``"unknown"``. Only
    ``"match"`` may short-circuit a sign-in; ``"mismatch"`` is the case a bare
    boolean check cannot express — a real, valid session for the wrong account.
    """
    ident = remote_identity(instance_id, profile, region)
    if ident is None:
        return "unknown"
    if not ident:
        return "absent"
    return "match" if identity_matches_target(ident, target) else "mismatch"


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


def _verified_already_logged_in(
    instance_id: str,
    profile: str,
    region: str,
    resolved: KiroLoginTarget,
    prompt: LoginPrompt,
) -> LoginPrompt:
    """Turn kiro-cli's "already logged in" into a verdict about *resolved*.

    A match keeps the prompt as the success it is. Anything else — a session for
    a different identity, or an identity that cannot be read — is NOT a
    success: ``already_logged_in`` is cleared and ``error`` names the mismatch
    and the recovery (``kirocrew cloud logout`` then sign in again), so the
    prompt is not actionable and every caller refuses it the same way.
    """
    state = remote_identity_state(instance_id, profile, region, target=resolved)
    if state == "match":
        return prompt
    prompt.already_logged_in = False
    if state == "mismatch":
        prompt.identity_mismatch = True
        prompt.error = (
            f"the instance is signed in to a different Kiro identity than {resolved.describe()}; "
            "run `kirocrew cloud logout` on this instance, then sign in again"
        )
    else:
        prompt.error = (
            f"the instance reports an existing Kiro session but its identity could not be "
            f"read ({state}); refusing to treat it as {resolved.describe()}. Retry, or run "
            "`kirocrew cloud logout` and sign in again"
        )
    return prompt


def start_device_login(
    instance_id: str,
    profile: str = "",
    region: str = "",
    *,
    open_browser: bool = True,
    target: Optional[KiroLoginTarget] = None,
) -> LoginPrompt:
    """Kick off ``kiro-cli login`` on the instance and return the sign-in prompt.

    Prefer the device-code flow. If kiro-cli cannot produce a device-code URL
    for a DEFAULT (Builder ID) target, fall back to the social-provider callback
    flow by opening the required SSM port-forward automatically. An
    identity-PINNED target (Identity Center) never falls back: the callback
    flow is a different identity family, so degrading into it would silently
    sign the crew in as the wrong account — the failure is surfaced instead.

    *target* is the durable identity a managed launch carries
    (:class:`KiroLoginTarget`); ``None`` means the default Builder ID target.
    """
    resolved = target or KiroLoginTarget()
    if is_logged_in(instance_id, profile, region, target=resolved):
        return LoginPrompt(already_logged_in=True)

    # `kiro-cli login --use-device-flow` prints the URL+code and then blocks
    # polling. Start it in the background first, then read the prompt from its
    # log. The same process keeps polling for the exact code shown to the user.
    res = ssm.run_command(
        instance_id,
        _device_login_command(replace_existing=True, **resolved.login_kwargs()),
        profile,
        region,
        total_wait=90,
    )
    prompt = parse_login_output(res.stdout or res.stderr or "", ignore_url=resolved.start_url)
    if prompt.already_logged_in:
        # kiro-cli ignores a login over a LIVE session and prints "already
        # logged in" whatever that session's identity is. The caller asked for
        # `resolved`; only a session that MATCHES it is a success. Anything
        # else is the mismatch every caller (CLI, wizard, launch engine) must
        # refuse rather than record as signed in — decided HERE, once, so no
        # caller can forget the check.
        return _verified_already_logged_in(instance_id, profile, region, resolved, prompt)
    if prompt.actionable:
        if open_browser and prompt.url:
            prompt.browser_opened = _open_browser(prompt.url)
        return prompt

    if resolved.is_identity_center:
        # Pinned: stay on this identity. Report, do not degrade.
        if _DRIVER_SETUP_FAILED_SENTINEL in (res.stdout or ""):
            prompt.error = (
                "could not stage the sign-in driver in a private temporary directory on "
                "the instance (mktemp/write failed); nothing was started. Retry, or check "
                "the instance's TMPDIR."
            )
        elif not prompt.error:
            prompt.error = (
                f"kiro-cli did not produce a device-code prompt for {resolved.describe()}; "
                "retry, or check the start URL and Identity Center region."
            )
        return prompt

    callback_prompt = _start_callback_login(instance_id, profile, region, open_browser=open_browser)
    if callback_prompt.raw and prompt.raw:
        callback_prompt.raw = f"{prompt.raw}\n{callback_prompt.raw}".strip()
    if callback_prompt.actionable or callback_prompt.ports or callback_prompt.error:
        return callback_prompt

    return prompt


def resume_login_daemon(
    instance_id: str,
    profile: str = "",
    region: str = "",
    *,
    target: Optional[KiroLoginTarget] = None,
) -> None:
    """Ensure a background ``kiro-cli login`` exists — for the SAME identity.

    ``start_device_login`` already keeps the displayed device-code process
    alive. This helper is retained for manual fallback paths and starts a new
    background login only when the recorded process is not running. It
    must receive the same *target* the start did: a resume that drops the
    identity restarts the flow as Builder ID, which is exactly the omission
    this type exists to make visible.
    """
    resolved = target or KiroLoginTarget()
    ssm.run_command(
        instance_id,
        _resume_login_command(**resolved.login_kwargs()),
        profile,
        region,
        total_wait=30,
    )


def wait_until_logged_in(
    instance_id: str,
    profile: str = "",
    region: str = "",
    *,
    attempts: int = 30,
    target: Optional[KiroLoginTarget] = None,
) -> bool:
    """Poll :func:`is_logged_in` (against *target* when given) until true or attempts exhausted."""
    for _ in range(max(1, attempts)):
        if is_logged_in(instance_id, profile, region, target=target):
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


def _whoami_json_command() -> str:
    """Build the remote identity read: ``kiro-cli whoami --format json``.

    Same discipline as :func:`_login_check_command` — stdin detached, exit code
    authoritative, explicit no-auth sentinel — but the OUTPUT is what matters
    here: the leading JSON object names the account type and start URL the
    session belongs to, which is what turns "logged in" into "logged in as whom".
    """
    return f"""
set +e
{_KIRO_BIN_RESOLVE}
out="$("$KIRO" whoami --format json < /dev/null 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "{_NOAUTH_SENTINEL}"
  exit 1
fi
echo "$out"
exit 0
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


def _device_login_command(
    *,
    replace_existing: bool,
    identity_provider: str = "",
    license_: str = "",
    idp_region: str = "",
) -> str:
    """Build the remote command that starts login and captures its prompt."""
    replace = ""
    if replace_existing:
        replace = """
if command -v pkill >/dev/null 2>&1; then
  pkill -u "$(id -u)" -f "kiro-cli login --use-device-flow" 2>/dev/null || true
fi
""".strip()
    # Build optional kiro-cli flags for enterprise / IAM Identity Center login.
    # Each value is shell-quoted: it flows from a CLI argument into a bash
    # script executed on the remote instance via SSM, so an unquoted value
    # carrying `$(...)`, spaces, or quotes would otherwise be interpreted by
    # the remote shell (remote command injection).
    extra_flags = ""
    if identity_provider:
        extra_flags += f" --identity-provider {shlex.quote(identity_provider)}"
    if license_:
        extra_flags += f" --license {shlex.quote(license_)}"
    if idp_region:
        extra_flags += f" --region {shlex.quote(idp_region)}"
    if identity_provider or license_ or idp_region:
        # Identity Center: kiro-cli PROMPTS for the start URL and region even
        # when the flags supply them (the flags only prefill the answers), and
        # it reads those prompts from a TTY. With stdin on /dev/null the answers
        # come back empty and the flow dies before any device code is printed
        # ("invalid value for field: region - must be a valid host label"). The
        # driver gives it a pty and presses Enter on each prefilled prompt.
        # The driver file must not be a predictable path a second local user
        # could pre-create or symlink: `mktemp -d` makes a fresh 0700 directory
        # with an unpredictable name, the write goes inside it, and any failure
        # aborts the launch (a sentinel the caller reports) instead of running
        # whatever sits at the path. The directory is removed by the driver's
        # parent shell once the login process has exited.
        launch = (
            'nohup sh -c \'python3 "$0" "$@"; rm -rf "$(dirname "$0")"\' "$KC_DRIVER" '
            f'"$KIRO" login --use-device-flow{extra_flags} >"{_LOGIN_LOG_PATH}" 2>&1 </dev/null &'
        )
        launch_block = f"""
KC_DRIVER_DIR="$(mktemp -d "${{TMPDIR:-/tmp}}/kirocrew-login-pty.XXXXXXXX")" || KC_DRIVER_DIR=""
if [ -z "$KC_DRIVER_DIR" ] || [ ! -d "$KC_DRIVER_DIR" ]; then
  echo "{_DRIVER_SETUP_FAILED_SENTINEL}"
  exit 0
fi
KC_DRIVER="$KC_DRIVER_DIR/driver.py"
if ! cat > "$KC_DRIVER" <<'PYDRIVER'
{_LOGIN_PTY_DRIVER}
PYDRIVER
then
  rm -rf "$KC_DRIVER_DIR"
  echo "{_DRIVER_SETUP_FAILED_SENTINEL}"
  exit 0
fi
{launch}
""".strip()
    else:
        # Builder ID prompts for nothing, so the plain background process is
        # enough and stays exactly as it is.
        launch_block = f"""
if command -v stdbuf >/dev/null 2>&1; then
  nohup stdbuf -oL -eL "$KIRO" login --use-device-flow{extra_flags} >"{_LOGIN_LOG_PATH}" 2>&1 </dev/null &
else
  nohup "$KIRO" login --use-device-flow{extra_flags} >"{_LOGIN_LOG_PATH}" 2>&1 </dev/null &
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
{launch_block}
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


def _resume_login_command(
    *,
    identity_provider: str = "",
    license_: str = "",
    idp_region: str = "",
) -> str:
    """Build a daemon-only login command for fallback use."""
    return f"""
set +e
if [ -s "{_LOGIN_PID_PATH}" ] && kill -0 "$(cat "{_LOGIN_PID_PATH}")" 2>/dev/null; then
  exit 0
fi
{_device_login_command(replace_existing=False, identity_provider=identity_provider, license_=license_, idp_region=idp_region)}
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
