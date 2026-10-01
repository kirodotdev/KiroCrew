import { test, expect, type Frame, type Page } from '@playwright/test'
import * as fs from 'fs'

/*
 * Incident kc-46d84a — real-Chromium E2E for the same-origin Remote Crew pane relay.
 *
 * Topology (test/e2e/test_instance_pane_relay_e2e.py builds it, passes the URLs here):
 *   - ONE published HTTPS origin (the hub) serves the REAL production-built parent
 *     SPA (a dedicated bundle that mounts the UNCHANGED `InstancesViewport` + relay
 *     authorities — see website/playwright-fixtures/pane-host/) AND the real
 *     `InstancePaneRelay` at `/instance-pane/<capability>/`.
 *   - The relay forwards to a REAL loopback peer gateway (real built SPA bytes,
 *     real `/api/status`, real `/api/ws`) over the manager seam — the only stand-in
 *     for the SSH transport. The peer's raw loopback port is never handed to the
 *     browser.
 *   - The hub issues a SHORT relay lease, so the production renewal timer inside
 *     `InstancesViewport` fires during the test and the pane rotates its lease.
 *
 * The bug was: over an HTTPS tunnel the pane was framed as
 * `http://<published-host>:<loopback-port>` — mixed content the browser blocks,
 * on a port the browser cannot reach — so the pane never loaded and the 15s
 * readiness watchdog fired forever. This asserts the fix's whole contract in a
 * real browser, driven by production frontend code, not a hand-written mirror:
 * grant issuance under the capability path, the opaque sandbox, the `window.name`
 * seed cleared after readiness, the built module graph + `/api/status` + `/api/ws`
 * all THROUGH the relay, the opaque-origin storage shims, `mc-embedded-ready`
 * before the watchdog, the production channel accepting a real upward action and
 * rejecting a stale/wrong/foreign message, storage that persists across a lease
 * rotation, an app API path that stays under the capability prefix, correct hash
 * + SVG-fragment resolution with NO `<base>`, a short-TTL lease that rotates
 * documentPath + channel + iframe while HTTP/WS stay live, the raw port
 * unreachable, and no mixed-content / CSP / auth failure.
 *
 * The parent installs `window.__paneTest` / `__validateForged` as PURE OBSERVERS
 * over production: the grant and readiness are read from the real store, and
 * every message decision is made by the real `resolvePaneMessage`. See
 * playwright-fixtures/pane-host/main.tsx.
 */

const HUB = process.env.PANE_HUB_URL as string
const PEER_PORT = process.env.PANE_PEER_PORT as string
const OUT = process.env.PANE_OUT_DIR as string

// The pane channel field the production frontend uses (pinned so a drift in the
// wire schema fails here). The sandbox keyword set the relay iframe must carry.
const PANE_CHANNEL_FIELD = 'mcPaneChannel'
const RELAY_SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads'
const WATCHDOG_MS = 15_000

/** The relay pane iframe the production InstancesViewport mounted (no test id). */
const PANE_IFRAME = 'iframe[src^="/instance-pane/"]'

