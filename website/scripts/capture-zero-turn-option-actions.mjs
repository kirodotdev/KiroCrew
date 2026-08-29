/**
 * Screenshot harness for the ZERO-TURN OPTION ACTION chip.
 *
 * Photographs the surface this change adds to the band above the composer: the
 * danger-painted close chip `[OPTION-ACTIONS: close=<label>]` renders, in the three
 * states a reviewer needs to judge it —
 *
 *   1. action-only     — a row offering ONLY the close chip (the zero-turn case).
 *   2. beside options  — content chips and the action chip together, showing the
 *                        divider and the separate container that keep "sends text"
 *                        visually apart from "tears the tab down".
 *   3. blocked         — the destructive gate: composer holds unsent work, so the chip
 *                        is disabled and the REASON renders visibly rather than in a
 *                        title attribute a keyboard or touch user never sees.
 *
 * WHY NOT `serve-dist` LIKE ITS SIBLINGS. Every other `capture-*.mjs` here serves the
 * real built SPA (`website/dist`). That path is unavailable in this checkout: three
 * declared deps in the private `@pierre` scope (`@pierre/diffs`, `@pierre/trees`,
 * `@pierre/theming`) are absent from `node_modules`, and `PierreImpl.tsx` pulls a web
 * worker out of `@pierre/diffs`, so `vite build` dies at `[UNRESOLVED_ENTRY] Cannot
 * resolve entry module src/pierre/@pierre/diffs/worker/worker-portable.js` before any
 * bundle exists. Installing a private scope into a shared `node_modules` is not a
 * side effect a screenshot harness should have.
 *
 * So this mounts the REAL `FollowUpBar` — the shipped component from this branch, with
 * the shipped stylesheet and theme tokens — through a vite DEV server, which transforms
 * only the modules actually imported and therefore never reaches `pierre`. What is real:
 * the component, its CSS, the chip geometry, the danger paint, the divider, the block
 * reason and its copy. What is NOT: the surrounding app shell (nav, transcript,
 * composer), because nothing in the reachable graph renders it. Judge the chip row from
 * these; the row's placement relative to the composer is covered by its sibling harness
 * `capture-above-composer-order.mjs` and by the DOM-order test in ChatInput.test.tsx.
 *
 * Usage: node scripts/capture-zero-turn-option-actions.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, writeFileSync, copyFileSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const WEB = path.resolve(HERE, '..')
const OUT = process.argv[2] || path.resolve(WEB, '../temp-screenshots/zero-turn-option-actions')
// Under the vite root so the dev server will serve and transform it. Removed on exit.
const STAGE = path.join(WEB, '.tmp-capture-option-actions')

// The label must NOT say "tab": the action closes the SESSION, and context.py rejects
// that wording -- a harness demonstrating it would ship a screenshot of the rejected copy.
const CLOSE = { action: 'close', label: "Nothing else, I'm done here" }
const OPTIONS = ['Show me the diff', 'Run the tests first']

/**
 * Six <FollowUpBar> mounts, each in its own labelled panel so one image carries the
 * whole story. `composerHasUnsentWork` is the only difference between 2 and 3 — the prop
 * this change threads through, so the pair IS the before/after of the destructive gate.
 *
 * Panels 3-6 then walk EVERY reason the gate can state, because a reason
 * nobody has seen rendered is a reason nobody can judge.
 */
const ENTRY = `
import React from 'react'
import { createRoot } from 'react-dom/client'
import '../src/index.css'
// MUST run before the first render, same reason main.tsx gives: a component that
// renders ahead of init resolves its keys to empty strings. Without this the block
// reason in panel 3 photographed BLANK -- the node was there, its text was not.
import { initI18n } from '../src/i18n/all'
import FollowUpBar from '../src/components/FollowUpBar'

const CLOSE = ${JSON.stringify(CLOSE)}
const OPTIONS = ${JSON.stringify(OPTIONS)}
const noop = () => {}

function Panel({ label, children }) {
  return (
    <div style={{ marginBottom: 34 }}>
      <div style={{
        font: '600 11px/1.4 ui-sans-serif, system-ui', letterSpacing: '.08em',
        textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 10,
      }}>{label}</div>
      {children}
    </div>
  )
}

async function start() {
  await initI18n()
  createRoot(document.getElementById('root')).render(
    <div style={{ padding: 28, background: 'var(--bg)' }}>
      <Panel label="1 — action only: the zero-turn close chip">
        <FollowUpBar options={[]} picked={new Set()} onSelect={noop}
          action={CLOSE} onAction={noop} />
      </Panel>
      <Panel label="2 — beside content options: divider keeps them apart">
        <FollowUpBar options={OPTIONS} picked={new Set()} onSelect={noop} onSend={noop}
          action={CLOSE} onAction={noop} />
      </Panel>
      <Panel label="3 — blocked (unsent work HERE): the reason is visible">
        <FollowUpBar options={OPTIONS} picked={new Set()} onSelect={noop} onSend={noop}
          action={CLOSE} onAction={noop} composerHasUnsentWork />
      </Panel>
      <Panel label="4 — blocked (a pick is staged): send it first">
        <FollowUpBar options={OPTIONS} picked={new Set([OPTIONS[0]])} onSelect={noop}
          onSend={noop} action={CLOSE} onAction={noop} />
      </Panel>
      <Panel label="5 — blocked (capture in flight): the recording would be lost">
        <FollowUpBar options={OPTIONS} picked={new Set()} onSelect={noop} onSend={noop}
          action={CLOSE} onAction={noop} composerCaptureInFlight />
      </Panel>
      <Panel label="6 — blocked (unsent work in ANOTHER window): named, not silent">
        <FollowUpBar options={OPTIONS} picked={new Set()} onSelect={noop} onSend={noop}
          action={CLOSE} onAction={noop} actionBlockedBySlot />
      </Panel>
    </div>,
  )
}

start()
`

