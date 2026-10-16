"""Guest bootstrap for a Fargate session task: the gateway's way in.

ECS Exec cannot carry piped bytes (it is a terminal path), so a Fargate task
runs THIS as its container command instead. It listens on guest loopback, and
the gateway reaches it through an SSM port forward, which is the only path in:
reaching a task's loopback needs ``ssm:StartSession`` on that very task.

Every connection begins with the bootstrap's ``KCB1 <nonce>`` line. Then:

* ``ENROLL <secret-hex>`` -- accepted ONCE, from the gateway that just launched
  the task (no agent process exists yet). Any later enrol is refused, so a
  second principal cannot take over; the gateway stops a task whose enrolment
  it did not win.
* ``REQ <b64 json> <mac>`` with ``mac = HMAC-SHA256(secret, nonce + b64)``:
  ``{"op": "run", "command", "stdin"}`` runs ``/bin/sh -c command`` and answers
  ``RES <b64 json> <mac>`` (mac over ``nonce + "res" + b64``);
  ``{"op": "pipe", "port"}`` answers ``OK <mac>`` (over ``nonce + "ok"``) and then
  splices the connection to ``127.0.0.1:<port>`` -- the guest worker, whose own
  ``wire`` handshake authenticates the gateway again.

Lines end at LF or CR: the SSM forward can turn a write's final LF into CR. The
MACs catch any other corruption, and the gateway retries.

The bootstrap is PID 1 of the container, so it reaps the orphaned workers it
outlives. It also EXITS, which stops the task, once nothing authenticated has
used it for ``KIROCREW_BOOTSTRAP_IDLE_SECS`` (default 900) with no pipe open and
no command running: a gateway that died for good cannot stop the task itself,
and its retention sweep only runs while it is up. The live gateway sends an
authenticated no-op each sweep even when the session has no worker pipe; an open
pipe also counts as in use. Stdlib only and Python 3.9 compatible.
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time

PORT = int(os.environ.get("KIROCREW_BOOTSTRAP_PORT") or 47821)
MAX_LINE = 32 * 1024 * 1024
#: Bounds a run's captured stdout; above a browse read (256 KB) with headroom.
OUT_MAX = 4 * 1024 * 1024
RUN_TIMEOUT = 120
_state: dict = {"secret": None}
_lock = threading.Lock()
_running: set = set()  # pids a "run" op owns; the reaper leaves those alone
#: Seconds with no authenticated use, no open pipe and no running command
#: before the bootstrap exits and the task stops. 0 disables it.
IDLE_SECS = float(os.environ.get("KIROCREW_BOOTSTRAP_IDLE_SECS") or 900)
#: ``last``: monotonic time of the last authenticated use; ``pipes``: open pipes.
_activity: dict = {"last": time.monotonic(), "pipes": 0}


def _touch():
    with _lock:
        _activity["last"] = time.monotonic()


def _idle_for(now):
    """Seconds this bootstrap has been unused, or 0 while anything is in use."""
    with _lock:
        if _activity["pipes"] or _running:
            return 0.0
        return now - _activity["last"]


def _idle_watch(idle_secs, exit_fn=None, sleep=time.sleep, clock=time.monotonic):
    """Exit once unused for ``idle_secs``: the task's only way to stop without a gateway."""
    exit_fn = exit_fn or (lambda: os._exit(0))
    while True:
        sleep(min(30.0, max(1.0, idle_secs / 10)))
        if _idle_for(clock()) > idle_secs:
            print(
                "crew-bootstrap: no gateway for %ds; exiting" % idle_secs,
                file=sys.stderr,
                flush=True,
            )
            exit_fn()
            return


def _mac(nonce, *parts):
    return hmac.new(_state["secret"], nonce + b"".join(parts), hashlib.sha256).hexdigest()


def _line(conn, buf):
    start = 0
    while True:
        ends = [i for i in (buf.find(b"\n", start), buf.find(b"\r", start)) if i >= 0]
        if ends:
            i = min(ends)
            return bytes(buf[:i]), buf[i + 1 :].lstrip(b"\r\n")
        if len(buf) > MAX_LINE:
            raise ValueError("line too long")
        start = len(buf)
        chunk = conn.recv(65536)
        if not chunk:
            raise EOFError
        buf += chunk