test('kc-46d84a: production parent relay pane boots, rotates its lease, raw port unreachable', async ({
  page,
}) => {
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  const relayResponses: { url: string; status: number }[] = []
  const relayFailedRequests: { url: string; failure: string }[] = []
  const wsUrls: string[] = []
  const wsFramesByUrl: Record<string, number> = {}

  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text())
  })
  page.on('pageerror', (e) => pageErrors.push(String(e)))
  page.on('requestfailed', (r) => {
    if (r.url().includes('/instance-pane/')) {
      relayFailedRequests.push({ url: r.url(), failure: r.failure()?.errorText || '' })
    }
  })
  page.on('response', (r) => {
    if (r.url().includes('/instance-pane/')) relayResponses.push({ url: r.url(), status: r.status() })
  })
  page.on('websocket', (ws) => {
    wsUrls.push(ws.url())
    wsFramesByUrl[ws.url()] = 0
    ws.on('framereceived', () => {
      wsFramesByUrl[ws.url()] = (wsFramesByUrl[ws.url()] || 0) + 1
    })
  })

  await page.goto(HUB + '/', { waitUntil: 'domcontentloaded' })

  // The production parent must obtain a same-origin-relay grant (read from the
  // real store by the observer once InstancesViewport's auto-warm connects).
  await page.waitForFunction(() => (window as any).__paneTest?.grant, null, { timeout: 20_000 })
  const grant = await page.evaluate(() => (window as any).__paneTest.grant)

  // ── Grant/endpoint contract: capability path, no raw port or token ──────────
  expect(grant.kind).toBe('same-origin-relay')
  expect(typeof grant.documentPath).toBe('string')
  expect(grant.documentPath).toMatch(/^\/instance-pane\/[A-Za-z0-9_-]+\/$/)
  expect(grant.protocol).toBe(1)
  expect(typeof grant.channel).toBe('string')
  expect(grant.channel.length).toBeGreaterThan(0)
  const grantStr = JSON.stringify(grant)
  expect(grantStr).not.toContain(String(PEER_PORT))
  expect(grantStr).not.toMatch(/\btoken\b/i)
  expect(grantStr).not.toMatch(/https?:\/\//)
  expect(grant.documentPath).not.toContain(':')

  // The capability that rides the FIRST lease, captured before the rotation the
  // short TTL forces later in this test.
  const channel1 = grant.channel as string
  const documentPath1 = grant.documentPath as string

  // ── The runtime helper relocates two representative same-dashboard paths ─────
  // A real-browser cross-check that the PRODUCTION runtime, resolved for the
  // pane's LIVE capability, prefixes a representative app-scoped API path and a
  // full-document route under `/instance-pane/<capability>/…` and never leaves
  // either at the hub root (which would answer for the wrong Crew, or 404). This
  // observer calls the runtime helper directly; it does NOT mount the scoped
  // client or IncidentChat. Those migrated call sites are exercised by their own
  // unit tests: src/test/appScopedApiRelayPane.test.tsx drives a real
  // AppScopedApiProvider fetch and src/apps/ops-mission-control/
  // IncidentChat.paneNavigate.test.tsx drives the real navigation call site —
  // each fails if its relocation is removed.
  const reloc = await page.evaluate(() => (window as any).__paneTest.relocation)
  expect(reloc, 'the production runtime resolved a relocation for the live capability').not.toBeNull()
  expect(reloc.prefix).toBe(documentPath1)
  // App-scoped API request: relocated under the capability prefix, never the root.
  expect(reloc.appApi).toBe(documentPath1 + 'api/apps/aws-control/accounts')
  expect(reloc.appApi.startsWith(documentPath1)).toBeTruthy()
  expect(reloc.appApi).not.toBe('/api/apps/aws-control/accounts')
  // Full-document navigation: relocated under the capability prefix, never the root.
  expect(reloc.nav).toBe(documentPath1 + 'chat')
  expect(reloc.nav.startsWith(documentPath1)).toBeTruthy()
  expect(reloc.nav).not.toBe('/chat')

  // ── The iframe: same-origin capability src, opaque sandbox, no raw port/token ─
  const iframeInfo = await page.evaluate((sel) => {
    const f = document.querySelector(sel) as HTMLIFrameElement | null
    return f ? { src: f.getAttribute('src'), sandbox: f.getAttribute('sandbox') } : null
  }, PANE_IFRAME)
  expect(iframeInfo, 'the production InstancesViewport mounted the relay iframe').not.toBeNull()
  expect(iframeInfo!.src).toBe(documentPath1) // relative, same-origin capability path
  expect(iframeInfo!.src).not.toContain(String(PEER_PORT))
  expect(iframeInfo!.src).not.toMatch(/token/i)
  expect(iframeInfo!.src!.startsWith('/instance-pane/')).toBeTruthy()
  // Sandbox omits allow-same-origin (opaque) and top-navigation.
  expect(iframeInfo!.sandbox).toBe(RELAY_SANDBOX)
  expect(iframeInfo!.sandbox).not.toContain('allow-same-origin')
  expect(iframeInfo!.sandbox).not.toContain('allow-top-navigation')

  // ── Readiness before the 15s watchdog (production store readiness) ──────────
  const readyStart = Date.now()
  await page.waitForFunction(() => (window as any).__paneTest?.ready === true, null, {
    timeout: WATCHDOG_MS,
  })
  expect(Date.now() - readyStart).toBeLessThan(WATCHDOG_MS)

  // ── window.name seed cleared after readiness ────────────────────────────────
  // Production seeds the bootstrap envelope into the iframe `name` BEFORE
  // navigation and empties it once the pane announces `mc-embedded-ready`
  // (name={ready?'':relaySeed}). That re-render is also where the parent records
  // readiness and posts the ack, so an emptied name is the parent's proof it
  // processed the handshake — and proof the channel/snapshot no longer linger in
  // a browser-visible attribute.
  await expect
    .poll(
      () =>
        page.evaluate((sel) => {
          const f = document.querySelector(sel) as HTMLIFrameElement | null
          return f ? f.getAttribute('name') ?? '' : 'no-frame'
        }, PANE_IFRAME),
      { timeout: 5_000 },
    )
    .toBe('')
  const nameAfterReady = await page.evaluate((sel) => {
    const f = document.querySelector(sel) as HTMLIFrameElement | null
    return f ? f.getAttribute('name') ?? '' : 'no-frame'
  }, PANE_IFRAME)
  expect(nameAfterReady).not.toContain(channel1) // the secret is gone

  // ── The pane frame: under the capability prefix, real module graph, rendered ─
  const frame = page.frames().find((f) => f.url().includes('/instance-pane/'))
  expect(frame, 'the relay pane frame exists').toBeTruthy()
  expect(frame!.url().startsWith(HUB + '/instance-pane/')).toBeTruthy()
  expect(frame!.url()).toContain(documentPath1.replace(/\/$/, ''))

  // The built static module graph loaded THROUGH the relay: the entry module and
  // at least one further chunk, all under the capability prefix, all 200.
  const jsThroughRelay = relayResponses.filter(
    (r) => /\/assets\/.*\.js(\?|$)/.test(r.url) && r.status === 200,
  )
  expect(jsThroughRelay.length, 'built JS chunks served through the relay').toBeGreaterThan(1)

  // The remote index + dashboard rendered inside the opaque frame, the storage
  // shims work, and — with NO <base> — relative, hash and SVG-fragment URLs all
  // resolve against the capability-prefixed DOCUMENT url (never the hub root).
  const rendered = await frame!.evaluate((docPath) => {
    const root = document.getElementById('root')
    // A hash anchor and an SVG-fragment reference must resolve against the
    // document URL (under the capability prefix), not a <base> at the root.
    const a = document.createElement('a')
    a.setAttribute('href', '#frag')
    const hashHref = a.href
    const svgFragHref = new URL('#grad', document.baseURI).href
    return {
      title: document.title,
      rootChildren: root ? root.childElementCount : -1,
      bodyLen: document.body?.innerText?.length ?? -1,
      // The relay inserts NO <base> (it would retarget SVG url(#id) fragments and
      // #hash anchors); relocation is via the runtime + relative asset refs.
      baseHref: document.querySelector('base')?.getAttribute('href') || null,
      hasRelayCtx: !!(window as any).__kcRelayPaneContext,
      hashHref,
      svgFragHref,
      baseURI: document.baseURI,
      localStorageWorks: (() => {
        try {
          window.localStorage.setItem('kc46_probe', 'lease1')
          const ok = window.localStorage.getItem('kc46_probe') === 'lease1'
          window.sessionStorage.setItem('kc46_probe', '1')
          const ok2 = window.sessionStorage.getItem('kc46_probe') === '1'
          return ok && ok2
        } catch {
          return false
        }
      })(),
    }
  }, documentPath1)
  // The booted app owns document.title and may prefix an unread-count badge
  // ("(1) Kiro Crew") the instant a notification lands during boot, so match the
  // base title with an optional badge rather than an exact string — either proves
  // the pane's OWN SPA rendered its title (not a stale/blank document).
  expect(rendered.title).toMatch(/^(\(\d+\) )?Kiro Crew$/)
  expect(rendered.rootChildren).toBeGreaterThan(0)
  expect(rendered.bodyLen).toBeGreaterThan(0)
  expect(rendered.hasRelayCtx, 'opaque pane relay context installed').toBe(true)
  expect(rendered.localStorageWorks, 'localStorage/sessionStorage shims usable in opaque child').toBe(
    true,
  )
  // NO <base>: the stale assertion (baseHref === documentPath) is gone — the
  // relay deliberately inserts none, because a <base> would retarget SVG
  // url(#id) fragments and #hash anchors against the base instead of the current
  // document URL. The SPA client-side-routes within its capability (React Router
  // basename = the capability prefix), so the document URL is UNDER the prefix
  // but past the root — and a #hash / SVG fragment must resolve against THAT
  // current URL (proving no <base> is retargeting it to the capability/hub root),
  // which is exactly what keeps hash navigation and SVG fragments correct.
  expect(rendered.baseHref, 'the relay inserts no <base> element').toBeNull()
  expect(
    rendered.baseURI.startsWith(HUB + documentPath1),
    'the pane document is served + routes under the capability prefix',
  ).toBeTruthy()
  expect(
    rendered.hashHref,
    'a #hash anchor resolves against the current document URL (no <base> retarget)',
  ).toBe(rendered.baseURI + '#frag')
  expect(rendered.hashHref.startsWith(HUB + documentPath1)).toBeTruthy()
  expect(rendered.hashHref).not.toBe(HUB + '/#frag') // never escapes to the hub root
  expect(
    rendered.svgFragHref,
    'an SVG url(#id) fragment resolves against the document url, not a <base>',
  ).toBe(rendered.baseURI + '#grad')

  // ── A representative /api/status call completes THROUGH the relay ───────────
  const statusThroughRelay = await page.evaluate(async (docPath) => {
    const r = await fetch(docPath + 'api/status')
    const body = await r.json().catch(() => ({}))
    return { status: r.status, protocol: body.pane_relay_protocol }
  }, documentPath1)
  expect(statusThroughRelay.status).toBe(200)
  expect(statusThroughRelay.protocol).toBe(1)

  // The child's OWN relocated API traffic (its boot /api/status etc.) stays under
  // the capability path — the network proof that an app API is relocated, not
  // escaped to the hub root.
  const apiThroughRelay = relayResponses.filter((r) => /\/instance-pane\/[^/]+\/api\//.test(r.url))
  expect(apiThroughRelay.length, 'the pane SPA\u2019s API calls ride the capability path').toBeGreaterThan(
    0,
  )

  // ── /api/ws opened THROUGH the relay and a frame received ───────────────────
  const paneWs = wsUrls.filter((u) => u.includes('/instance-pane/') && u.includes('/api/ws'))
  expect(paneWs.length, 'the pane opened /api/ws through the relay').toBeGreaterThan(0)
  await expect
    .poll(() => Object.entries(wsFramesByUrl).filter(([u, n]) => u.includes('/api/ws') && n > 0).length, {
      timeout: 10_000,
    })
    .toBeGreaterThan(0)

  // ── Remote→parent action across the exact frame + channel boundary ──────────
  // The real SPA's upward messages (mc-embedded-ready, mc-relay-storage from the
  // storage shim) were accepted ONLY via channel + exact contentWindow, attributed
  // by the production resolvePaneMessage.
  await expect
    .poll(() => page.evaluate(() => (window as any).__paneTest.storageMsgs), { timeout: 10_000 })
    .toBeGreaterThan(0)
  const st = await page.evaluate(() => (window as any).__paneTest)
  expect(st.upwardAccepted, 'at least one attributed remote→parent action').toBeGreaterThan(0)
  expect(st.acceptedTypes).toContain('mc-embedded-ready')
  expect(st.storageMsgs, 'storage-shim mutation crossed to the parent bank').toBeGreaterThan(0)
  expect(st.upwardRejected).toBe(0)
  expect(st.channelField).toBe(PANE_CHANNEL_FIELD)

  // A stale/wrong channel, a channel-less payload, and a foreign frame are rejected
  // by the production channel authority; only the correct channel from the exact
  // frame attributes.
  const forged = await page.evaluate(() => {
    const w = window as any
    return {
      good: w.__validateForged('good'),
      wrongChannel: w.__validateForged('wrong-channel'),
      noChannel: w.__validateForged('no-channel'),
      wrongFrame: w.__validateForged('wrong-frame'),
    }
  })
  expect(forged.good).toBe('kc-46d84a')
  expect(forged.wrongChannel).toBeNull()
  expect(forged.noChannel).toBeNull()
  expect(forged.wrongFrame).toBeNull()

  // ── Short-TTL lease ROTATION: path + channel + iframe rotate, HTTP/WS live ──
  // The hub issues a short lease, so the production renewal timer reissues before
  // the deadline: openInstancePane mints a fresh capability (new documentPath +
  // channel + lease), setWarm sees a changed endpoint (clears readiness), and the
  // iframe key (which embeds the channel) remounts the frame onto the new lease.
  // The old lease stays valid until its own deadline, so there is no gap.
  await page.waitForFunction(
    (ch1) => {
      const t = (window as any).__paneTest
      return t?.grant && t.grant.channel !== ch1 && t.ready === true
    },
    channel1,
    { timeout: 25_000 },
  )
  const grant2 = await page.evaluate(() => (window as any).__paneTest.grant)
  expect(grant2.kind).toBe('same-origin-relay')
  expect(grant2.channel).not.toBe(channel1)
  expect(grant2.documentPath).not.toBe(documentPath1)
  expect(grant2.documentPath).toMatch(/^\/instance-pane\/[A-Za-z0-9_-]+\/$/)

  // The runtime helper cross-check re-runs against the ROTATED capability — a
  // stale prefix cannot linger after renewal.
  const reloc2 = await page.evaluate(() => (window as any).__paneTest.relocation)
  expect(reloc2.prefix).toBe(grant2.documentPath)
  expect(reloc2.appApi).toBe(grant2.documentPath + 'api/apps/aws-control/accounts')
  expect(reloc2.nav).toBe(grant2.documentPath + 'chat')

  // The iframe remounted onto the new capability (src rotated to the new path).
  const iframeSrc2 = await page.evaluate((sel) => {
    const f = document.querySelector(sel) as HTMLIFrameElement | null
    return f ? f.getAttribute('src') : null
  }, PANE_IFRAME)
  expect(iframeSrc2).toBe(grant2.documentPath)

  // HTTP is live on the NEW lease: /api/status through the new capability answers.
  const statusAfterRotate = await page.evaluate(async (docPath) => {
    const r = await fetch(docPath + 'api/status')
    const body = await r.json().catch(() => ({}))
    return { status: r.status, protocol: body.pane_relay_protocol }
  }, grant2.documentPath)
  expect(statusAfterRotate.status).toBe(200)
  expect(statusAfterRotate.protocol).toBe(1)

  // WS is live on the NEW lease: the remounted pane opened /api/ws under the new
  // capability (and its module graph re-loaded through the relay on the new path).
  await expect
    .poll(
      () => wsUrls.filter((u) => u.includes(grant2.documentPath) && u.includes('/api/ws')).length,
      { timeout: 15_000 },
    )
    .toBeGreaterThan(0)
  const frame2 = page.frames().find((f) => f.url().includes(grant2.documentPath.replace(/\/$/, '')))
  expect(frame2, 'the rotated pane frame exists').toBeTruthy()

  // Storage PERSISTED across the rotation: the parent RelayStorageBank kept the
  // value the first pane wrote and re-seeded the remounted child from it.
  const persisted = await frame2!.evaluate(() => {
    try {
      return window.localStorage.getItem('kc46_probe')
    } catch {
      return 'threw'
    }
  })
  expect(persisted, 'the parent storage bank persisted the value across the lease rotation').toBe(
    'lease1',
  )

  // ── The raw remote-pane loopback port is unreachable from Chromium ──────────
  const rawReach = await page.evaluate(async (peerPort) => {
    try {
      await fetch('http://127.0.0.1:' + peerPort + '/api/status', { mode: 'no-cors' })
      return { reached: true }
    } catch {
      return { reached: false }
    }
  }, PEER_PORT)
  expect(rawReach.reached, 'raw peer loopback port must be unreachable from the browser').toBe(false)

  // ── No relay-path request failed; no mixed-content / CSP / auth failure ──────
  // A lease rotation REMOUNTS the iframe, which cancels the old capability's
  // in-flight asset/socket requests — the browser records those as net::ERR_ABORTED
  // (and the hub logs the mirror "connection reset"). That is the expected cost of
  // a deliberate rotation, not a relay fault; every OTHER failure class (refused,
  // TLS, reset without an abort) still fails.
  const realRelayFailures = relayFailedRequests.filter(
    (f) => !/ERR_ABORTED|aborted|cancel/i.test(f.failure),
  )
  expect(realRelayFailures, 'no non-abort failure on the relay path').toEqual([])
  const relayAuthDenials = relayResponses.filter((r) => r.status === 401 || r.status === 403)
  expect(relayAuthDenials, 'no auth denial on the relay path').toEqual([])

  // Two console-error classes are EXPECTED and allowed: the intentional raw-port
  // reachability probe (mixed content / CSP to the KNOWN peer port), and a benign
  // upstream 404/503 (the minimal peer fixture has no model, so /api/models 503s)
  // — neither is a mixed-content / CSP / auth failure.
  const isExpected = (t: string) =>
    t.includes(String(PEER_PORT)) || /Failed to load resource: the server responded/.test(t)
  const unexpectedConsole = consoleErrors.filter((t) => !isExpected(t))
  const mixedOrCspOrAuth = unexpectedConsole.filter((t) =>
    /Mixed Content|Content Security Policy|CORS policy|Access-Control-Allow|401|403|net::ERR_CERT/i.test(
      t,
    ),
  )
  expect(mixedOrCspOrAuth, 'no mixed-content / CSP / auth console failure').toEqual([])

  // The ONLY tolerated pageError is the by-design opaque-sandbox Service Worker
  // SecurityError: the relay omits allow-same-origin to make the pane an opaque
  // origin, which disables the Service Worker API; a library probing for it throws
  // a benign SecurityError. Any OTHER pageError fails.
  const unexpectedPageErrors = pageErrors.filter(
    (t) => !(/serviceWorker/i.test(t) && /sandbox/i.test(t)),
  )
  expect(unexpectedPageErrors, 'no unexpected page error').toEqual([])

  // Evidence artifact.
  fs.mkdirSync(OUT, { recursive: true })
  fs.writeFileSync(
    OUT + '/instance-pane-relay-evidence.json',
    JSON.stringify(
      {
        grant,
        grant2,
        relocation: reloc,
        relocationAfterRotate: reloc2,
        rotated: { channel1, documentPath1, channel2: grant2.channel, documentPath2: grant2.documentPath },
        iframe: iframeInfo,
        nameAfterReady,
        rendered,
        statusThroughRelay,
        statusAfterRotate,
        paneWs,
        wsFramesByUrl,
        persistedAcrossRotation: persisted,
        upward: {
          accepted: st.upwardAccepted,
          rejected: st.upwardRejected,
          types: st.acceptedTypes,
          storageMsgs: st.storageMsgs,
        },
        forged,
        rawReach,
        relayResponses: relayResponses.length,
        apiThroughRelay: apiThroughRelay.length,
        relayFailedRequests,
        consoleErrors,
        pageErrors,
      },
      null,
      2,
    ),
  )
})

