"""The ``linkOpen`` point on the shared app frame: its resolver and its frame script.

Registered in ``apps/app_frame.py``. A link-open frame is minted when the user
chooses to open a claimed link in the app (each mint names the clicked host and the
URL mode, checked by :func:`admit_link_open` against the live grant), and gets one
invocation per open on the shared invocation contract (``_INVOCATION`` there):

- host to frame ``link.open { invocationId, deadlineMs, url, theme }``;
- the script calls the module's ``open(link)``, where ``link`` carries ``url``,
  ``theme``, ``deadlineMs`` and the invocation's frozen controls (``resize``,
  ``close``, ``error``, ``offerCopy``, ``offerOpen``);
- when ``open`` settles the script reports the document's size
  (``invocation.resize``), which is the host's signal that the app has drawn the
  link; when it throws, ``invocation.error``. ``invoke.cancel`` reaches the
  module's ``cancel(invocationId, reason)`` through the shared runtime.

Each ``invocationId`` is opened once; a repeat is ignored.
"""

from __future__ import annotations

from collections.abc import Mapping

from kiro_crew.apps.app_hosts import host_refusal, normalize_host
from kiro_crew.apps.link_open_grants import link_open_grant_row
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled


def resolve_link_open(name: str, entry_id: str) -> tuple[str, bool, str] | None:
    """The served entry of *name*'s ``linkOpen`` entry *entry_id*, or None.

    None unless the app is enabled, its manifest names itself, the entry exists and
    is contained in ``ui/``, and the owner approved at least one of its hosts.
    """
    # Imported here: app_frame imports this module to register the point.
    from kiro_crew.apps.app_frame import clean_entry, process_backed

    if not is_app_enabled(name):
        return None
    manifest = get_app_manifest(name)
    if manifest is None or manifest.name != name:
        return None
    found = [e for e in manifest.contributes.linkOpen if e.id == entry_id]
    if not found:
        return None
    entry = clean_entry(found[0].entry)
    if entry is None:
        return None
    grant = link_open_grant_row(name, manifest).get("linkOpenGrant") or {}
    if not grant.get("hosts"):
        return None
    return entry, process_backed(manifest), manifest.name


def _claims(patterns: list[str], host: str) -> bool:
    """``host`` matched by an approved pattern, as the dashboard's ``claimsHost``:
    ``.example.dev`` covers ``example.dev`` and every subdomain."""
    for pattern in patterns:
        if host_refusal(pattern) is not None:
            continue
        if pattern.startswith("."):
            if host == pattern[1:] or host.endswith(pattern):
                return True
        elif host == pattern:
            return True
    return False


def admit_link_open(name: str, entry_id: str, query: Mapping[str, str]) -> bool:
    """Whether this mint's clicked ``host`` and URL mode are within the live grant.

    Every mint (and every renewal) names the host of the link it opens and whether
    the app is handed the whole URL (``url=full``) or only its origin and path
    (``url=stripped``). A host outside the owner's live grant, or a whole URL the
    live grant does not include, is refused before the frame exists, so a tab
    still holding an older app list cannot hand the app what the owner took back.
    """
    host = query.get("host", "")
    mode = query.get("url")
    if mode not in ("full", "stripped") or not host or normalize_host(host) != host:
        return False
    if host.startswith(".") or host_refusal(host) is not None:
        return False
    manifest = get_app_manifest(name)
    if manifest is None or manifest.name != name:
        return False
    found = [e for e in manifest.contributes.linkOpen if e.id == entry_id]
    if not found:
        return False
    grant = link_open_grant_row(name, manifest).get("linkOpenGrant") or {}
    if not _claims(list(grant.get("hosts") or []), host):
        return False
    return mode == "stripped" or (found[0].fullUrl and grant.get("fullUrl") is True)


LINK_OPEN_POINT_SCRIPT = r"""
const LINK_OPEN_ID = /^[A-Za-z0-9_-]{8,64}$/;
const linkOpens = new Set();
function sizeReport() {
  const el = document.documentElement;
  return { width: clampNum(el && el.scrollWidth), height: clampNum(el && el.scrollHeight) };
}
const POINT = {
  ctx: { appName: CFG.app, theme: () => CFG.theme, fetch: capFetch },
  accepts: (h) => !!h && typeof h.open === 'function',
  missing: 'activate(ctx) returned no link opener',
  message(m) {
    if (m.type !== 'link.open') return;
    if (typeof m.invocationId !== 'string' || !LINK_OPEN_ID.test(m.invocationId)) return;
    if (typeof m.url !== 'string' || linkOpens.has(m.invocationId) || !handler) return;
    linkOpens.add(m.invocationId);
    CFG.theme = m.theme === 'dark' ? 'dark' : 'light';
    const controls = invocationControls(m.invocationId);
    const link = Object.freeze(Object.assign({
      url: m.url,
      theme: CFG.theme,
      deadlineMs: Number.isFinite(m.deadlineMs) ? m.deadlineMs : 0,
    }, controls));
    const h = handler;
    Promise.resolve()
      .then(() => h.open(link))
      .then(() => controls.resize(sizeReport()), (err) => {
        console.error('[app-frame] ' + CFG.app + ':', err);
        controls.error(String((err && err.message) || err));
      });
  },
};
"""

__all__ = ["LINK_OPEN_POINT_SCRIPT", "admit_link_open", "resolve_link_open"]
