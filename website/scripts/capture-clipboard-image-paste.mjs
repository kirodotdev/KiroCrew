/**
 * Screenshot harness for CLIPBOARD IMAGE PASTE into the chat composer
 * (issue #2489).
 *
 * Two scenarios against the REAL built SPA (website/dist), gateway-free:
 *
 *  1. paste-attaches.png — a synthetic ClipboardEvent carrying an image file
 *     (the OS-screenshot clipboard shape: Files only, no text/plain) is
 *     dispatched on the composer textarea. The paste handler funnels the file
 *     into the picker's upload path; the stubbed /api/upload/file answers
 *     with a stored path and the attachment chip appears above the input,
 *     labeled with the synthesized `pasted-image-<timestamp>.png` name.
 *
 *  2. paste-oversize-error.png — the same paste with a >50 MB image blob.
 *     ChatPage.uploadFiles rejects it client-side with the same
 *     file_too_large banner a picked file gets; no upload request is made.
 *
 * Both scenarios ASSERT as well as photograph: scenario 1 exits non-zero if
 * no chip labeled pasted-image-* renders, scenario 2 if the error banner is
 * missing or the stub saw an upload request.
 *
 * Usage: node scripts/capture-clipboard-image-paste.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chat-clipboard-image-paste'
const SLOT = 'chat-paste'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Clipboard paste demo',
  running: false,
  last_message: 'Sure — paste the screenshot here.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: '',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 90, content: 'Can I show you what the dialog looks like?' },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: 'Sure — paste the screenshot here.' },
  ],
}

/** Dispatch a native ClipboardEvent('paste') carrying one image File on the
 *  composer textarea. Runs in the page: DataTransfer + File are real browser
 *  objects there, so the React onPaste handler sees exactly what a user
 *  pressing Cmd/Ctrl+V with a screenshot on the clipboard produces. */
async function pasteImage(page, { name, bytes }) {
  await page.evaluate(async ({ name, bytes }) => {
    const ta = document.querySelector('textarea[data-composer-input]')
    if (!ta) throw new Error('composer textarea not found')
    ta.focus()
    const data = new Uint8Array(bytes)
    const file = new File([data], name, { type: 'image/png' })
    const dt = new DataTransfer()
    dt.items.add(file)
    const ev = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt })
    ta.dispatchEvent(ev)
  }, { name, bytes })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 } })
  const page = await context.newPage()
  logPageProblems(page)

  let uploadRequests = 0
  // A real 48x32 PNG (accent-tinted) so the chip thumbnail renders as an
  // image instead of a broken-image placeholder in the evidence shots.
  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAADAAAAAgCAIAAAAt/+nTAAAAKklEQVRYw+3NMQEAIAwDsIF/z+MMHJXQZOnZ3GmVSCQSiUQikUgkEon0Iw+dcgKY0eBqRAAAAABJRU5ErkJggg==',
    'base64',
  )
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path === '/api/upload/file') {
        uploadRequests += 1
        await json(route, { paths: ['/home/user/.kiro/crew/uploads/abc123_pasted-image-20260810-091500123.png'] })
        return true
      }
      if (path === '/api/file-raw') {
        await route.fulfill({ status: 200, contentType: 'image/png', body: png })
        return true
      }
      if (path.startsWith('/api/chat/slot/')) {
        await json(route, detail)
        return true
      }
      return false
    },
  })

  await page.goto(`${base}/chat/${SLOT}`)
  await page.waitForSelector('textarea[data-composer-input]')
  await page.waitForTimeout(500)

  // ── Scenario 1: normal screenshot paste → attachment chip ────────────────
  await pasteImage(page, { name: 'image.png', bytes: [0x89, 0x50, 0x4e, 0x47] })
  // The chip is an <img alt={storedPath}> thumbnail in the preview strip; the
  // stored path carries the synthesized pasted-image name through the stub.
  await page.waitForSelector('img[alt*="pasted-image"]', { timeout: 5000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/paste-attaches.png` })
  if (uploadRequests !== 1) throw new Error(`expected 1 upload request, saw ${uploadRequests}`)
  console.log('scenario 1 OK: pasted image attached via the upload path')

  // ── Scenario 2: oversize pasted image → same error as the picker ─────────
  await page.reload()
  await page.waitForSelector('textarea[data-composer-input]')
  await page.waitForTimeout(500)
  const before = uploadRequests
  // 51 MB of zeros — over the 50 MB client-side cap.
  await page.evaluate(async () => {
    const ta = document.querySelector('textarea[data-composer-input]')
    ta.focus()
    const blob = new Blob([new ArrayBuffer(51 * 1024 * 1024)], { type: 'image/png' })
    const file = new File([blob], 'huge-screenshot.png', { type: 'image/png' })
    const dt = new DataTransfer()
    dt.items.add(file)
    ta.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt }))
  })
  await page.waitForSelector('text=/too large/i', { timeout: 5000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/paste-oversize-error.png` })
  if (uploadRequests !== before) throw new Error('oversize paste must never reach the server')
  console.log('scenario 2 OK: oversize pasted image rejected with the picker error')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
