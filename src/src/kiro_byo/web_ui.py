"""Small loopback-only management UI for Kiro BYO.

The UI intentionally exposes only non-secret provider metadata.  Credentials are
write-only: browser requests may create/replace them, but no API returns their
plaintext value.  A per-process random token protects all API requests from
cross-origin localhost attacks in addition to normal browser same-origin rules.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .i18n import locale_messages, normalize_ui_language, error_code
from .account_status import account_statuses
from .capabilities import capability_report
from .config import ConfigError, config_path
from .credential_store import CredentialStore, CredentialStoreError, default_credential_store
from .discovery import DiscoveryError
from .native_tools import advertised_native_tool_names
from .tool_center import tool_center_report
from .paths import app_home, crew_data_home
from .telemetry import TurnTelemetryStore
from .runtime_trace import RuntimeTraceStore
from .provider_admin import (
    configure_provider,
    delete_provider,
    list_provider_platforms,
    list_provider_presets,
    list_providers,
    refresh_provider_models,
    reasoning_language_settings,
    response_language_settings, set_response_language_preference,
    ui_language_settings, set_ui_language_preference,
    set_model_thinking_preference,
    set_reasoning_language_preference,
    update_provider_credential,
)


class _ManagementServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: CredentialStore,
        token: str,
        telemetry: TurnTelemetryStore | None = None,
        runtime_trace: RuntimeTraceStore | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.credential_store = store
        self.ui_token = token
        self.telemetry = telemetry or TurnTelemetryStore()
        self.runtime_trace = runtime_trace or RuntimeTraceStore()


def _integration_state() -> dict[str, Any]:
    path = app_home() / "data" / "integration.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: raw.get(key)
        for key in ("mode", "baseline", "overlay_version", "kiro_crew_home")
        if raw.get(key) is not None
    }


class _Handler(BaseHTTPRequestHandler):
    server: _ManagementServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - stdlib signature
        # Keep API keys/body content out of access logs.  Method/path/status are
        # enough for local diagnostics.
        return

    def _json(self, status: int, payload: Any) -> None:
        if status >= 400 and isinstance(payload, dict) and isinstance(payload.get("error"), str):
            code = error_code(payload["error"])
            if code:
                payload = {**payload, "code": code}  # original technical message stays unchanged
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _api_authorized(self) -> bool:
        supplied = self.headers.get("X-Kiro-BYO-Token", "")
        return secrets.compare_digest(supplied, self.server.ui_token)

    def _require_api_auth(self) -> bool:
        if self._api_authorized():
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "invalid management token"})
        return False

    def _body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 1024 * 1024:
            raise ValueError("request body is empty or too large")
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _provider_id_from_path(self, suffix: str = "") -> str | None:
        path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/providers/"
        if not path.startswith(prefix):
            return None
        tail = path[len(prefix):]
        if suffix:
            marker = "/" + suffix
            if not tail.endswith(marker):
                return None
            tail = tail[: -len(marker)]
        if not tail or "/" in tail:
            return None
        return urllib.parse.unquote(tail)


    def _provider_model_from_path(self, suffix: str) -> tuple[str, str] | None:
        path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/providers/"
        if not path.startswith(prefix):
            return None
        tail = path[len(prefix):]
        parts = tail.split("/")
        if len(parts) != 4 or parts[1] != "models" or parts[3] != suffix:
            return None
        if not parts[0] or not parts[2]:
            return None
        return urllib.parse.unquote(parts[0]), urllib.parse.unquote(parts[2])

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            query = urllib.parse.parse_qs(parsed.query)
            supplied = (query.get("token") or [""])[0]
            if not secrets.compare_digest(supplied, self.server.ui_token):
                self._html(HTTPStatus.FORBIDDEN, "<h1>403</h1><p>Invalid management token.</p>")
                return
            self._html(HTTPStatus.OK, _page(self.server.ui_token))
            return
        if parsed.path == "/api/status":
            if not self._require_api_auth():
                return
            crew_home = crew_data_home()
            self._json(
                HTTPStatus.OK,
                {
                    "version": __version__,
                    "kiro_byo_home": str(app_home()),
                    "config_path": str(config_path()),
                    "kiro_crew_data_home": str(crew_home) if crew_home else None,
                    "credential_backend": self.server.credential_store.backend_name,
                    "product": "Kiro BYO",
                    "target_client": "KiroCrew",
                    "runtime_capabilities": capability_report(),
                    "backend_card": {
                        "source": "KiroCrew",
                        "note": "KiroCrew Settings → Agent Harness is authoritative for backend parity.",
                    },
                    "native_tools": advertised_native_tool_names(),
                    "jev_owner": "KiroCrew",
                    "integration": _integration_state(),
                    "diagnostics": self.server.telemetry.summary(),
                    "reasoning_language": reasoning_language_settings(),
                    "response_language": response_language_settings(),
                    "ui_language": ui_language_settings(),
                    "providers": list_providers(store=self.server.credential_store),
                },
            )
            return
        if parsed.path.startswith("/api/locales/"):
            if not self._require_api_auth():
                return
            try:
                name = parsed.path.removeprefix("/api/locales/")
                if name == "auto" or normalize_ui_language(name) != name:
                    raise ValueError("invalid locale")
                self._json(HTTPStatus.OK, {"locale": name, "messages": locale_messages(name)})
            except (ValueError, OSError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "unsupported UI locale"})
            return
        if parsed.path == "/api/presets":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, {"presets": list_provider_presets(), "platforms": list_provider_platforms()})
            return
        if parsed.path == "/api/accounts":
            if not self._require_api_auth():
                return
            providers = list_providers(store=self.server.credential_store)
            accounts = account_statuses(providers, self.server.credential_store)
            self._json(
                HTTPStatus.OK,
                {
                    "accounts": accounts,
                    "providers": [
                        {
                            "id": str(row.get("id") or ""),
                            "name": str(row.get("name") or row.get("id") or ""),
                            "base_url": str(row.get("base_url") or ""),
                        }
                        for row in providers
                    ],
                    "summary": {
                        "configured_providers": len(providers),
                        "capable_providers": len(accounts),
                        "ok": sum(1 for row in accounts if row.get("state") == "ok"),
                        "errors": sum(1 for row in accounts if row.get("state") == "error"),
                    },
                },
            )
            return
        if parsed.path == "/api/diagnostics":
            if not self._require_api_auth():
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            self._json(
                HTTPStatus.OK,
                {
                    "summary": self.server.telemetry.summary(),
                    "turns": self.server.telemetry.recent(limit),
                    "content_recording": False,
                    "client": "kiro-crew",
                },
            )
            return
        if parsed.path == "/api/tool-center":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, tool_center_report(
                advertised_native_tool_names(),
                self.server.runtime_trace.recent(500),
            ))
            return
        if parsed.path == "/api/runtime-trace":
            if not self._require_api_auth():
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError:
                limit = 100
            session_id = str((query.get("session_id") or [""])[0]).strip()
            event = str((query.get("event") or [""])[0]).strip()
            if len(event) > 128:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "event filter is too long"})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "status": self.server.runtime_trace.status(),
                    "events": self.server.runtime_trace.recent(limit, session_id=session_id, event=event),
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback
        if not self._require_api_auth():
            return
        try:
            body = self._body_json()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/providers":
                result = configure_provider(
                    base_url=str(body.get("base_url") or ""),
                    api_key=str(body.get("api_key") or ""),
                    provider_name=str(body.get("name") or ""),
                    preset_id=str(body.get("preset_id") or "custom"),
                    create_new=bool(body.get("create_new", False)),
                    store=self.server.credential_store,
                )
                self._json(HTTPStatus.OK, result)
                return
            provider_id = self._provider_id_from_path("refresh")
            if provider_id is not None:
                result = refresh_provider_models(provider_id, store=self.server.credential_store)
                self._json(HTTPStatus.OK, result)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, ConfigError, CredentialStoreError, DiscoveryError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802 - stdlib callback
        if not self._require_api_auth():
            return
        try:
            body = self._body_json()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/preferences/ui-language":
                self._json(HTTPStatus.OK, set_ui_language_preference(str(body.get("value") or "auto")))
                return
            if path == "/api/preferences/response-language":
                self._json(HTTPStatus.OK, set_response_language_preference(str(body.get("value") or "auto")))
                return
            if path == "/api/preferences/reasoning-language":
                result = set_reasoning_language_preference(str(body.get("value") or body.get("language") or "auto"))
                self._json(HTTPStatus.OK, result)
                return
            model_target = self._provider_model_from_path("thinking")
            if model_target is not None:
                provider_id, model_id = model_target
                result = set_model_thinking_preference(
                    provider_id,
                    model_id,
                    str(body.get("mode") or body.get("preference") or ""),
                    store=self.server.credential_store,
                )
                self._json(HTTPStatus.OK, result)
                return
            provider_id = self._provider_id_from_path("credential")
            if provider_id is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            api_key = body.get("api_key")
            if not isinstance(api_key, str) or not api_key:
                raise ValueError("api_key is required")
            result = update_provider_credential(provider_id, api_key, store=self.server.credential_store)
            self._json(HTTPStatus.OK, result)
        except (ValueError, ConfigError, CredentialStoreError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback
        if not self._require_api_auth():
            return
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/diagnostics":
                removed = self.server.telemetry.clear()
                # Runtime Trace is independent from request telemetry.
                self._json(HTTPStatus.OK, {"cleared": True, "removed": removed, "removed_trace": 0})
                return
            if path == "/api/runtime-trace":
                self._json(HTTPStatus.OK, {"cleared": True, "removed": self.server.runtime_trace.clear()})
                return
            provider_id = self._provider_id_from_path()
            if provider_id is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            delete_provider(provider_id, store=self.server.credential_store)
            self._json(HTTPStatus.OK, {"deleted": provider_id})
        except (ConfigError, CredentialStoreError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def _page(token: str) -> str:
    html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kiro BYO · KiroCrew Backend</title>
<style>
:root{
  --n-0:#fff;--n-1:#fafafa;--n-2:#f4f4f5;--n-3:#e9e9ec;--n-4:#d6d6da;
  --n-5:#a5a5ad;--n-6:#74747d;--n-7:#55555e;--n-8:#3d3d45;--n-9:#1a1a1e;
  --bg-page:var(--n-1);--bg-surface:var(--n-0);--bg-subtle:var(--n-2);
  --border:var(--n-3);--border-strong:var(--n-4);
  --text:var(--n-9);--text-body:var(--n-8);--text-secondary:var(--n-6);--text-muted:var(--n-5);
  --accent:var(--n-9);--accent-hover:var(--n-8);--accent-on:var(--n-0);
  --slab-bg:#eeeef1;--slab-fg:var(--n-8);--slab-border:var(--n-4);
  --pass-fg:#15703c;--pass-bg:#eef9f2;--pass-border:#bfe3cd;
  --exception-fg:#96580a;--exception-bg:#fdf6e9;--exception-border:#e8d3a6;
  --fail-fg:#a92a22;--fail-bg:#fdf1f0;--fail-border:#ecc4c0;
  --font-sans:"Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;
  --font-mono:"Cascadia Mono",Consolas,"Sarasa Mono SC",monospace;
  --fs-micro:11px;--fs-tiny:12px;--fs-small:13px;--fs-body:14px;
  --fs-lead:16px;--fs-h3:18px;--fs-h2:21px;--fs-h1:28px;--fs-metric:30px;
  --sp-05:2px;--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:20px;--sp-6:24px;--sp-8:32px;
  --r-sm:6px;--r-md:10px;--r-lg:14px;--r-pill:999px;--rail:2px;
  --shadow-sm:0 1px 2px rgba(20,20,24,.05);--shadow-md:0 2px 8px rgba(20,20,24,.07);
  --dur-press:140ms;--dur-switch:180ms;--dur-page:200ms;
  --ease-out:cubic-bezier(.23,1,.32,1);
  font-family:var(--font-sans);font-size:var(--fs-body);color:var(--text-body);color-scheme:light
}
:root[data-theme='dark']{
  color-scheme:dark;
  --n-0:#1b1b1f;--n-1:#121215;--n-2:#242429;--n-3:#303036;--n-4:#3e3e46;
  --n-5:#6e6e78;--n-6:#9a9aa4;--n-7:#bdbdc5;--n-8:#dededf;--n-9:#f7f7f8;
  --bg-page:var(--n-1);--bg-surface:var(--n-0);--bg-subtle:var(--n-2);
  --border:var(--n-3);--border-strong:var(--n-4);
  --accent:var(--n-9);--accent-hover:var(--n-8);--accent-on:var(--n-1);
  --slab-bg:#26262c;--slab-fg:var(--n-8);--slab-border:#3a3a42;
  --pass-fg:#5fd08b;--pass-bg:#12251a;--pass-border:#245c39;
  --exception-fg:#e5b155;--exception-bg:#271e0d;--exception-border:#5f4718;
  --fail-fg:#f08a83;--fail-bg:#2a1615;--fail-border:#6b2a26;
  --shadow-sm:0 1px 2px rgba(0,0,0,.4);--shadow-md:0 2px 10px rgba(0,0,0,.5)
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;height:100%;overflow:hidden;background:var(--bg-page)}
button,input,select,textarea{font:inherit;color:inherit}
code,.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.app{height:100vh;display:grid;grid-template-columns:220px minmax(0,1fr)}
main{height:100vh;overflow:hidden;padding:var(--sp-6) var(--sp-8);display:flex;flex-direction:column}
.page-head{flex:0 0 auto;padding-bottom:var(--sp-3);border-bottom:1px solid var(--border);margin-bottom:var(--sp-4)}
.page-head h1{font-size:var(--fs-h2);font-weight:700;color:var(--text);letter-spacing:-.015em;line-height:1.1}
.page-head p{color:var(--text-secondary);font-size:var(--fs-small);margin-top:var(--sp-05)}
.page{display:none;flex:1;flex-direction:column;min-height:0}
.page.active{display:flex}
.scroll{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:var(--sp-4);min-height:0;scrollbar-width:thin;scrollbar-color:var(--border-strong) transparent}
.scroll::-webkit-scrollbar{width:6px}.scroll::-webkit-scrollbar-thumb{border-radius:var(--r-pill);background:var(--border-strong)}
aside{padding:var(--sp-6) var(--sp-3);border-right:1px solid var(--border);background:var(--bg-surface);display:flex;flex-direction:column;overflow:hidden}
.brand{display:flex;gap:var(--sp-3);align-items:center;padding:0 var(--sp-2) var(--sp-6)}
.brand b{display:grid;place-items:center;width:40px;height:40px;border-radius:var(--r-md);background:var(--accent);color:var(--accent-on);font-size:var(--fs-h3);font-weight:700}
.brand strong{display:block;color:var(--text);font-size:var(--fs-lead)}
.brand small{display:block;margin-top:var(--sp-05);color:var(--text-muted);font-size:var(--fs-tiny)}
nav{display:grid;gap:var(--sp-05);flex:1;align-content:start}
nav button{display:flex;align-items:center;border:0;border-radius:var(--r-md);padding:var(--sp-3);background:transparent;color:var(--text-secondary);font-size:var(--fs-body);font-weight:600;text-align:left;cursor:pointer;transition:background var(--dur-press) var(--ease-out),color var(--dur-press) var(--ease-out)}
nav button i{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;font-style:normal;font-size:13px;flex-shrink:0;margin-right:var(--sp-2)}
nav button:active{transform:scale(.97)}
@media(hover:hover)and (pointer:fine){nav button:hover{background:var(--bg-subtle);color:var(--text-body)}}
nav button.active{position:relative;background:var(--bg-subtle);color:var(--text);font-weight:700}
nav button.active::before{content:"";position:absolute;left:0;top:var(--sp-2);bottom:var(--sp-2);width:var(--rail);border-radius:var(--r-pill);background:var(--accent);animation:slideIn var(--dur-page) var(--ease-out)}
@keyframes slideIn{from{opacity:0;transform:scaleY(0)}to{opacity:1;transform:scaleY(1)}}
.aside-foot{margin-top:auto;padding:var(--sp-4) var(--sp-3);border-top:1px solid var(--border);font-size:var(--fs-tiny);color:var(--text-muted);display:flex;flex-direction:column;gap:var(--sp-2)}
.aside-foot .dot{display:inline-block;width:6px;height:6px;margin-right:var(--sp-2);border-radius:50%;background:var(--text-muted)}
.card{background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:var(--sp-5);box-shadow:var(--shadow-sm)}
.card h2{font-size:var(--fs-lead);font-weight:700;color:var(--text);margin-bottom:var(--sp-3)}
.card h3{font-size:var(--fs-body);font-weight:600;color:var(--text-body);margin-bottom:var(--sp-2)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-4)}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.kv{display:flex;flex-direction:column;gap:var(--sp-2)}
.kv .row{display:flex;justify-content:space-between;align-items:baseline;gap:var(--sp-4);padding:var(--sp-2) 0;border-bottom:1px solid var(--border)}
.kv .row:last-child{border-bottom:none}
.kv .k{color:var(--text-secondary);font-size:var(--fs-small);white-space:nowrap}
.kv .v{color:var(--text-body);font-family:var(--font-mono);font-size:var(--fs-small);text-align:right;word-break:break-all}
.alert-slab{background:var(--slab-bg);border:1px solid var(--slab-border);border-radius:var(--r-lg);padding:var(--sp-5);display:flex;justify-content:space-between;align-items:center;gap:var(--sp-5)}
.alert-slab .count{font-size:var(--fs-metric);font-weight:700;color:var(--text);font-family:var(--font-mono)}
.alert-slab .desc{font-size:var(--fs-small);color:var(--slab-fg);margin-top:var(--sp-1)}
.alert-slab.has-alert{border-color:var(--fail-border);background:var(--fail-bg)}
.alert-slab.has-alert .count{color:var(--fail-fg)}
.alert-slab.ok{border-color:var(--pass-border);background:var(--pass-bg)}
.alert-slab.ok .count{color:var(--pass-fg)}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--sp-4)}
.m-card{background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:var(--sp-5)}
.m-card span{font-size:var(--fs-micro);color:var(--text-muted);text-transform:uppercase;letter-spacing:.09em;font-weight:700}
.m-card b{display:block;font-size:var(--fs-h2);font-weight:700;color:var(--text);font-family:var(--font-mono);margin-top:var(--sp-1)}
.m-card small{display:block;font-size:var(--fs-tiny);color:var(--text-muted);margin-top:var(--sp-1)}
.m-card.fail b,.m-card .fail-text{color:var(--fail-fg)}
.m-card.pass b,.m-card .pass-text{color:var(--pass-fg)}
label.field{display:block;font-size:var(--fs-small);color:var(--text-secondary);margin-top:var(--sp-3)}
label.field input{width:100%;margin-top:var(--sp-1);padding:var(--sp-2) var(--sp-3);border:1px solid var(--border-strong);border-radius:var(--r-md);background:var(--bg-page);color:var(--text);outline:none;transition:border-color var(--dur-press) var(--ease-out)}
label.field input:focus{border-color:var(--accent)}
label.field select,label.field textarea{width:100%;margin-top:var(--sp-1);padding:var(--sp-2) var(--sp-3);border:1px solid var(--border-strong);border-radius:var(--r-md);background:var(--bg-page);color:var(--text);outline:none;color-scheme:inherit}
label.field select:focus,label.field textarea:focus{border-color:var(--accent)}
select option,select optgroup{background:var(--bg-surface);color:var(--text)}
input::placeholder,textarea::placeholder{color:var(--text-muted);opacity:.8}
.btn{border:1px solid var(--border-strong);border-radius:var(--r-md);padding:var(--sp-2) var(--sp-4);background:var(--bg-surface);color:var(--text-body);font-size:var(--fs-small);font-weight:600;cursor:pointer;transition:border-color var(--dur-press) var(--ease-out),transform var(--dur-press) var(--ease-out);margin-top:var(--sp-3);margin-right:var(--sp-2)}
.btn:active{transform:scale(.97)}
@media(hover:hover)and (pointer:fine){.btn:hover{border-color:var(--accent)}}
.btn.primary{background:var(--accent);color:var(--accent-on);border-color:var(--accent)}
.btn.danger{color:var(--fail-fg);border-color:var(--fail-border)}
.msg{margin-top:var(--sp-3);font-size:var(--fs-small)}
.msg.ok{color:var(--pass-fg)}.msg.err{color:var(--fail-fg);white-space:pre-wrap}
.provider-card{background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:var(--sp-5);box-shadow:var(--shadow-sm);margin-bottom:var(--sp-4)}
.provider-card h3{font-size:var(--fs-lead);font-weight:700;color:var(--text)}
.provider-card .url{font-family:var(--font-mono);font-size:var(--fs-tiny);color:var(--text-muted);margin:var(--sp-1) 0 var(--sp-3)}
.badge{font-size:var(--fs-micro);font-weight:700;padding:var(--sp-05) var(--sp-2);border-radius:var(--r-sm)}
.badge-ok{background:var(--pass-bg);color:var(--pass-fg);border:1px solid var(--pass-border)}
.badge-warn{background:var(--exception-bg);color:var(--exception-fg);border:1px solid var(--exception-border)}
.badge-fail{background:var(--fail-bg);color:var(--fail-fg);border:1px solid var(--fail-border)}
.fail-text{color:var(--fail-fg)}
.badge-muted{background:var(--bg-subtle);color:var(--text-muted);border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:var(--fs-tiny)}
th,td{text-align:left;border-bottom:1px solid var(--border);padding:var(--sp-2) var(--sp-2)}
th{color:var(--text-muted);font-weight:700;text-transform:uppercase;letter-spacing:.05em;font-size:var(--fs-micro)}
td{color:var(--text-body);font-variant-numeric:tabular-nums}
.tbl-wrap{overflow:auto;border:1px solid var(--border);border-radius:var(--r-lg);background:var(--bg-surface)}
.note{background:var(--bg-subtle);border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-4);font-size:var(--fs-small);color:var(--text-secondary);line-height:1.6}
.note strong{display:block;color:var(--text-body);margin-bottom:var(--sp-1)}
.note p{margin-top:var(--sp-2)}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:var(--sp-8);color:var(--text-muted);text-align:center;gap:var(--sp-2)}
.empty .ic{font-size:var(--fs-h1);opacity:.4}

.model-card,.account-card{background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:var(--sp-4);margin-bottom:var(--sp-3)}
.model-card .meta,.account-card .meta{font-size:var(--fs-tiny);color:var(--text-muted);margin-top:var(--sp-1)}
.think-ctl{display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap;margin-top:var(--sp-3)}
.think-ctl .lbl{font-size:var(--fs-tiny);color:var(--text-muted);font-weight:700}
.seg-mini{display:inline-flex;border:1px solid var(--border);border-radius:var(--r-md);overflow:hidden;background:var(--bg-subtle)}
.seg-mini button{border:0;border-right:1px solid var(--border);padding:6px 10px;background:transparent;color:var(--text-secondary);font-size:var(--fs-tiny);cursor:pointer}
.seg-mini button:last-child{border-right:0}.seg-mini button.on{background:var(--accent);color:var(--accent-on)}.seg-mini button:disabled{cursor:not-allowed;opacity:.45}
.cap-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--sp-2);margin-top:var(--sp-3)}
.cap-box{border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-3);font-size:var(--fs-small)}
.cap-box small{display:block;color:var(--text-muted);font-size:var(--fs-micro);margin-bottom:var(--sp-1)}
.pill{display:inline-block;border-radius:var(--r-pill);padding:2px 7px;margin:2px 4px 2px 0;background:var(--bg-subtle);font-size:var(--fs-tiny)}
.pill.ok{background:var(--pass-bg);color:var(--pass-fg)}
.cachebar{height:8px;border-radius:var(--r-pill);overflow:hidden;background:var(--bg-subtle);margin-top:var(--sp-2)}.cachebar span{display:block;height:100%;background:var(--accent)}
.theme-btn{border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-2) var(--sp-4);background:var(--bg-surface);color:var(--text-secondary);font-size:var(--fs-small);font-weight:600;cursor:pointer;transition:all var(--dur-press) var(--ease-out)}
.theme-btn:active{transform:scale(.97)}
.theme-btn.active{border-color:var(--accent);background:var(--accent);color:var(--accent-on)}
html.theme-transition,html.theme-transition *,html.theme-transition *::before,html.theme-transition *::after{transition:background-color .3s var(--ease-out),color .3s var(--ease-out),border-color .3s var(--ease-out),box-shadow .3s var(--ease-out)!important}
@media(prefers-reduced-motion:reduce){*{transition-duration:0ms!important;animation-duration:0ms!important}}
button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:720px){.app{grid-template-columns:1fr}aside{display:none}main{padding:var(--sp-4)}}
</style>
</head>
<body>
<div class="app">
<aside>
  <div class="brand"><b>K</b><div><strong>Kiro BYO</strong><small>ACP Harness <span id="version"></span></small></div></div>
  <nav id="nav">
    <button data-page="overview" class="active"><i>⌂</i>概览</button>
    <button data-page="providers"><i>◇</i>供应商</button>
    <button data-page="models"><i>◈</i>模型</button>
    <button data-page="accounts"><i>◉</i>账户</button>
    <button data-page="diagnostics"><i>◎</i>诊断</button>
    <button data-page="settings"><i>⚙</i>设置</button>
  </nav>
  <div class="aside-foot">
    <div><span class="dot"></span>绑定 127.0.0.1，仅本机访问</div>
    <div>ACP Harness for KiroCrew</div>
  </div>
</aside>
<main>

<!-- ===== 概览 ===== -->
<div class="page active" id="page-overview">
  <div class="page-head"><h1>概览</h1><p>后端健康、Provider 状态与近期活动。</p></div>
  <div class="scroll">
    <div class="alert-slab ok" id="status-slab">
      <div><div class="count" id="slab-count">—</div><div class="desc" id="slab-desc">正在读取后端状态…</div></div>
      <button class="btn" onclick="navTo('providers')">管理供应商</button>
    </div>
    <div class="metrics" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
      <div class="m-card"><span>Providers</span><b id="ov-providers">—</b><small>已连接供应商</small></div>
      <div class="m-card"><span>Models</span><b id="ov-models">—</b><small>已发现聊天模型</small></div>
      <div class="m-card"><span>Turns</span><b id="ov-turns">—</b><small>诊断窗口请求数</small></div>
      <div class="m-card fail"><span>Failed</span><b id="ov-failed">—</b><small>近期失败请求</small></div>
    </div>
    <div class="grid2">
      <div class="card"><h2>Crew 集成</h2><div class="kv" id="integration-kv"></div></div>
      <div class="card"><h2>模型能力</h2>
        <div class="note" style="background:transparent;border:none;padding:0">
          <p>图片、Tools、上下文、深度思考开关与思考力度档位按具体模型展示。</p>
          <p>未被上游声明或运行时证明的能力保持未知，不会凭厂商品牌猜测。</p>
          <button class="btn" onclick="navTo('models')">查看模型</button>
        </div>
      </div>
    </div>
    <div class="card"><h2>Provider 状态</h2><div id="ov-provider-status"></div></div>
  </div>
</div>

<!-- ===== 供应商 ===== -->
<div class="page" id="page-providers">
  <div class="page-head"><h1>供应商</h1><p>管理多个 Provider 实例；每个实例拥有独立 endpoint、凭据和模型目录。</p></div>
  <div class="scroll">
    <div class="card">
      <h2>添加供应商实例</h2><div class="note">同一家供应商或中转站可以添加多个账号 / endpoint，不会覆盖已有实例。</div>
      <label class="field">供应商<select id="preset"></select></label>
      <div id="preset-note" class="note" style="margin-top:var(--sp-2)"></div>
      <label class="field">Base URL<input id="base" placeholder="https://api.example.com/v1"></label>
      <div id="endpoint-note" class="msg"></div>
      <label class="field">API key<input id="key" type="password" autocomplete="new-password"></label>
      <label class="field">实例名称（可选）<input id="name"></label>
      <button class="btn primary" onclick="saveProvider()">发现并保存</button>
      <div id="formmsg" class="msg"></div>
    </div>
    <div><h2 style="font-size:var(--fs-lead);font-weight:700;color:var(--text);margin:var(--sp-2) 0 var(--sp-3)">已配置供应商</h2><div id="providers"></div></div>
  </div>
</div>

<!-- ===== 模型 ===== -->
<div class="page" id="page-models">
  <div class="page-head"><h1>模型</h1><p>查看输入输出模态、Tools、深度思考开关与思考力度档位。</p></div>
  <div class="scroll"><div id="models"></div></div>
</div>

<!-- ===== 账户 ===== -->
<div class="page" id="page-accounts">
  <div class="page-head"><h1>账户</h1><p>读取供应商明确提供的官方账户余额与状态，不用本地 token 估算余额。</p></div>
  <div class="scroll">
    <div style="display:flex;justify-content:flex-end"><button class="btn" onclick="loadAccounts()">刷新账户</button></div>
    <div class="metrics" id="accountstats"></div>
    <div id="accounts"></div>
  </div>
</div>

<!-- ===== 诊断 ===== -->
<div class="page" id="page-diagnostics">
  <div class="page-head"><h1>诊断</h1><p>后端 Turn 遥测。仅元数据——不记录 prompt、回复、工具参数与结果。</p></div>
  <div class="scroll">
    <div class="metrics" id="diagstats" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))"></div>
    <div class="card"><h2>Token 与缓存</h2><div id="tokenstats" class="metrics"></div><div id="cache-summary" style="margin-top:var(--sp-3)"></div></div>
    <div class="tbl-wrap">
      <table><thead><tr><th>结果</th><th>错误</th><th>供应商</th><th>请求 → 服务</th><th>深度思考</th><th>思考力度</th><th>TTFT</th><th>总耗时</th><th>恢复</th><th>工具</th><th>Tokens 入/出</th><th>缓存读 / 写</th></tr></thead>
      <tbody id="diagrows"></tbody></table>
    </div>
  </div>
</div>

<!-- ===== 设置 ===== -->
<div class="page" id="page-settings">
  <div class="page-head"><h1>设置</h1><p>运行时路径、外观主题与边界声明。</p></div>
  <div class="scroll">
    <div class="grid2">
      <div class="card"><h2>运行时</h2><div class="kv" id="runtime-kv"></div></div>
      <div class="card"><h2>外观</h2>
        <p style="font-size:var(--fs-small);color:var(--text-secondary);margin-bottom:var(--sp-4)">默认跟随系统，手动选过就记住。浅色与深色是两套独立取值。</p>
        <div style="display:flex;gap:var(--sp-2)">
          <button class="theme-btn" data-val="light" onclick="setTheme('light')">浅色</button>
          <button class="theme-btn" data-val="dark" onclick="setTheme('dark')">深色</button>
          <button class="theme-btn" data-val="system" onclick="setTheme('system')">跟随系统</button>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>推理语言</h2>
      <p style="font-size:var(--fs-small);color:var(--text-secondary)">控制上游可见 Thinking / reasoning summary 的主要语言。Auto 跟随当前用户对话；ACP、LLM、Token、API、模型名、代码标识符、路径和命令等技术内容保留最自然的原文。不会要求模型暴露额外隐藏思维链，也不强制改变最终回答语言。</p>
      <label class="field" style="max-width:360px">推理语言<select id="reasoning-language" onchange="setReasoningLanguage(this.value)">
        <option value="auto">自动（跟随用户）</option><option value="zh-CN">简体中文</option><option value="zh-TW">繁體中文</option><option value="en">English</option><option value="ja">日本語</option><option value="ko">한국어</option>
      </select></label>
      <div id="reasoning-language-note" class="msg"></div>
    </div>
    <div class="note"><strong>数据边界</strong><p>Kiro BYO 绑定 127.0.0.1，仅本机访问。一个每进程随机 token 保护所有 API 请求。凭据写入即只写——接口永不回读明文。</p></div>
  </div>
</div>

</main>
</div>
<script>
if (window.location.search) history.replaceState({}, document.title, window.location.pathname);
const TOKEN=__TOKEN__;
const headers={'X-Kiro-BYO-Token':TOKEN,'Content-Type':'application/json'};
async function api(path,opts={}){opts.headers={...headers,...(opts.headers||{})};let r=await fetch(path,opts);let j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function ms(v){v=Number(v||0);return v<0?'—':(v>=1000?(v/1000).toFixed(2)+'s':Math.round(v)+'ms')}
function kvRows(el,pairs){el.innerHTML=pairs.map(([k,v])=>`<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('')}

/* ===== 导航 ===== */
function navTo(p){document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===p));document.querySelectorAll('.page').forEach(pg=>pg.classList.toggle('active',pg.id==='page-'+p))}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>navTo(b.dataset.page)));

/* ===== 主题 ===== */
function setTheme(t){const choice=t;if(t==='system'){localStorage.removeItem('kiroapi.theme');t=window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'}else{localStorage.setItem('kiroapi.theme',t)}document.documentElement.classList.add('theme-transition');document.documentElement.setAttribute('data-theme',t);setTimeout(()=>document.documentElement.classList.remove('theme-transition'),350);updateThemeBtns(choice)}
function updateThemeBtns(choice){document.querySelectorAll('.theme-btn').forEach(btn=>btn.classList.toggle('active',btn.dataset.val===choice))}
(function(){const s=localStorage.getItem('kiroapi.theme');const theme=s||(window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');document.documentElement.setAttribute('data-theme',theme);setTimeout(()=>updateThemeBtns(s||'system'),0)})();

/* ===== 数据加载 ===== */
let PRESETS=[];
async function load(){try{let [s,d,pres]=await Promise.all([api('/api/status'),api('/api/diagnostics?limit=12'),api('/api/presets')]);
  PRESETS=pres.presets||[];renderPresetSelect();
  document.getElementById('version').textContent='v'+s.version;
  let i=s.integration||{};
  // 概览统计
  document.getElementById('ov-providers').textContent=(s.providers||[]).length;
  document.getElementById('ov-models').textContent=(s.providers||[]).reduce((n,p)=>n+Number(p.model_count||0),0);
  document.getElementById('ov-turns').textContent=(d.summary||{}).turns||0;
  document.getElementById('ov-failed').textContent=(d.summary||{}).failed||0;
  // 状态横幅
  let missing=(s.providers||[]).filter(p=>!p.credential.configured).length;
  let slab=document.getElementById('status-slab');
  if((s.providers||[]).length===0){slab.className='alert-slab';document.getElementById('slab-count').textContent='0';document.getElementById('slab-desc').textContent='尚未配置任何供应商。到「供应商」页添加一个即可开始。'}
  else if(missing>0){slab.className='alert-slab has-alert';document.getElementById('slab-count').textContent=missing;document.getElementById('slab-desc').textContent='个供应商缺少凭据。到「供应商」页补上 API key。'}
  else{slab.className='alert-slab ok';document.getElementById('slab-count').textContent='✓';document.getElementById('slab-desc').textContent=(s.providers||[]).length+' 个供应商就绪，凭据齐全。目标客户端：'+esc(s.target_client)+'。'}
  // Crew 集成 kv
  kvRows(document.getElementById('integration-kv'),[['目标客户端',s.target_client],['Crew 能力','以 KiroCrew 代理运行框架页面为准'],['Crew baseline',i.baseline||'未记录'],['Overlay 版本',i.overlay_version||'未记录'],['凭据后端',s.credential_backend]]);
  // 运行时 kv
  kvRows(document.getElementById('runtime-kv'),[['版本','v'+s.version],['Kiro BYO home',s.kiro_byo_home],['配置文件',s.config_path],['KiroCrew 数据',s.kiro_crew_data_home||'未记录'],['凭据后端',s.credential_backend]]);
  let rl=document.getElementById('reasoning-language');if(rl)rl.value=(s.reasoning_language||{}).reasoning_language||'auto';
  renderProviders(s.providers);renderModels(s.providers);renderOverviewProviders(s.providers);renderDiagnostics(d)
}catch(e){document.getElementById('slab-desc').innerHTML='<span class="msg err">'+esc(e.message)+'</span>'}}

function renderPresetSelect(){let el=document.getElementById('preset');if(!el)return;let current=el.value||'deepseek';el.innerHTML=PRESETS.map(p=>`<option value="${esc(p.id)}" ${p.available?'':'disabled'}>${esc(p.display_name)}${p.available?'':'（暂未支持）'}</option>`).join('');if([...el.options].some(o=>o.value===current&&!o.disabled))el.value=current;else if(el.options.length)el.selectedIndex=0;syncPresetBase();renderPresetNote()}
function syncPresetBase(){let el=document.getElementById('preset'),p=PRESETS.find(x=>x.id===el?.value);if(p&&p.default_base_url&&!base.value)base.value=p.default_base_url;renderEndpointNote()}
function renderPresetNote(){let el=document.getElementById('preset'),p=PRESETS.find(x=>x.id===el?.value),n=document.getElementById('preset-note');if(!n)return;if(!p){n.textContent='';n.style.display='none';return}n.style.display='block';let strategy=p.transport_strategy_label||p.transport_label||'OpenAI Compatible';n.innerHTML=`<strong>接入策略：${esc(strategy)}</strong>${p.transport_strategy==='model'?'<p>协议按具体模型选择；只有官方已验证支持的模型才启用原生协议，其余模型保持兼容协议。</p>':''}${p.note?`<p>${esc(p.note)}</p>`:''}`}
function renderEndpointNote(){let el=document.getElementById('preset'),p=PRESETS.find(x=>x.id===el?.value),n=document.getElementById('endpoint-note');if(!n)return;let current=(base.value||'').replace(/[/]$/,'');let official=[((p&&p.default_base_url)||''),...((p&&p.official_base_urls)||[])].map(x=>String(x||'').replace(/[/]$/,'')).filter(Boolean);if(p&&p.id!=='custom'&&current&&!official.includes(current)){n.className='msg err';n.textContent='检测到自定义 endpoint：为避免把厂商原生协议误发到中转站，本实例会安全回退为 OpenAI Compatible；模型能力也不会套用官方厂商档案。'}else{n.className='msg';n.textContent=''}}
setTimeout(()=>{let el=document.getElementById('preset');if(el)el.addEventListener('change',()=>{let p=PRESETS.find(x=>x.id===el.value);if(p)base.value=p.default_base_url||'';renderPresetNote();renderEndpointNote()});let b=document.getElementById('base');if(b)b.addEventListener('input',renderEndpointNote)},0);

function renderProviders(ps){let root=document.getElementById('providers');if(!ps||!ps.length){root.innerHTML='<div class="empty"><span class="ic">◇</span><div>还没有配置供应商</div></div>';return}root.innerHTML='';for(let p of ps){let d=document.createElement('div');d.className='provider-card';let tlabel=p.transport_label||p.provider_transport_label||'OpenAI Compatible';d.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;gap:12px"><h3>${esc(p.name)} <span style="color:var(--text-muted);font-weight:400;font-size:var(--fs-small)">(${esc(p.provider_family||p.preset_id||'Provider')} · ${esc(p.instance_id||p.id)})</span></h3><span class="badge ${p.credential.configured?'badge-ok':'badge-warn'}">${p.credential.configured?'凭据已配置':'缺少凭据'}</span></div><div class="url">${esc(p.base_url)}</div><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:var(--sp-3)"><span class="badge ${p.native_transport?'badge-ok':'badge-muted'}">接入：${esc(tlabel)}</span>${p.official_endpoint===false?'<span class="badge badge-warn">自定义 endpoint · 安全兼容模式</span>':''}</div><div style="font-size:var(--fs-small);color:var(--text-secondary)">模型数：<b style="color:var(--text)">${esc(p.model_count)}</b> <button class="btn" style="margin-top:0" onclick="navTo('models')">查看模型</button></div><label class="field">轮换 API key<input data-key type="password" autocomplete="new-password" placeholder="留空则不修改"></label><button class="btn" data-act="key">替换 key</button><button class="btn" data-act="refresh">重新发现模型</button><button class="btn danger" data-act="delete">删除</button>`;d.querySelector('[data-act=key]').onclick=()=>replaceKey(p.id,d.querySelector('[data-key]'));d.querySelector('[data-act=refresh]').onclick=()=>refreshModels(p.id);d.querySelector('[data-act=delete]').onclick=()=>removeProvider(p.id);root.appendChild(d)}}

function modText(xs){return (xs&&xs.length)?xs.map(x=>({text:'文本',image:'图片',audio:'音频',video:'视频',file:'文件'})[String(x).toLowerCase()]||x).join(' / '):'未声明'}
function thinkingText(c){let r=(c&&c.reasoning)||{},t=r.thinking||{};if(t.supported===false)return '不支持';if(t.supported!==true)return '未声明';if(t.can_disable===true)return '可调节：自动 / 开 / 关';if(t.can_disable===false)return '固定开启（不可关闭）';return '支持；开关能力未完整声明'}
function effortText(c){let r=(c&&c.reasoning)||{},e=r.effort||{},xs=Array.isArray(e.levels)?e.levels:[];if(xs.length>1)return xs.length+' 档：'+xs.join(' / ');if(xs.length===1)return '固定：'+xs[0];return '无可调强度 / 未声明'}
function renderModels(ps){let root=document.getElementById('models');root.innerHTML='';let total=0;for(let p of ps||[]){if(!(p.models||[]).length)continue;let h2=document.createElement('h2');h2.style.cssText='font-size:var(--fs-lead);margin:var(--sp-2) 0';h2.innerHTML=`${esc(p.name)} · ${esc(p.instance_id||p.id)} · ${esc(p.models.length)} 个 <span class="badge ${p.native_transport?'badge-ok':'badge-muted'}" style="margin-left:8px">${esc(p.transport_label||p.provider_transport_label||'OpenAI Compatible')}</span>`;root.appendChild(h2);for(let m of p.models){total++;let c=m.capabilities||{},r=c.reasoning||{},t=r.thinking||{};let d=document.createElement('div');d.className='model-card';let badges=`<span class="badge ${m.native_transport?'badge-ok':'badge-muted'}">${esc(m.transport_label||'OpenAI Compatible')}</span>`;let sw=t.supported===true&&t.can_disable===true,pref=['auto','on','off'].includes(String(m.thinking_preference||'').toLowerCase())?String(m.thinking_preference).toLowerCase():'auto',visual=sw?pref:(t.supported===true&&t.can_disable===false?'on':t.supported===false?'off':'');let btns=['auto','on','off'].map(mode=>`<button data-think="${mode}" class="${visual===mode?'on':''}" ${sw?'':'disabled'}>${mode==='auto'?'自动':mode==='on'?'开':'关'}</button>`).join('');let note=sw?'自动：在 Type 中由形态按本轮决定；固定模型时沿用模型/厂商默认。开/关为用户硬锁。':t.supported===true&&t.can_disable===false?'模型固定开启深度思考，无法切换。':t.supported===true?'模型支持深度思考，但厂商未明确声明是否可关闭；暂不提供开关。':t.supported===false?'模型不支持深度思考，无法切换。':'能力未声明，暂不提供控制。';d.innerHTML=`<div style="display:flex;justify-content:space-between;gap:12px"><strong>${esc(m.name||m.id)}</strong><div style="display:flex;gap:6px;flex-wrap:wrap">${badges}</div></div><div class="meta">${esc(m.id)}</div><div class="cap-grid"><div class="cap-box"><small>输入模态</small>${esc(modText(c.input_modalities))}</div><div class="cap-box"><small>输出模态</small>${esc(modText(c.output_modalities))}</div><div class="cap-box"><small>Tools</small>${c.tool_use===true?'支持':c.tool_use===false?'不支持':'未声明'}</div><div class="cap-box"><small>上下文 / 最大输出</small>${esc(c.context_window||'未声明')} / ${esc(c.max_output_tokens||'未声明')}</div><div class="cap-box"><small>深度思考</small>${esc(thinkingText(c))}</div><div class="cap-box"><small>思考力度</small>${esc(effortText(c))}</div></div><div class="think-ctl"><span class="lbl">深度思考</span><span class="seg-mini">${btns}</span><span class="meta">${esc(note)}</span></div><div class="meta">深度思考和思考力度独立展示 · 能力来源：${esc(r.source||'unknown')} · 接入：${esc(m.transport_label||'OpenAI Compatible')}</div>`;if(sw){d.querySelectorAll('[data-think]').forEach(b=>b.onclick=()=>setModelThinking(p.id,m.id,b.dataset.think))}root.appendChild(d)}}if(!total)root.innerHTML='<div class="empty">尚未发现模型。</div>'}
function renderOverviewProviders(ps){let root=document.getElementById('ov-provider-status');if(!ps||!ps.length){root.innerHTML='<div class="empty">尚未配置供应商。</div>';return}root.innerHTML=(ps||[]).slice(0,8).map(p=>`<div class="kv"><div class="row"><span class="k">${esc(p.name)} <small>${esc(p.instance_id||p.id)}</small></span><span class="v">${p.credential.configured?'正常':'缺少 Key'} · ${esc(p.model_count||0)} models · ${esc(p.transport_label||p.provider_transport_label||'兼容')}</span></div></div>`).join('')}

async function loadAccounts(){let root=document.getElementById('accounts');root.innerHTML='<div class="empty">正在查询账户…</div>';try{let a=await api('/api/accounts');let s=a.summary||{};document.getElementById('accountstats').innerHTML=`<div class="m-card"><span>已配置供应商</span><b>${esc(s.configured_providers||0)}</b></div><div class="m-card"><span>具有账户能力</span><b>${esc(s.capable_providers||0)}</b></div><div class="m-card pass"><span>查询成功</span><b>${esc(s.ok||0)}</b></div><div class="m-card fail"><span>查询异常</span><b>${esc(s.errors||0)}</b></div>`;root.innerHTML='';for(let r of a.accounts||[]){let d=document.createElement('div');d.className='account-card';let balances=(r.balances||[]).map(b=>`<div class="cap-box"><small>${esc(b.currency)}</small><strong style="font-size:var(--fs-h2)">${esc(b.total)}</strong><div class="meta">充值 ${esc(b.topped_up||'—')} · 赠金 ${esc(b.granted||'—')}</div></div>`).join('');d.innerHTML=`<div style="display:flex;justify-content:space-between;gap:12px"><strong>${esc(r.provider_name)}</strong><span class="badge ${r.state==='ok'?'badge-ok':r.state==='credential_missing'?'badge-warn':'badge-fail'}">${r.state==='ok'?'查询成功':r.state==='credential_missing'?'缺少凭据':'查询失败'}</span></div><div class="meta">${esc(r.base_url)}</div>${balances?`<div class="cap-grid">${balances}</div>`:''}<div class="meta">${esc(r.note||'')}</div>`;root.appendChild(d)}if(!(a.accounts||[]).length)root.innerHTML='<div class="empty">当前供应商没有 Kiro BYO 已确认可用的官方账户查询能力。模型调用不受影响。</div>'}catch(e){root.innerHTML='<div class="msg err">'+esc(e.message)+'</div>'}}

function renderDiagnostics(d){let s=d.summary||{};document.getElementById('diagstats').innerHTML=`<div class="m-card"><span>近期 Turn</span><b>${esc(s.turns||0)}</b></div><div class="m-card"><span>成功 / 失败 / 取消</span><b><span class="pass-text">${esc(s.success||0)}</span> / <span class="fail-text">${esc(s.failed||0)}</span> / ${esc(s.cancelled||0)}</b></div><div class="m-card"><span>TTFT 中位</span><b>${ms(s.median_first_visible_ms)}</b></div><div class="m-card"><span>总耗时中位</span><b>${ms(s.median_total_ms)}</b></div>`;let cacheKnown=Number(s.cache_usage_reported_turns||0)>0,writeKnown=Number(s.cache_write_usage_reported_turns||0)>0;document.getElementById('tokenstats').innerHTML=`<div class="m-card"><span>输入 Token</span><b>${esc(s.input_tokens||0)}</b></div><div class="m-card"><span>输出 Token</span><b>${esc(s.output_tokens||0)}</b></div><div class="m-card"><span>推理 Token</span><b>${Number(s.reasoning_usage_reported_turns||0)>0?esc(s.reasoning_tokens||0):'—'}</b></div><div class="m-card"><span>缓存读取 Token</span><b>${cacheKnown?esc(s.cached_tokens||0):'—'}</b></div><div class="m-card"><span>缓存写入 Token</span><b>${writeKnown?esc(s.cache_write_tokens||0):'—'}</b></div>`;let rate=cacheKnown&&Number(s.input_tokens||0)>0?Number(s.cached_tokens||0)/Number(s.input_tokens||0):null;document.getElementById('cache-summary').innerHTML=rate==null?'<span style="color:var(--text-muted)">上游未明确报告缓存读取 token；未知不会显示为 0%。缓存写入仅在厂商明确返回时记录。</span>':`缓存命中率 <strong>${(rate*100).toFixed(1)}%</strong><div class="cachebar"><span style="width:${Math.max(0,Math.min(100,rate*100))}%"></span></div>`;let rows=document.getElementById('diagrows');if(!(d.turns||[]).length){rows.innerHTML='<tr><td colspan="12" style="text-align:center;color:var(--text-muted);padding:var(--sp-6)">暂无 Turn 记录</td></tr>';return}rows.innerHTML='';for(let t of [...(d.turns||[])].reverse()){let known=!!t.cache_usage_reported||Number(t.cached_tokens||0)>0,writeKnown=!!t.cache_write_usage_reported||Number(t.cache_write_tokens||0)>0;let cr=known&&Number(t.input_tokens||0)>0?(Number(t.cached_tokens||0)/Number(t.input_tokens||0)*100).toFixed(1)+'%':'—';let cw=writeKnown?esc(t.cache_write_tokens||0):'—';let tr=document.createElement('tr'),err=t.error_category?(t.error_kind?`${esc(t.error_category)} · ${esc(t.error_kind)}`:esc(t.error_category)):(t.error_kind?esc(t.error_kind):'—'),recovery=`retry ${esc(t.transport_retries||0)} / fallback ${esc(t.fallbacks||0)}`;tr.innerHTML=`<td class="${['failed','error'].includes(String(t.outcome||'').toLowerCase())?'fail-text':''}">${esc(t.outcome)}</td><td class="${err!=='—'?'fail-text':''}">${err}</td><td>${esc(t.provider_id)}</td><td>${esc(t.requested_model)} → ${esc(t.served_model)}</td><td>${esc(t.thinking_mode||'—')}</td><td>${esc(t.effort||'—')}</td><td>${ms(t.first_visible_ms)}</td><td>${ms(t.total_ms)}</td><td>${recovery}</td><td>${esc(t.tool_calls||0)} (${esc(t.native_calls||0)} native)</td><td>${esc(t.input_tokens||0)} / ${esc(t.output_tokens||0)}</td><td>${cr} / ${cw}</td>`;rows.appendChild(tr)}}

async function saveProvider(){let m=document.getElementById('formmsg');m.textContent='处理中…';m.className='msg';try{await api('/api/providers',{method:'POST',body:JSON.stringify({preset_id:preset.value||'custom',base_url:base.value,api_key:key.value,name:name.value,create_new:true})});key.value='';m.textContent='已添加实例。';m.className='msg ok';await load()}catch(e){m.textContent=e.message;m.className='msg err'}}
async function replaceKey(id,input){let v=input.value;if(!v)return;try{await api('/api/providers/'+encodeURIComponent(id)+'/credential',{method:'PUT',body:JSON.stringify({api_key:v})});input.value='';await load()}catch(e){alert(e.message)}}
async function refreshModels(id){try{await api('/api/providers/'+encodeURIComponent(id)+'/refresh',{method:'POST',body:'{}'});await load()}catch(e){alert(e.message)}}
async function setModelThinking(providerId,modelId,mode){try{await api('/api/providers/'+encodeURIComponent(providerId)+'/models/'+encodeURIComponent(modelId)+'/thinking',{method:'PUT',body:JSON.stringify({mode})});await load()}catch(e){alert(e.message)}}
async function setReasoningLanguage(value){let n=document.getElementById('reasoning-language-note');if(n){n.textContent='保存中…';n.className='msg'}try{await api('/api/preferences/reasoning-language',{method:'PUT',body:JSON.stringify({value})});if(n){n.textContent='已保存；下一轮对话生效。';n.className='msg ok'}}catch(e){if(n){n.textContent=e.message;n.className='msg err'}}}
async function removeProvider(id){if(!confirm('删除供应商 '+id+' 及其存储的凭据？'))return;try{await api('/api/providers/'+encodeURIComponent(id),{method:'DELETE'});await load()}catch(e){alert(e.message)}}
load();loadAccounts();
</script>
</body></html>"""
    return html.replace("__TOKEN__", json.dumps(token))


def serve_ui(*, port: int = 8765, open_browser: bool = True, store: CredentialStore | None = None) -> None:
    store = store or default_credential_store()
    token = secrets.token_urlsafe(24)
    server = _ManagementServer(("127.0.0.1", int(port)), store, token)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/?token={urllib.parse.quote(token)}"
    print(f"Kiro BYO for KiroCrew management UI: {url}")
    print(f"Credential backend: {store.backend_name}")
    print("Bound to loopback only. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
