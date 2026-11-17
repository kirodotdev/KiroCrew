// Screenshots for ErrorNotice `impact` / `nested` / `actions`: every scene of
// capture/error-notice-impact.tsx in light and dark. The `buttons` scene's
// hover and focus states are forced through DevTools (CSS.forcePseudoState),
// so the frame shows the real :hover and :focus-visible rules. Each shot waits
// for the scene's own text, so a blank frame cannot pass as evidence.
//   node capture/shoot-error-notice-impact.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || 'temp-screenshots/error-notice-impact'
const PORT = Number(process.argv[3] || 5193)
mkdirSync(OUT, { recursive: true })

const SCENES = [
  ['signin-name', 'kiro-cli whoami'],
  ['forecast', 'run-out date'],
  ['balance', 'Could not read your balance.'],
  ['buttons', 'partial: Could not read your balance.'],
  ['effort-inline', 'reasoning effort'],
  ['switch-nested', 'settings file is read-only'],
  ['replaced-blocking', 'previous sign-in could not be removed'],
  ['pending-read', 'outside changes'],
]

const server = await createServer({ configFile: 'vite.config.ts', server: { port: PORT, strictPort: true, host: '127.0.0.1' }, logLevel: 'error' })
await server.listen()
const browser = await chromium.launch({ executablePath: chromiumExecutable(), args: ['--no-sandbox'] })

for (const theme of ['light', 'dark']) {
  for (const [id, phrase] of SCENES) {
    const ctx = await browser.newContext({ viewport: { width: 700, height: 600 }, deviceScaleFactor: 1, reducedMotion: 'reduce' })
    const page = await ctx.newPage()
    await page.goto(`http://127.0.0.1:${PORT}/capture/error-notice-impact.html?theme=${theme}&scene=${id}`)
    await page.getByText(phrase, { exact: false }).first().waitFor({ timeout: 20000 })
    if (id === 'buttons') {
      const cdp = await ctx.newCDPSession(page)
      await cdp.send('DOM.enable')
      await cdp.send('CSS.enable')
      const { root } = await cdp.send('DOM.getDocument', { depth: -1 })
      for (const [state, classes] of [['hover', ['hover']], ['focus', ['focus', 'focus-visible']]]) {
        const { nodeIds } = await cdp.send('DOM.querySelectorAll', { nodeId: root.nodeId, selector: `[data-state="${state}"]` })
        for (const nodeId of nodeIds) await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: classes })
      }
    }
    await page.waitForTimeout(200)
    await page.locator('[data-capture-root]').screenshot({ path: join(OUT, `${id}-${theme}.png`) })
    console.log('wrote', join(OUT, `${id}-${theme}.png`))
    await ctx.close()
  }
}

await browser.close()
await server.close()