test('kc-46d84a: a subsequent-document navigation and a reload re-bootstrap under the same capability', async ({
  page,
}) => {
  // The confirmed second-document defect: after the first pane boot cleared
  // `window.name`, any full-page navigation within the same capability (a link, a
  // hard SPA navigation, or a reload) landed on a document with an empty
  // `window.name` and no envelope — so the opaque-origin storage shims never
  // installed, `window.localStorage` threw `SecurityError`, and the root rendered
  // empty. The fix has such a document derive its capability prefix from its own
  // location and ask the PARENT to reseed (channel + bounded bank snapshot) before
  // the gated app module runs. This drives the real navigation mechanism (not a
  // `location.assign` spy) and asserts the destination actually boots.
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text())
  })
  page.on('pageerror', (e) => pageErrors.push(String(e)))

  await page.goto(HUB + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => (window as any).__paneTest?.grant, null, { timeout: 20_000 })
  const grant = await page.evaluate(() => (window as any).__paneTest.grant)
  const documentPath1 = grant.documentPath as string
  const channel1 = grant.channel as string

  // First document boots (readiness before the watchdog).
  await page.waitForFunction(() => (window as any).__paneTest?.ready === true, null, {
    timeout: WATCHDOG_MS,
  })
  const firstFrame = page.frames().find((f) => f.url().includes(documentPath1.replace(/\/$/, '')))
  expect(firstFrame, 'the first pane document exists').toBeTruthy()

  // Write a value on the FIRST document; the shim reports it to the parent bank,
  // which is what a subsequent document must be re-seeded from.
  await firstFrame!.evaluate(() => {
    window.localStorage.setItem('kc46_nav_probe', 'first-doc')
  })
  await expect
    .poll(() => page.evaluate(() => (window as any).__paneTest.storageMsgs), { timeout: 10_000 })
    .toBeGreaterThan(0)

  const bootState = async (urlPrefix: string) => {
    const f = page.frames().find((fr) => fr.url().startsWith(urlPrefix))
    if (!f) return null
    try {
      return await f.evaluate(() => ({
        rootChildren: document.getElementById('root')?.childElementCount ?? -1,
        hasRelayCtx: !!(window as any).__kcRelayPaneContext,
        name: window.name,
        localStorageWorks: (() => {
          try {
            window.localStorage.setItem('kc46_probe2', 'ok')
            return window.localStorage.getItem('kc46_probe2') === 'ok'
          } catch {
            return false
          }
        })(),
        persisted: (() => {
          try {
            return window.localStorage.getItem('kc46_nav_probe')
          } catch {
            return 'threw'
          }
        })(),
      }))
    } catch {
      return null // frame mid-navigation
    }
  }

  // ── SUBSEQUENT DOCUMENT: a real full-page navigation to a sub-path ──────────
  const chatUrl = HUB + documentPath1 + 'chat'
  await firstFrame!.evaluate((url) => {
    window.location.assign(url)
  }, chatUrl)
  await expect
    .poll(async () => (await bootState(chatUrl))?.rootChildren ?? -1, { timeout: 20_000 })
    .toBeGreaterThan(0)
  const afterNav = await bootState(chatUrl)
  expect(afterNav, 'the /chat document is present').not.toBeNull()
  // The destination booted: opaque-origin context installed, storage usable, and
  // the value the first document wrote was re-seeded from the parent bank.
  expect(afterNav!.hasRelayCtx, 'reseeded relay context on the subsequent document').toBe(true)
  expect(afterNav!.localStorageWorks, 'opaque-origin storage shims usable after navigation').toBe(
    true,
  )
  expect(afterNav!.persisted, 'storage preserved across the navigation via the parent bank').toBe(
    'first-doc',
  )
  // window.name never lingers the secret on a subsequent document.
  expect(afterNav!.name).toBe('')
  expect(afterNav!.name).not.toContain(channel1)

  // ── RELOAD under the existing capability: also re-bootstraps ────────────────
  const chatFrame = page.frames().find((f) => f.url().startsWith(chatUrl))!
  await chatFrame.evaluate(() => {
    window.location.reload()
  })
  await expect
    .poll(async () => (await bootState(chatUrl))?.rootChildren ?? -1, { timeout: 20_000 })
    .toBeGreaterThan(0)
  const afterReload = await bootState(chatUrl)
  expect(afterReload!.hasRelayCtx, 'reseeded relay context after a reload').toBe(true)
  expect(afterReload!.localStorageWorks, 'storage shims usable after a reload').toBe(true)
  expect(afterReload!.persisted, 'storage still preserved after a reload').toBe('first-doc')
  expect(afterReload!.name).toBe('')

  // No mixed-content / CSP / auth failure introduced by the reseed path.
  const mixedOrCspOrAuth = consoleErrors.filter((t) =>
    /Mixed Content|Content Security Policy|CORS policy|Access-Control-Allow|401|403|net::ERR_CERT/i.test(
      t,
    ),
  )
  expect(mixedOrCspOrAuth, 'no mixed-content / CSP / auth failure on the reseed path').toEqual([])
  const unexpectedPageErrors = pageErrors.filter(
    (t) => !(/serviceWorker/i.test(t) && /sandbox/i.test(t)),
  )
  expect(unexpectedPageErrors, 'no unexpected page error on the reseed path').toEqual([])

  fs.mkdirSync(OUT, { recursive: true })
  fs.writeFileSync(
    OUT + '/instance-pane-relay-subsequent-doc-evidence.json',
    JSON.stringify({ documentPath1, chatUrl, afterNav, afterReload }, null, 2),
  )
})

