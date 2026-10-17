#!/usr/bin/env python3
"""Loopback SigV4 signing proxy for the fork GPT review lane (issue #8789).

WHY THIS EXISTS — the invariant it enforces
--------------------------------------------
A secret reachable by a process is reachable by any same-UID sibling that
process can spawn. The fork GPT reviewer (fork-gpt-review.yml) hands the model
a real shell (``codex exec``), and it runs on an UNTRUSTED fork diff, so a
prompt-injected reviewer can be told to read the parent's ``/proc/<ppid>/environ``
or any file the job user can read. Every earlier mitigation for #8671 filtered
the credential out of the shell's *environment* or denied a *path*; each lived
below this invariant and was routed around, because the credential still sat in
a process (or a file) owned by the SAME uid the model's shell runs as.

This proxy puts the credential behind a PRIVILEGE boundary instead of a filter.
It runs as a DIFFERENT uid from the one that runs ``codex`` and the shell it
drives. It holds the Bedrock credentials in a file readable only by its own
uid and listens on loopback. The reviewer's codex process is configured with
only this endpoint's URL and NO AWS credentials at all. Neither procfs nor a
0400 file owned by another uid is readable across the uid boundary by the
job user's own processes.

The residual, stated plainly — the boundary's two real dependencies:

* The GitHub-hosted runner's job user has PASSWORDLESS ``sudo``, so a shell
  running as that user could ``sudo cat`` the signer's credential file and
  cross the boundary directly. What stops that is codex's sandbox
  (``sandbox_mode = "read-only"`` with ``no_new_privs``), which denies the
  review subprocess the ability to escalate. The boundary is therefore only as
  strong as that sandbox setting: if the sandbox were weakened or removed, or
  the job user's ``sudo`` were used before the passes, the credential would be
  reachable again. This is a smaller attack surface than the earlier
  environment filters (it needs an escalation, not just a file read), but it is
  not an absolute boundary and must not be described as one.
* The model's shell can still *call* this endpoint (it is on loopback and
  egress is blocked to only the Bedrock host either way). That is a lesser harm
  than credential disclosure — it can consume model capacity, but it cannot
  publish a credential to a public comment, and this proxy forwards only to the
  single Bedrock Mantle responses host the reviewer needs.

WHAT IT DOES
------------
* Binds ``127.0.0.1:<PORT>`` (PORT from ``$BEDROCK_PROXY_PORT``, default 8713).
* For every request, re-signs it with AWS SigV4 (service ``bedrock``, region
  from ``$BEDROCK_PROXY_REGION``, default ``us-east-1``) using the credentials
  in the signer-only file at ``$BEDROCK_PROXY_CRED_FILE``, and forwards it to
  ``https://bedrock-mantle.<region>.api.aws`` (overridable via
  ``$BEDROCK_PROXY_UPSTREAM``, which the proxy's own test points at a mock
  upstream), preserving method, path and query.
* Streams the upstream response (status, headers, body) back verbatim, so the
  OpenAI ``responses`` SSE stream the codex provider consumes is untouched.

Dependency-free on purpose: AWS SDKs would add a floating dependency closure to
a job that holds credentials, so SigV4 is computed with the Python standard
library alone. Python 3 is present on the GitHub-hosted ``ubuntu-latest`` image.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import socketserver
import ssl
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_UNSIGNED = "UNSIGNED-PAYLOAD"
# Headers that are hop-by-hop or recomputed by the signer / upstream connection
# and must NOT be copied from the incoming request onto the signed request.
_STRIP_REQUEST_HEADERS = frozenset(
    h.lower()
    for h in (
        "authorization",
        "x-amz-date",
        "x-amz-security-token",
        "x-amz-content-sha256",
        "host",
        "connection",
        "proxy-connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "content-length",
    )
)
# Hop-by-hop response headers that must not be relayed to the client; the
# proxy's own connection handling owns them.
_STRIP_RESPONSE_HEADERS = frozenset(
    h.lower()
    for h in (
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
    )
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


class _Creds:
    """AWS credentials for signing, resolved fresh on every request.

    The reviewer's two GPT passes are separated by a role re-assumption (a
    Bedrock session lasts an hour and the discovery pass can consume most of
    it), so the credentials this proxy signs with CHANGE mid-run. Rather than
    restart the proxy, the signer's credentials live in a file
    (``$BEDROCK_PROXY_CRED_FILE``, written only by the trusted job user and
    readable only by the signer uid) that this process re-reads per request.
    The file is the ONLY source: the proxy never reads credentials from its own
    environment, so a mis-launch that leaked credentials into the signer
    process env could not be signed with by accident.

    Nothing here is ever logged.
    """

    __slots__ = ("access_key", "secret_key", "session_token")

    def __init__(self, access_key: str, secret_key: str, session_token: str) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token


def _read_creds(cred_file: str) -> _Creds:
    values: dict[str, str] = {}
    if cred_file and os.path.exists(cred_file):
        try:
            with open(cred_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    name, _, value = line.partition("=")
                    values[name.strip()] = value.strip()
        except OSError:
            # Return empty credentials on a read error rather than crash the
            # request; the caller surfaces an auth failure (503) when they are
            # empty, which fails closed.
            pass
    return _Creds(
        values.get("AWS_ACCESS_KEY_ID", ""),
        values.get("AWS_SECRET_ACCESS_KEY", ""),
        values.get("AWS_SESSION_TOKEN", ""),
    )


def _sign(
    creds: _Creds,
    region: str,
    service: str,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: dict[str, str],
    body: bytes,
) -> dict[str, str]:
    """Return the SigV4 headers to add to the forwarded request.

    ``headers`` is the already-filtered set of request headers that will be
    sent upstream (lower-cased names). The signed header set is exactly those
    plus ``host`` and the amz date / content-sha / security-token, so the
    signature covers precisely what is transmitted.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = _sha256_hex(body)

    signed = dict(headers)
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash
    if creds.session_token:
        signed["x-amz-security-token"] = creds.session_token

    signed_header_names = sorted(signed.keys())
    canonical_headers = "".join(f"{name}:{signed[name].strip()}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)

    # Canonical query string: AWS requires key-sorted, each component URI-encoded.
    if query:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        encoded = sorted(
            (
                urllib.parse.quote(k, safe="-_.~"),
                urllib.parse.quote(v, safe="-_.~"),
            )
            for k, v in pairs
        )
        canonical_query = "&".join(f"{k}={v}" for k, v in encoded)
    else:
        canonical_query = ""

    canonical_request = "\n".join(
        [
            method,
            path or "/",
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    key = _signing_key(creds.secret_key, date_stamp, region, service)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    out = {
        "Authorization": authorization,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
    }
    if creds.session_token:
        out["X-Amz-Security-Token"] = creds.session_token
    return out


def _make_handler(
    cred_file: str,
    region: str,
    service: str,
    upstream: urllib.parse.ParseResult,
):
    import http.client

    upstream_host = upstream.hostname or ""
    upstream_port = upstream.port or (443 if upstream.scheme == "https" else 80)
    use_tls = upstream.scheme != "http"
    # The Host header AWS signs is the bare hostname (plus a non-default port).
    if (use_tls and upstream_port != 443) or (not use_tls and upstream_port != 80):
        signed_host = f"{upstream_host}:{upstream_port}"
    else:
        signed_host = upstream_host

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # Silence the default stderr access log: a request line can echo a
        # path a prompt-injected caller chose, and the log is pure noise here.
        def log_message(self, *_args) -> None:  # noqa: D401,ANN002
            return

        def _relay(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            forward_headers: dict[str, str] = {}
            for name in self.headers.keys():
                lname = name.lower()
                if lname in _STRIP_REQUEST_HEADERS:
                    continue
                forward_headers[lname] = self.headers.get(name, "")

            split = urllib.parse.urlsplit(self.path)
            creds = _read_creds(cred_file)
            if not creds.access_key or not creds.secret_key:
                self.send_response_only(503)
                self.send_header("Content-Type", "text/plain")
                data = b"bedrock-signing-proxy: no credentials available\n"
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            sig = _sign(
                creds,
                region,
                service,
                self.command,
                signed_host,
                split.path,
                split.query,
                forward_headers,
                body,
            )

            send_headers = dict(forward_headers)
            send_headers["host"] = signed_host
            for k, v in sig.items():
                send_headers[k] = v
            send_headers["content-length"] = str(len(body))

            if use_tls:
                # Explicit verifying context: default-verify peer certificate
                # and hostname, so TLS verification does not depend on the
                # interpreter version's HTTPSConnection default. The audit rule
                # flags the HTTPSConnection API itself; the risk it names
                # (unverified TLS on old interpreters) is removed by the context
                # built just above (check_hostname + CERT_REQUIRED), and the
                # upstream host is the fixed mantle endpoint, never caller-chosen.
                ctx = ssl.create_default_context()
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(  # noqa: E501
                    upstream_host, upstream_port, timeout=600, context=ctx
                )
            else:
                conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=600)
            try:
                conn.request(self.command, self.path, body=body, headers=send_headers)
                resp = conn.getresponse()
                self.send_response_only(resp.status, resp.reason)
                relayed_cl = False
                for name, value in resp.getheaders():
                    if name.lower() in _STRIP_RESPONSE_HEADERS:
                        continue
                    if name.lower() == "content-length":
                        relayed_cl = True
                    self.send_header(name, value)
                # Stream the body in chunks when the upstream did not declare a
                # fixed length (SSE), so the responses stream is not buffered.
                stream = not relayed_cl
                if stream:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                if stream:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    remaining = int(resp.getheader("Content-Length") or 0)
                    while remaining > 0:
                        chunk = resp.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                self.wfile.flush()
            except Exception as exc:  # noqa: BLE001 — report, never leak
                self.send_response_only(502)
                self.send_header("Content-Type", "text/plain")
                msg = f"bedrock-signing-proxy upstream error: {type(exc).__name__}\n"
                data = msg.encode("utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                conn.close()

        # codex's responses provider issues only POST /openai/v1/responses, so
        # that is the only method relayed. Any other verb falls through to
        # BaseHTTPRequestHandler's 501, which is the honest answer.
        do_POST = _relay

    return Handler


class _LoopbackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # http.server resolves socket.getfqdn(host) on bind, a system-resolver
        # lookup that can stall for a long time on some macOS hosts while the
        # socket sits bound but not listening. This proxy serves loopback only
        # and is reached by its URL, never by a name, so the lookup buys nothing.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def main() -> int:
    region = os.environ.get("BEDROCK_PROXY_REGION", "us-east-1")
    # The upstream is always Bedrock Mantle, so the SigV4 service is always
    # `bedrock`; it is not configurable.
    service = "bedrock"
    port = int(os.environ.get("BEDROCK_PROXY_PORT", "8713"))
    cred_file = os.environ.get("BEDROCK_PROXY_CRED_FILE", "")
    upstream_url = os.environ.get(
        "BEDROCK_PROXY_UPSTREAM",
        f"https://bedrock-mantle.{region}.api.aws",
    )
    upstream = urllib.parse.urlparse(upstream_url)
    if not upstream.hostname:
        sys.stderr.write(f"bedrock-signing-proxy: bad upstream {upstream_url!r}\n")
        return 2

    handler = _make_handler(cred_file, region, service, upstream)
    server = _LoopbackServer(("127.0.0.1", port), handler)
    # Mark the actual bound port so a caller that passed port 0 can discover it.
    sys.stdout.write(f"bedrock-signing-proxy: listening on 127.0.0.1:{server.server_address[1]}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
