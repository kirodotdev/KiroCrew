/**
 * Screenshot harness for the WHATSAPP CHANNEL settings panel.
 *
 * The panel is the only user-visible surface of the WhatsApp channel, and its
 * job is almost entirely to communicate STATE: whether the linked device is
 * paired, what the QR step expects the operator to do, and who is allowed to
 * command the agent. So the interesting variants are lifecycle states rather
 * than layouts, and each one is photographed against the real built SPA
 * (website/dist) with the dashboard API stubbed.
 *
 * The `state` field drives the badge and which controls render, so the six
 * captured here are the ones an operator actually passes through:
 *
 *   unpaired, the first-run view, and what the panel says when no code exists
 *   pairing    — a QR code is live and the phone has not scanned it yet
 *   connected  — paired and receiving, with the access-policy controls
 *   down: paired and NOT carrying traffic, with the gateway's reason
 *   revoked, the phone unlinked the device, so a restart is what re-pairs it
 *   allowlist  — connected, with dm_policy=allowlist so the number list shows
 *
 * `down` and `revoked` exist because `configured` cannot express them: the
 * gateway computes it as `enabled AND connected`, so a panel reading it renders
 * "connected" over a channel that is carrying nothing. These two are the shots
 * that prove the badge and the connection reason come off `state` instead.
 *
 * Nothing in CI runs this file; it exists so the PR's evidence is the same
 * evidence used to verify the change. Both themes are captured because the
 * status badge and the QR card carry their own surfaces.
 *
 * Usage: node scripts/capture-whatsapp-panel.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { QR_PNG, BASE_CONFIG } from './lib/whatsapp-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/whatsapp-panel'

const SCENARIOS = [
  {
    name: 'unpaired',
    config: { ...BASE_CONFIG, state: 'unpaired', configured: false, enabled: false },
    qr: { state: 'unpaired', qr_data_url: null, detail: '' },
  },
  {
    name: 'pairing',
    // configured:false matches connected:false here: nothing is paired yet.
    config: { ...BASE_CONFIG, state: 'pairing', configured: false },
    qr: { state: 'pairing', qr_data_url: QR_PNG, detail: '' },
    clickPair: true,
  },
  {
    name: 'connected',
    config: { ...BASE_CONFIG, state: 'connected', connected: true },
    qr: { state: 'connected', qr_data_url: null, detail: '' },
  },
  {
    // Paired, and not carrying traffic. `configured` stays true to make the point
    // that it is not what the badge may read.
    name: 'down',
    config: {
      ...BASE_CONFIG,
      state: 'error',
      connected: false,
      connect_error: 'error: disconnected (auto-reconnecting)',
    },
    qr: { state: 'error', qr_data_url: null, detail: '' },
  },
  {
    name: 'revoked',
    config: {
      ...BASE_CONFIG,
      state: 'logged_out',
      connected: false,
      connect_error: 'logged_out: device unlinked — re-pair from Settings',
    },
    qr: { state: 'logged_out', qr_data_url: null, detail: '' },
  },
  {
    name: 'allowlist',
    config: {
      ...BASE_CONFIG,
      state: 'connected',
      connected: true,
      dm_policy: 'allowlist',
      allowed_wa_ids: ['447700900111', '447700900222'],
      groups: [{ jid: '120363000000000001@g.us', name: 'Weekend plans', mode: 'mention', rules: '', cooldown_s: 120 }],
    },
    qr: { state: 'connected', qr_data_url: null, detail: '' },
  },
]

function routes(scenario) {
  return async (path, route) => {
    if (path === '/api/whatsapp/config') return json(route, scenario.config), true
    if (path === '/api/channels/whatsapp/qr/status') return json(route, scenario.qr), true
    if (path === '/api/whatsapp/groups') {
      return json(route, { groups: [{ jid: '120363000000000001@g.us', name: 'Weekend plans' }] }), true
    }
    // The channel accordion hides EVERY channel's settings behind the effective
    // `channels` governance policy, and a failed read is treated as "cannot
    // confirm" rather than as permitted. Without this the pane renders the
    // policy-unavailable placeholder and the screenshot shows no panel at all.
    if (path === '/api/governance/channels') {
      return json(route, { whatsapp: true }), true
    }
    // The panel probes this POST before it will render the QR card at all (see
    // the phase note below). It REPORTS the live client's state rather than
    // starting anything, and the panel abandons the wait on any state but
    // `pairing`, so the stub has to answer with the scenario's own state.
    if (path === '/api/channels/whatsapp/qr/start') {
      return json(route, { ok: true, state: scenario.qr.state }), true
    }
    return false
  }
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['light', 'dark']) {
      for (const scenario of SCENARIOS) {
        const context = await browser.newContext({
          viewport: { width: 1400, height: 1000 },
          deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
        })
        const page = await context.newPage()
        logPageProblems(page)
        await stubDashboardApi(page, { theme, extra: routes(scenario) })
        // Selection is URL-backed (?channel=<key>), so deep-link straight to the
        // panel rather than clicking a row: the list renders role="option" in a
        // listbox, and on a narrow viewport the list and the detail pane swap.
        await page.goto(base + '/settings?tab=channels&channel=whatsapp', {
          waitUntil: 'domcontentloaded',
        })
        await page.getByText(/WhatsApp/).first().waitFor({ state: 'visible', timeout: 20000 })
        await page.waitForTimeout(700) // query settle

        // The QR card is gated on the panel's OWN `phase`, which starts at
        // 'idle' and only advances when the operator asks for the code. Polling
        // the status endpoint does not move it, so without this click the
        // "pairing" shot is indistinguishable from the connected one and shows no
        // code. The control is also disabled unless the gateway reports a live
        // code, which is why only the `pairing` scenario sets `clickPair`.
        if (scenario.clickPair) {
          const pair = page.getByRole('button', { name: /Show pairing code/ }).first()
          await pair.waitFor({ state: 'visible', timeout: 15000 })
          await pair.click()
          await page.waitForTimeout(900) // qr/start -> phase 'waiting' -> render
        }

        const file = `${OUT}/whatsapp-${scenario.name}-${theme}.png`
        await page.screenshot({ path: file, fullPage: true })
        console.log('wrote', file)
        await context.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