test('kc-46d84a: a bootstrap handshake delayed past the old 2s fallback still boots with the real bank (fail-closed)', async ({
  page,
}) => {
  // The confirmed timeout defect: the bootstrap's 2s fallback removed the reply
  // listener and started the app with an EMPTY channel and empty storage — a
  // ready-but-blank pane that then reload-looped, and whose upward actions the
  // parent dropped. The fix holds the gated module until an AUTHENTICATED port
  // reply and NEVER releases with an empty channel. This delays the parent's port
  // handshake by 3.5s (well past the old 2s window) WITHOUT changing production
  // parent or child code — a fixture capture-phase hook in pane-host/main.tsx
  // holds only the port-carrying request — and asserts the pane still boots with a
  // real channel + persisted storage before the 15s readiness watchdog.
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text())
  })
  page.on('pageerror', (e) => pageErrors.push(String(e)))

  const t0 = Date.now()
  await page.goto(HUB + '/?delayBootstrapMs=3500', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => (window as any).__paneTest?.grant, null, { timeout: 20_000 })
  const grant = await page.evaluate(() => (window as any).__paneTest.grant)
  const documentPath1 = grant.documentPath as string
  const channel1 = grant.channel as string
  expect(typeof channel1).toBe('string')
  expect(channel1.length).toBeGreaterThan(0)

  // Readiness still arrives — after the delay, and before the watchdog.
  await page.waitForFunction(() => (window as any).__paneTest?.ready === true, null, {
    timeout: WATCHDOG_MS,
  })
  const readyMs = Date.now() - t0
  // It took longer than the OLD 2s fallback (proving the delayed handshake, not a
  // 2s empty release, is what booted it) and still beat the 15s watchdog.
  expect(readyMs).toBeGreaterThan(2000)
  expect(readyMs).toBeLessThan(WATCHDOG_MS)

  // The pane booted with a REAL channel + working storage — not the empty-channel,
  // empty-storage fallback that produced the reload loop.
  const frame = page.frames().find((f) => f.url().includes(documentPath1.replace(/\/$/, '')))
  expect(frame, 'the delayed pane document exists').toBeTruthy()
  const booted = await frame!.evaluate(() => {
    const ctx = (window as any).__kcRelayPaneContext
    let storageWorks = false
    try {
      window.localStorage.setItem('kc46_delay_probe', 'delayed')
      storageWorks = window.localStorage.getItem('kc46_delay_probe') === 'delayed'
    } catch {
      storageWorks = false
    }
    return {
      rootChildren: document.getElementById('root')?.childElementCount ?? -1,
      hasRelayCtx: !!ctx,
      channel: ctx?.channel ?? null,
      storageWorks,
    }
  })
  expect(booted.rootChildren, 'the delayed pane rendered its root').toBeGreaterThan(0)
  expect(booted.hasRelayCtx, 'the delayed pane got a real relay context, not an empty fallback').toBe(true)
  expect(booted.channel, 'the delayed pane adopted the authenticated channel').toBe(channel1)
  expect(booted.storageWorks, 'opaque-origin storage shims usable after the delayed handshake').toBe(true)

  // The parent's readiness handshake completed too (upward action attributed),
  // and the storage the delayed child wrote crossed to the parent bank.
  const st = await page.evaluate(() => (window as any).__paneTest)
  expect(st.ready).toBe(true)
  expect(st.acceptedTypes).toContain('mc-embedded-ready')

  const mixedOrCspOrAuth = consoleErrors.filter((t) =>
    /Mixed Content|Content Security Policy|CORS policy|Access-Control-Allow|401|403|net::ERR_CERT/i.test(t),
  )
  expect(mixedOrCspOrAuth, 'no mixed-content / CSP / auth failure on the delayed path').toEqual([])
  const unexpectedPageErrors = pageErrors.filter((t) => !(/serviceWorker/i.test(t) && /sandbox/i.test(t)))
  expect(unexpectedPageErrors, 'no unexpected page error on the delayed path').toEqual([])

  fs.mkdirSync(OUT, { recursive: true })
  fs.writeFileSync(
    OUT + '/instance-pane-relay-delayed-bootstrap-evidence.json',
    JSON.stringify({ grant, readyMs, booted, acceptedTypes: st.acceptedTypes }, null, 2),
  )
})

