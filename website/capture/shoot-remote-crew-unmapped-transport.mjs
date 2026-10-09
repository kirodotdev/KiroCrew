// Shoot a Remote Crew row with an unmapped connection method beside an SSH row,
// via the remote-crew-unmapped-transport harness, in both themes.
//
// The hint is a native `title` tooltip, which a headless screenshot does not
// paint. So each frame gets a caption strip BELOW the panel, outside it and
// labelled as an annotation, carrying the badge's live `title` text read from
// the DOM. The caption is evidence of what hovering shows, not product UI.
//
// Run from website/: node capture/shoot-remote-crew-unmapped-transport.mjs <outdir>
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync, writeFileSync } from 'node:fs'

const outDir = process.argv[2] || path.join(process.env.TMPDIR || '/tmp', 'remote-crew-unmapped-shots')
mkdirSync(outDir, { recursive: true })
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath })

const report = []
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 900, height: 900 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:5199/capture/remote-crew-unmapped-transport.html?theme=${theme}`)
  await page.waitForSelector('[data-crew-id="u1"]', { timeout: 45000 })
  await page.waitForTimeout(400)

  const read = async (id, label) => {
    const badge = page.locator(`[data-crew-id="${id}"]`).getByText(label, { exact: true })
    return { title: await badge.getAttribute('title'), aria: await badge.getAttribute('aria-label') }
  }
  const unmapped = await read('u1', 'outbound')
  const ssh = await read('m1', 'SSH')
  if (!unmapped.title || unmapped.title !== unmapped.aria) {
    throw new Error(`unmapped badge hint missing or aria mismatch: ${JSON.stringify(unmapped)}`)
  }
  if (ssh.title !== 'Connects over SSH.') throw new Error(`SSH hint changed: ${JSON.stringify(ssh)}`)

  await page.locator('[data-crew-id="u1"]').getByText('outbound', { exact: true }).hover()
  await page.evaluate(({ u, s }) => {
    const root = document.querySelector('[data-capture-root]')
    const cap = document.createElement('div')
    cap.setAttribute('data-capture-annotation', '')
    cap.style.cssText =
      'margin-top:16px;padding:8px 12px;border:1px dashed var(--border);font:12px/1.5 ui-monospace,monospace;color:var(--text-muted)'
    cap.textContent = `Capture annotation (not product UI). Hover title on "outbound": ${u}  |  on "SSH": ${s}`
    root.appendChild(cap)
  }, { u: unmapped.title, s: ssh.title })

  const name = `unmapped-transport-row-${theme}`
  await page.locator('[data-capture-root]').screenshot({ path: path.join(outDir, `${name}.png`) })
  report.push({ theme, unmapped, ssh })
  console.log('shot', name)
  await ctx.close()
}
writeFileSync(path.join(outDir, 'hints.json'), JSON.stringify(report, null, 2))
await browser.close()
await server.close()
