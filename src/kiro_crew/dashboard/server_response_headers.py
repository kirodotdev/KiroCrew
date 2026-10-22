"""The response header values and cache policies the dashboard sets besides its CSP.

The Permissions-Policy, the CORS and Private Network Access answer for ``/vendor/``,
and the cache-control policies for hashed assets, worker chunks and everything else.
Pure data: ``kiro_crew.dashboard.server`` imports every name here and is the only
module that does, so the server's binding is the one its security-headers owner reads
and the one a test rebinds.
"""

from __future__ import annotations

# Permissions-Policy header. Chrome 143+ changed the default policy so
# that clipboard-write is DENIED unless explicitly allowlisted, even in
# secure contexts like http://localhost (crbug.com/414348233). Without
# this header, ``navigator.clipboard.writeText`` fails with a permissions
# policy violation, breaking the "Copy link" button on published
# artifacts. Grant same-origin only; cross-origin remains denied.
_PERMISSIONS_POLICY = "clipboard-write=(self), clipboard-read=(self)"


# /vendor/* is fetched by sandboxed widget/artifact iframes, which are
# null-origin (srcdoc/blob) documents and therefore NON-secure contexts. On the
# default deployment the gateway is plain http on loopback — a "more-private
# address space" under Chrome's Private Network Access policy — which blocks
# the iframe's <script src> for the Tailwind runtime unless the load goes
# through CORS with server approval: the tag carries
# crossorigin="anonymous" (widgetSrcdoc.ts) and this response carries
# Access-Control-Allow-Origin. Verified against real Chromium: with the
# header the runtime loads; without it the load hard-fails (crossorigin
# makes the header MANDATORY, not additive), the runtime never arrives,
# Tailwind-classed widgets render unstyled, and the widget loading overlay
# sits on its hang backstop (blank box). `*` leaks nothing:
# /vendor/ holds only public, non-secret static JS (already auth-exempt via
# token_auth._BYPASS_PREFIXES) and the response carries no credentials or
# user data.
_VENDOR_PATH_PREFIX = "/vendor/"


_VENDOR_CORS_HEADER_VALUE = "*"


_PNA_REQUEST_HEADER = "Access-Control-Request-Private-Network"


_PNA_RESPONSE_HEADER = "Access-Control-Allow-Private-Network"


# Two hours — Chrome caps preflight cache entries at 7200s, so a larger value
# documents a guarantee the browser does not honour. The vendor files are
# stable, unversioned assets; caching the approval avoids a preflight per
# widget for the cap's duration.
_VENDOR_PREFLIGHT_MAX_AGE_SECS = 7200


# Content-hashed build output (Vite emits ``/assets/<name>-<hash>.<ext>``;
# the URL changes whenever the content changes) is safe to cache forever.
# Everything else — index.html, the SPA shell, /api — keeps the no-store
# policy so upgrades are picked up immediately. Without this exemption the
# ~6MB entry bundle is re-downloaded on every page load, and a reload right
# after a gateway restart bets the whole page on that transfer succeeding
# while the gateway is at cold-start peak (the "black screen until hard
# refresh" failure mode). Deliberately excludes /vendor, /fonts and
# /sprites: those use stable, un-hashed filenames.
_IMMUTABLE_PATH_PREFIXES = ("/assets/",)


_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


_NO_STORE_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"


# Worker scripts are the one hashed asset whose runtime behaviour is governed
# by its OWN response header rather than the document's: a same-origin worker
# takes its CSP from the header on its script RESPONSE, not from the page that
# spawned it (see _BASE_CSP's script-src 'wasm-unsafe-eval' note). Vite content-
# hashes a chunk by its CONTENT only, so a build that changes just a header — a
# CSP directive, or a cache policy — keeps the identical hashed filename. Under
# ``immutable`` a browser that cached the worker never re-fetches it, so it
# replays the stale header for up to a year and runs on a header that differs
# from the one the running build serves (WASM refused, every diff/highlight
# surface dead until a hard refresh). A plain JS/CSS chunk is unaffected: it
# runs under the DOCUMENT's CSP, and the shell that carries it is served
# no-store, so the fresh policy always wins; a worker has no fresher copy to
# override it.
#
# Workers therefore use a SHORT-LIVED cacheable policy, not ``immutable`` and
# not ``no-store``. A 60-second ``max-age`` lets the browser serve the worker
# from cache for a minute (covering a burst of loads and a brief gateway
# restart with no round-trip), then re-fetch it — so a header-only change
# reaches the worker within a minute instead of a year, bounding the stale-CSP
# window to a minute of degraded highlight. ``no-store`` is wrong here: it would
# evict the bytes and make the worker unloadable the moment the gateway is
# unreachable, the very window ``immutable`` protects the entry bundle through.
# ``stale-if-error`` is added as a best-effort grant to a caching tunnel/proxy
# in front of the gateway; no mainstream browser honours it, so for a plain
# browser the gateway-down coverage is the ``max-age`` window alone.
#
# A worker chunk is identified by the ``worker`` substring in its filename
# (``diffWorker-``, ``hljsWorker-``, ``subset-worker.chunk-``,
# ``worker-portable-``), matched case-insensitively. That the build emits every
# worker chunk with this substring is asserted against the built dist by
# test_every_built_worker_chunk_carries_the_marker so a worker named without it
# fails the build rather than silently regaining ``immutable``.
_WORKER_ASSET_MARKER = "worker"


# A short fresh lifetime, not ``no-cache``/``must-revalidate``: the browser
# serves the worker from cache for 60s (covering a burst of loads and a brief
# gateway restart without a round-trip), then revalidates and picks up a
# header-only build within a minute. 60s bounds how long a browser can run a
# stale worker CSP after an upgrade — a minute of degraded highlight, not a
# year. ``stale-if-error`` is an intermediary (CDN/proxy) hint that no
# mainstream browser honours; it is kept as a best-effort grant for a caching
# tunnel in front of the gateway and does nothing in a plain browser, so the
# gateway-down guarantee for the browser is the ``max-age`` window alone.
_WORKER_CACHE_CONTROL = "public, max-age=60, stale-if-error=86400"