test('kc-46d84a: an off-capability document replacing the pane in the same iframe gets NO channel and NO host model', async ({
  page,
}) => {
  // The confirmed disclosure defect: the parent broadcast `mc-host-model` (with
  // the live channel) to the iframe's WindowProxy with target '*', so a document
  // that REPLACED the dashboard in the same iframe — surviving WindowProxy, no
  // capability, no bootstrap — inherited the channel and parent control. The fix
  // binds downward delivery to the AUTHENTICATED document's MessagePort; a
  // replacement holds none, so it receives neither the channel nor any host model.
  // This navigates the opaque pane iframe to a neutral SAME-HUB document OUTSIDE
  // /instance-pane/ (topology `/neutral-recorder`, which only records what its
  // parent sends) and asserts it received nothing.
  await page.goto(HUB + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => (window as any).__paneTest?.grant, null, { timeout: 20_000 })
  const grant = await page.evaluate(() => (window as any).__paneTest.grant)
  const documentPath1 = grant.documentPath as string
  const channel1 = grant.channel as string

  // First document boots (so the parent has a live channel + host model to leak).
  await page.waitForFunction(() => (window as any).__paneTest?.ready === true, null, {
    timeout: WATCHDOG_MS,
  })
  const firstFrame = page.frames().find((f) => f.url().includes(documentPath1.replace(/\/$/, '')))
  expect(firstFrame, 'the first pane document exists').toBeTruthy()

  // Navigate the SAME iframe to the neutral off-capability document.
  const recorderUrl = HUB + '/neutral-recorder'
  await firstFrame!.evaluate((url) => {
    window.location.assign(url)
  }, recorderUrl)
  const recorder = await recorderFrame(page, recorderUrl)
  // Let the parent's iframe onLoad + any host-model broadcast fire against the
  // replaced document. On the OLD wildcard path this is exactly when the leak
  // happened; on the fix nothing is delivered.
  await page.waitForTimeout(3000)

  const seen = await recordedByReplacement(page, recorder, channel1)
  // The recorder provably installed and hears the parent, so an empty result
  // below is delivery that never happened. The off-capability document
  // authenticated nothing, so it holds no relay context, received no host model,
  // and the channel never reached it.
  expect(seen.installed, 'the recorder installed under the production CSP').toBe(true)
  expect(seen.controlReceived, 'the recorder provably receives parent-posted messages').toBe(true)
  expect(seen.hasRelayCtx, 'off-capability document has no relay context').toBe(false)
  expect(seen.types, 'off-capability document received no mc-host-model').not.toContain('mc-host-model')
  expect(seen.types, 'off-capability document received no readiness ack').not.toContain('mc-embedded-ack')
  expect(seen.channelReached, 'the pane channel never reached the replacement document').toBe(false)

  fs.mkdirSync(OUT, { recursive: true })
  fs.writeFileSync(
    OUT + '/instance-pane-relay-offcap-evidence.json',
    JSON.stringify({ grant, recorderUrl, seen }, null, 2),
  )
})

