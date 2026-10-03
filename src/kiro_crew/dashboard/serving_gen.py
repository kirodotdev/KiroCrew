"""The generation a transcript read's rows were served under.

A dashboard tab keeps the older rows of a long chat above its newest page and
re-serves them only when this value moves (or when it marks its own loaded rows
changed). So it must move with EVERY input that conditions what
``chat_utils._prepare_messages`` serves, not just the redaction allow-list: an
input that is not folded in here leaves those older rows stale, silently.

Every such input is registered in :data:`SERVING_INPUTS`, the one place the
value is derived from. ``test_slot_detail_full_history.TestServingInputsArePinned``
fails when the code reachable from ``_prepare_messages`` changes, so a new input
cannot land without someone deciding whether it belongs here.

A keyed hash (HMAC) under a domain-separated use of the dashboard's persisted
signing secret, the precedent ``chat_tag_grants`` set. Keyed because the value
rides the status frame every socket receives, app tokens included, while the
allow-list itself is owner-only: a plain hash over that small input space would
let a socket confirm guessed hosts. A per-process boot value (``_BOOT``) is folded
in, so the value also never repeats across a restart: the write counts restart
at zero, and without it a list revoked back to its boot state would answer the
value a tab took before the restart. The cost: after each gateway restart every
tab re-reads the open chat and each on-screen pane once, and a view that holds
more than one page ceiling of rows (older pages loaded by scrolling up) re-reads
its whole transcript, since no bounded page re-serves every row it holds. Only
on-screen slots pay it, and a restart already drops every socket, so it lands
beside the reconnect. A content-only value would avoid it, but the value reaches
tabs on a polled frame: a list written and written back between two polls would
answer the value a tab already holds while that tab kept rows read in between.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable

from kiro_crew.config import loader as config_loader
from kiro_crew.dashboard import token_secret
from kiro_crew.security import exfil, redaction_allow

#: Domain separation from the secret's other uses (token signing, grant certs).
_DOMAIN = b"kiro-crew:serving-generation:v1\x00"

#: This process's own value. The inputs' write counts (``redaction_allow._writes``,
#: ``exfil.oauth_extension_changes``) are in memory and restart at zero, so a list
#: revoked back to its boot state after a restart would hash to the value a tab
#: took before it; with this folded in, a restart always reads as a change.
_BOOT = secrets.token_hex(8)


def _exempt_hosts() -> list[str]:
    # The companion's trusted-tenant hosts skip the exfil length/base64 checks.
    return sorted(exfil._exfil_exempt_hosts())


def _oauth_endpoints() -> list[object]:
    # The operator's ``oauth_endpoints.json`` extension, re-read when it changes.
    # Its stat rides along so a file edited and then edited back still moves the
    # value: a render spanning both edits may have prepared rows under the middle
    # state, so the state it ends in must not vouch for them. ``st_ctime_ns`` is
    # set by the kernel on every change and cannot be restored, so even a backup
    # copied back with its old mtime and size moves the value; device and inode
    # catch a file replaced by another. Where ``st_ctime`` is the creation time
    # (Windows) a restore can reproduce the whole stat, so the count of changes
    # to the set the loader has returned in this process rides along too: a render
    # that consumed the middle state bumped it, and it never counts back.

    try:
        st = config_loader.oauth_endpoints_path().stat()
        stamp: list[int] | None = [st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_dev, st.st_ino]
    except OSError:
        stamp = None
    endpoints = sorted([host, path] for host, path in exfil._load_operator_oauth_endpoints())
    return [stamp, exfil.oauth_extension_changes(), endpoints]


#: Every input that conditions what ``_prepare_messages`` serves, by name. Each
#: reader looks its source up at call time, so a test can stand one in. Not one:
#: the owner's credential-redaction switch, reachable through
#: ``redaction_switch.credential_pass_bypassed`` but True only inside
#: the owner-view scope, which only the file viewer enters -- a chat render never does
#: (pinned by ``test_the_credential_switch_never_conditions_a_chat_render``). Nor
#: a reply variant switch: it changes one slot's rows, so it is counted on that
#: slot (``_ChatSlot.variant_seq``, carried on the slot list) and re-serves that
#: slot alone rather than moving this workspace-wide value.
SERVING_INPUTS: tuple[tuple[str, Callable[[], object]], ...] = (
    ("allowed_hosts", lambda: redaction_allow.list_fingerprint()),
    ("exempt_hosts", _exempt_hosts),
    ("oauth_endpoints", _oauth_endpoints),
)


def serving_generation() -> str:
    """Opaque value that moves whenever any of :data:`SERVING_INPUTS` does.

    Does file I/O (the OAuth extension's stat, a first secret load): call it off
    the event loop, as the status snapshot, the allow/revoke routes and the
    slot-detail render all do."""
    inputs = [[name, read()] for name, read in SERVING_INPUTS]
    body = json.dumps([_BOOT, inputs], separators=(",", ":"))
    mac = hmac.new(token_secret._get_secret(), _DOMAIN + body.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:16]