def _splice(a, b):
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        for s in (dst,):
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join()


def _run(req):
    with _lock:  # registered before the reaper can see it exit
        proc = subprocess.Popen(
            ["/bin/sh", "-c", str(req.get("command") or "")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _running.add(proc.pid)
    try:
        out, err = proc.communicate(base64.b64decode(req.get("stdin") or ""), RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        with _lock:
            _running.discard(proc.pid)
    return {
        "code": proc.returncode,
        "stdout": base64.b64encode(out[-OUT_MAX:]).decode(),
        "stderr": err.decode("utf-8", "replace")[-400:],
    }


def _serve(conn):
    try:
        nonce = os.urandom(16).hex().encode()
        conn.sendall(b"KCB1 " + nonce + b"\n")
        line, rest = _line(conn, bytearray())
        words = line.split(b" ")
        if words[0] == b"ENROLL" and len(words) == 2:
            with _lock:
                if _state["secret"] is not None:
                    conn.sendall(b"ERR enrolled\n")
                    return
                secret = bytes.fromhex(words[1].decode())
                if len(secret) < 32:
                    conn.sendall(b"ERR short\n")
                    return
                _state["secret"] = secret
            _touch()
            conn.sendall(b"ENROLLED " + _mac(nonce, b"enrolled").encode() + b"\n")
            return
        if words[0] != b"REQ" or len(words) != 3 or _state["secret"] is None:
            conn.sendall(b"ERR refused\n")
            return
        if not hmac.compare_digest(_mac(nonce, words[1]).encode(), words[2]):
            conn.sendall(b"ERR mac\n")
            return
        _touch()
        req = json.loads(base64.b64decode(words[1]))
        if req.get("op") == "run":
            body = base64.b64encode(json.dumps(_run(req)).encode())
            conn.sendall(b"RES " + body + b" " + _mac(nonce, b"res", body).encode() + b"\n")
        elif req.get("op") == "pipe":
            port = int(req["port"])
            if not 0 < port < 65536 or port == PORT:
                conn.sendall(b"ERR port\n")
                return
            target = socket.create_connection(("127.0.0.1", port), timeout=10)
            target.settimeout(None)
            conn.sendall(b"OK " + _mac(nonce, b"ok").encode() + b"\n")
            if rest:
                target.sendall(bytes(rest))
            with _lock:
                _activity["pipes"] += 1
            try:
                _splice(conn, target)
            finally:
                with _lock:
                    _activity["pipes"] -= 1
                    _activity["last"] = time.monotonic()
                target.close()
        else:
            conn.sendall(b"ERR op\n")
    except (OSError, ValueError, EOFError, KeyError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _reap():
    # PID 1 inherits every orphaned worker; collect them, never a "run" child.
    while True:
        time.sleep(1)
        try:
            while True:
                info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if info is None:
                    break
                with _lock:  # held across the reap: a "run" child is never taken
                    if info.si_pid in _running:
                        break
                    os.waitpid(info.si_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        except OSError:
            pass


def main():
    if os.getpid() == 1 and hasattr(os, "waitid"):
        threading.Thread(target=_reap, daemon=True).start()
    if IDLE_SECS > 0:
        threading.Thread(target=_idle_watch, args=(IDLE_SECS,), daemon=True).start()
    # The worker scripts live under $HOME; an image may not set it.
    if not os.environ.get("HOME"):
        home = os.path.expanduser("~")
        os.environ["HOME"] = home if home != "~" else os.getcwd()
    workdir = os.environ.get("KIROCREW_WORKDIR") or ""
    if workdir:
        os.makedirs(workdir, exist_ok=True)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "posix":
        # The task is Linux; on Windows this option would let another socket steal the port.
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", PORT))
    port = server.getsockname()[1]
    server.listen(16)
    print("crew-bootstrap: listening on 127.0.0.1:%d" % port, file=sys.stderr, flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