/**
 * The off-capability recorder document after the pane iframe navigated to it,
 * with its listener PROVEN installed. The document is served under the parent's
 * production CSP (`script-src 'self'`): an inline recorder is refused by Chromium
 * and leaves `__received` undefined, and a spec that read that as "nothing
 * arrived" would pass against a leak. Requiring `__recorderInstalled` turns a
 * recorder that never installed into a failure instead of a false negative.
 */
async function recorderFrame(page: Page, recorderUrl: string): Promise<Frame> {
  // Polled from Playwright, not from the parent document: the pane iframe is
  // sandboxed without `allow-same-origin`, so the recorder is an opaque origin
  // whose `contentWindow.location` the parent cannot read even on the same hub.
  await expect
    .poll(() => page.frames().some((f) => f.url().startsWith(recorderUrl)), {
      message: 'the off-capability recorder document is present',
      timeout: 15_000,
    })
    .toBe(true)
  const recorder = page.frames().find((f) => f.url().startsWith(recorderUrl))!
  await recorder.waitForFunction(() => (window as any).__recorderInstalled === true, null, {
    timeout: 10_000,
  })
  return recorder
}

/**
 * What the replacement document recorded, read only AFTER a positive control
 * posted from the parent window reached it. Messages from one window to one
 * destination arrive in order, so a refusal or host model the parent had already
 * posted to this frame would precede the control. Requiring the control makes an
 * empty negative result evidence of delivery that never happened, not of a
 * recorder that could not hear.
 */
