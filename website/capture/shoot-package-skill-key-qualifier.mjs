// Run from website/: node capture/shoot-package-skill-key-qualifier.mjs <outdir>
//
// A vite DEV server, not a built bundle: it compiles only what the capture entry imports, so the
// isolated editor renders even though the full SPA has an unresolvable import in this sandbox.
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'

const outDir = process.argv[2] || path.join(process.env.TMPDIR || '/tmp', 'pskq-shots')
mkdirSync(outDir, { recursive: true })
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5211, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath })

const shots = [
  // The qualifier itself: both colliding copies mapped, each naming its own bundle. Waits on the
  // second disambiguator, so a shot cannot pass with only one copy addressable.
  {
    name: '1-colliding-copies-each-under-its-own-key',
    scene: 'twins',
    ready: 'text=/OMEGA/',
  },
  // The surface with no screenshot at all until now: the config glyph and the remove control.
  {
    name: '2-hand-authored-uris-carry-a-config-glyph-and-a-remove',
    scene: 'unmanaged',
    ready: '[data-testid="agent-skills-unmanaged-region"] button',
  },
  // A mapped key whose installed copy is gone: the warn chip and the count line.
  {
    name: '3-mapped-key-with-no-installed-copy-is-marked',
    scene: 'warn',
    ready: '.bg-warn-subtle',
  },
]

let failed = 0
for (const shot of shots) {
  const ctx = await browser.newContext({
    viewport: { width: 860, height: 620 },
    reducedMotion: 'reduce',
    deviceScaleFactor: 2,
  })
  const page = await ctx.newPage()
  const problems = []
  page.on('pageerror', e => problems.push(String(e)))
  await page.goto(
    `http://127.0.0.1:5211/capture/package-skill-key-qualifier.html?scene=${shot.scene}&theme=dark`,
  )
  try {
    await page.waitForSelector(shot.ready, { timeout: 45000 })
    await page.waitForTimeout(400)
    await page.locator(`[data-testid="scene-${shot.scene}"]`).screenshot({
      path: path.join(outDir, `${shot.name}.png`),
    })
    console.log('shot', shot.name)
  } catch (err) {
    failed += 1
    console.error('MISSED', shot.name, String(err).split('\n')[0])
    if (problems.length) console.error('  page errors:', problems.slice(0, 3).join(' | '))
  }
  await ctx.close()
}
await browser.close()
await server.close()
process.exit(failed ? 1 : 0)