const HOST_HTML = path.join(HERE, 'fixtures', 'option-actions-capture-host.html')

/**
 * A browser, by `launch()` where that works and by a loopback CDP attach where it does not.
 *
 * Some hosts fail Playwright's `--remote-debugging-pipe` handshake while the browser binary
 * itself is healthy (it renders `--dump-dom` and reports `--version`), which surfaces only as
 * "Target page, context or browser has been closed" with empty browser logs. Attaching over a
 * port is the same browser on a different transport, so the captures are unaffected.
 */
async function openBrowser() {
  try {
    return { browser: await chromium.launch(), child: null }
  } catch (launchErr) {
    const { spawn } = await import('node:child_process')
    const { mkdtempSync } = await import('node:fs')
    const { tmpdir } = await import('node:os')
    const port = 9300 + Math.floor(Math.random() * 400)
    const child = spawn(chromium.executablePath(), [
      '--headless', '--no-sandbox', '--disable-gpu',
      `--remote-debugging-port=${port}`,
      // Loopback only: this is a debugging surface with no auth of its own.
      '--remote-debugging-address=127.0.0.1',
      `--user-data-dir=${mkdtempSync(path.join(tmpdir(), 'zto-capture-'))}`,
      'about:blank',
      // Node's own bundled libstdc++ shadows the system one through LD_LIBRARY_PATH, and it is
      // missing a CXXABI version that libLLVM needs, so the browser dies before it can listen.
    ], { stdio: 'ignore', env: { ...process.env, LD_LIBRARY_PATH: '' } })
    for (let attempt = 0; attempt < 40; attempt++) {
      try {
        const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`)
        console.log(`(launch() failed, attached over CDP on ${port} instead)`)
        return { browser, child }
      } catch {
        await new Promise(resolve => setTimeout(resolve, 250))
      }
    }
    child.kill()
    throw launchErr
  }
}

async function main() {
  mkdirSync(STAGE, { recursive: true })
  mkdirSync(OUT, { recursive: true })
  writeFileSync(path.join(STAGE, 'entry.tsx'), ENTRY)
  // Copied rather than written from a literal: see the comment in the host page.
  copyFileSync(HOST_HTML, path.join(STAGE, 'index.html'))

  const server = await createServer({
    root: WEB,
    logLevel: 'warn',
    server: { host: '127.0.0.1', port: 0 },
  })
  await server.listen()
  // `port: 0` above asks the OS for a free port, so the listener is the authoritative
  // source for the one actually assigned. Do not read it back from
  // `config.server.port`: that is resolved config, not the bound socket, and a `??`
  // against it cannot fall back (0 is not nullish) if it ever does carry the 0.
  const port = server.httpServer.address().port
  const url = `http://127.0.0.1:${port}/${path.basename(STAGE)}/index.html`

  const { browser, child } = await openBrowser()
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({
        viewport: { width: 900, height: 350 },
        // 11–12px chip type renders soft on GitHub at 1x, same reason as the siblings.
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await context.newPage()
      const problems = []
      page.on('pageerror', e => problems.push(String(e)))
      page.on('console', m => { if (m.type() === 'error') problems.push(m.text()) })
      await page.goto(url, { waitUntil: 'networkidle' })
      // The chips play a staggered entrance; wait past it so the still is at rest.
      await page.waitForSelector('button', { timeout: 15000 })
      await page.waitForTimeout(1200)
      // `data-theme` on <html>, which is what index.css keys its variable blocks on.
      // A `.light` CLASS does nothing: the first run of this harness produced a
      // "light" PNG that was byte-identical to the dark one, mislabelling the file.
      await page.evaluate(t => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(400)
      const file = path.join(OUT, `chip-states-${theme}.png`)
      await page.screenshot({ path: file, fullPage: true })
      const buttons = await page.locator('button').count()
      // The blocked panel's whole point is that the reason is VISIBLE, so assert its
      // text is non-empty. It photographed blank once: i18n was not initialised, the
      // node rendered with an empty string, and only the caption claimed otherwise.
      const reason = (await page.locator('[role="note"] [aria-hidden="true"]').first().innerText()).trim()
      // Proof the theme actually took, so a mislabelled capture cannot ship again.
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
      console.log(`${theme}: ${file}  buttons=${buttons}  bg=${bg}  reason=${JSON.stringify(reason)}  problems=${problems.length}`)
      for (const p of problems.slice(0, 5)) console.log(`   ! ${p}`)
      // A render that produced no buttons photographed nothing worth reviewing.
      if (buttons < 4) throw new Error(`${theme}: expected >=4 chips, saw ${buttons}`)
      if (!reason) throw new Error(`${theme}: blocked panel rendered an EMPTY reason`)
      await context.close()
    }
  } finally {
    await browser.close()
    if (child) child.kill()
    await server.close()
    rmSync(STAGE, { recursive: true, force: true })
  }
}

main().catch(e => { console.error(e); process.exit(1) })