async function recordedByReplacement(page: Page, recorder: Frame, channel: string) {
  const controlNonce = 'spec-control-' + Math.random().toString(36).slice(2)
  await page.evaluate(
    (nonce) => {
      const frame = document.querySelector<HTMLIFrameElement>('iframe[src^="/instance-pane/"]')
      frame?.contentWindow?.postMessage({ type: 'spec-positive-control', nonce }, '*')
    },
    controlNonce,
  )
  await recorder.waitForFunction(
    (nonce) =>
      ((window as any).__received as { data?: { nonce?: unknown } }[]).some((m) => m.data?.nonce === nonce),
    controlNonce,
    { timeout: 10_000 },
  )
  return recorder.evaluate(
    ({ channel, nonce }) => {
      const list = (window as any).__received as { data: unknown; origin: string }[]
      const types = list
        .map((m) => (m.data && typeof m.data === 'object' ? (m.data as { type?: unknown }).type : undefined))
        .filter((t): t is string => typeof t === 'string')
      return {
        installed: (window as any).__recorderInstalled === true,
        count: list.length,
        types,
        controlReceived: types.includes('spec-positive-control'),
        controlNonce: nonce,
        channelReached: JSON.stringify(list).includes(channel),
        hasRelayCtx: !!(window as any).__kcRelayPaneContext,
      }
    },
    { channel, nonce: controlNonce },
  )
}

