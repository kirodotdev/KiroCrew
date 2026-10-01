// Capture the two native dialog.showMessageBox states this PR reworks, for the
// UX Review blind-read. These are Electron OS-native dialogs (not DOM), so the
// faithful evidence is the exact title + message text the user reads, rendered
// in a platform-message-box frame and photographed headlessly.
//
// The strings below are lifted VERBATIM from website/electron/window-lifecycle.js
// (the reworked "Can't clear remote host from this tab" refusal and the reworked
// Token Refresh dialog's unselectable-port detail), with the ${...} operands
// filled by a realistic default-port scene (:80) so the frame reads like
// production. A guard at the end asserts each rendered string is a substring of
// the live source, so the capture cannot drift from the shipped copy.
//
// Run from website/: node capture/shoot-remote-crew-dialogs.mjs <outdir>
import { chromium } from 'playwright-core'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const outDir = process.argv[2] || '/tmp/remote-crew-dialog-shots'
const executablePath = process.env.CHROMIUM_PATH || undefined

const PORT = 80
const src = readFileSync(new URL('../electron/window-lifecycle.js', import.meta.url), 'utf8')

// --- Dialog 1: the Clear-refusal (title names what failed; no dead-end route) ---
const clearTitle = "Can't clear remote host from this tab"
const clearMessage =
  `This crew was reached on a default port (:${PORT}), so the app can't change or clear its remote-host setting from this tab. The setting is kept on purpose so this window is never mistaken for a local gateway while the connection is open. To stop reaching this crew, close this tab (and stop the "ssh -L :${PORT}" tunnel if you started one). A crew opened on a selectable port (for example "ssh -L 7777:…") can be cleared from its own tab.`

// --- Dialog 2: the Token Refresh dialog, unselectable-port (default) branch ---
const refreshTitle = 'Token Refresh'
const refreshMessage = 'Could not fetch a fresh token.'
const refreshDetail =
  `No token could be fetched for crew.example.test, and this tab's port (${PORT})` +
  " can't be used to save one. Reconnect the crew on a different local" +
  ` port (for example "ssh -L 7777:…" instead of :${PORT}), then open that tab.`

// --- Dialog 2b: Token Refresh, SSH-failed branch ---
const refreshSshDetail =
  'SSH to crew.example.test failed.\n\nssh: connect to host crew.example.test port 22: Connection refused'

// --- Dialog 2c: Token Refresh, re-enter-host branch (selectable port) ---
const refreshReenterDetail =
  "No token could be fetched for crew.example.test, and no SSH attempt was made." +
  " Re-enter the host with 'Set Remote Host…' from the Tab menu so it's" +
  " saved for this tab."

// Drift guard: each rendered detail must be present verbatim in the shipped
// source (matched on a distinctive template-stable fragment; the source splits
// long strings across concatenated lines, and interpolates the port operand).
function assertInSource(label, probe) {
  if (!src.includes(probe)) {
    throw new Error(`DRIFT: ${label} copy not found verbatim in window-lifecycle.js (probe: ${probe})`)
  }
}
assertInSource('clear', "can't change or clear its remote-host setting from this tab")
assertInSource('refresh-unselectable', "can't be used to save one. Reconnect the crew on a different local")
assertInSource('refresh-ssh', 'SSH to ${config?.host || "the remote host"} failed.')
assertInSource('refresh-reenter', "and no SSH attempt was made.")

// A faithful macOS-style modal message box (icon, title, message, detail, one
// default button), on a dimmed dashboard-dark backdrop.
function box({ title, message, detail, button }) {
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return `
    <div class="scrim">
      <div class="dialog" role="alertdialog" aria-modal="true">
        <div class="row">
          <div class="glyph">⚠️</div>
          <div class="body">
            <div class="title">${esc(title)}</div>
            <div class="message">${esc(message)}</div>
            ${detail ? `<div class="detail">${esc(detail)}</div>` : ''}
          </div>
        </div>
        <div class="actions"><button class="default">${esc(button)}</button></div>
      </div>
    </div>`
}

const css = `
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body { width: 720px; font-family: -apple-system, "Segoe UI", system-ui, sans-serif; }
  .scrim {
    width: 720px; min-height: 340px; padding: 40px;
    background: #0f1117; display: flex; align-items: center; justify-content: center;
  }
  .dialog {
    width: 460px; background: #f2f2f4; color: #1b1b1f; border-radius: 12px;
    padding: 20px 22px 16px; box-shadow: 0 24px 60px rgba(0,0,0,.55);
  }
  .row { display: flex; gap: 16px; }
  .glyph { font-size: 40px; line-height: 1; flex: 0 0 auto; }
  .body { flex: 1 1 auto; }
  .title { font-weight: 700; font-size: 15px; margin-bottom: 8px; }
  .message { font-size: 13px; line-height: 1.5; }
  .detail { font-size: 12px; line-height: 1.5; margin-top: 10px; color: #3a3a40; white-space: pre-wrap; }
  .actions { display: flex; justify-content: flex-end; margin-top: 18px; }
  button.default {
    font-size: 13px; padding: 5px 18px; border-radius: 6px; border: none;
    background: #2f6fed; color: #fff; font-weight: 600; cursor: default;
  }`

const shots = [
  {
    name: '01-cant-clear-refusal',
    html: box({ title: clearTitle, message: clearMessage, button: 'OK' }),
  },
  {
    name: '02-token-refresh-unselectable-port',
    html: box({ title: refreshTitle, message: refreshMessage, detail: refreshDetail, button: 'OK' }),
  },
  {
    name: '03-token-refresh-ssh-failed',
    html: box({ title: refreshTitle, message: refreshMessage, detail: refreshSshDetail, button: 'OK' }),
  },
  {
    name: '04-token-refresh-reenter-host',
    html: box({ title: refreshTitle, message: refreshMessage, detail: refreshReenterDetail, button: 'OK' }),
  },
]

const browser = await chromium.launch({ executablePath })
for (const shot of shots) {
  const ctx = await browser.newContext({ viewport: { width: 720, height: 360 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.setContent(`<!doctype html><meta charset="utf-8"><style>${css}</style>${shot.html}`)
  await page.waitForSelector('.dialog')
  const el = await page.$('.scrim')
  await el.screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name)
  await ctx.close()
}
await browser.close()
console.log('done ->', outDir)