test('kc-46d84a: a chained-crew refusal resolving after the pane navigated away never reaches the replacement document', async ({
  page,
}) => {
  // The refusal is the one downward message that is produced ASYNCHRONOUSLY:
  // `adoptChainedCrew` awaits the owner-side add/connect and only then answers the
  // announcing pane. The document that announced can have navigated away by the
  // time that answer exists. A wildcard `contentWindow.postMessage(..., '*')`
  // there would hand the live channel to whatever document now occupies the
  // frame; the fix sends it over the authenticated document port, which the
  // replacement never held. This holds the gateway's answer, swaps the document
  // underneath it, releases the refusal, and asserts the replacement got nothing.
  const refusedLog = page.waitForEvent('console', {
    predicate: (m) => m.text().startsWith('[pane] chain-refused'),
    timeout: 20_000,
  })
  let releaseRefusal: () => void = () => {}
  const held = new Promise<void>((resolve) => {
    releaseRefusal = resolve
  })
  let announcementArrived: () => void = () => {}
  const announced = new Promise<void>((resolve) => {
    announcementArrived = resolve
  })
  // Only the hub's own `/api/instances` POST is held: the pane's relayed reads
  // live under `/instance-pane/<cap>/api/...` and keep flowing.
  await page.route(
    (url) => url.pathname === '/api/instances',
    async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      announcementArrived()
      await held
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'chain too deep', code: 'chain_depth' }),
      })
    },
  )

  await page.goto(HUB + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => (window as any).__paneTest?.ready === true, null, {
    timeout: WATCHDOG_MS + 5_000,
  })
  const grant = await page.evaluate(() => (window as any).__paneTest.grant)
  const channel = grant.channel as string
  const paneFrame = page.frames().find((f) => f.url().includes('/instance-pane/'))
  expect(paneFrame, 'the authenticated pane document exists').toBeTruthy()

  // The authenticated document announces a crew it connected, exactly as the
  // production embedded child does: channel-bound, to the parent's exact origin.
  await paneFrame!.evaluate((channelField) => {
    const ctx = (window as any).__kcRelayPaneContext as { channel: string; parentOrigin: string }
    window.parent.postMessage(
      {
        type: 'mc-instance-ready',
        v: 1,
        id: 'spec-chained-child',
        name: 'Spec chained child',
        sshHost: 'unused.example',
        remotePort: 7777,
        port: 12345,
        [channelField]: ctx.channel,
      },
      ctx.parentOrigin,
    )
  }, PANE_CHANNEL_FIELD)
  await announced

  // Replace the announcing document while the gateway's answer is still held.
  const recorderUrl = HUB + '/neutral-recorder'
  await paneFrame!.evaluate((url) => {
    window.location.assign(url)
  }, recorderUrl)
  const recorder = await recorderFrame(page, recorderUrl)

  // Release the refusal and wait until the production parent has actually
  // produced it, so the negative below is read AFTER the message existed.
  releaseRefusal()
  const refused = await refusedLog
  expect(refused.text(), 'the parent produced the refusal').toContain('chain-refused')

  const seen = await recordedByReplacement(page, recorder, channel)
  expect(seen.installed, 'the recorder installed under the production CSP').toBe(true)
  expect(seen.controlReceived, 'the recorder provably receives parent-posted messages').toBe(true)
  expect(seen.hasRelayCtx, 'the replacement document authenticated nothing').toBe(false)
  expect(seen.types, 'the refusal never reached the replacement document').not.toContain('mc-instance-refused')
  expect(seen.types, 'no host model reached the replacement document').not.toContain('mc-host-model')
  expect(seen.channelReached, 'the live channel never reached the replacement document').toBe(false)

  fs.mkdirSync(OUT, { recursive: true })
  fs.writeFileSync(
    OUT + '/instance-pane-relay-refusal-evidence.json',
    JSON.stringify({ grant, recorderUrl, refused: refused.text(), seen }, null, 2),
  )
})
