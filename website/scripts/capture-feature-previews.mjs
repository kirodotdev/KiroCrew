/**
 * Regenerate the Feature Previews "See what it looks like" media set —
 * `public/app-assets/feature-previews/` — from a RUNNING pod, so the pictures
 * the dialog shows stay pictures of the real surfaces rather than frozen bytes.
 *
 * Why this exists: `FeaturePreviewIntroDialog.tsx` promises "what you see is
 * what will appear". A preview surface is, by definition, the part of the app
 * that changes fastest, so the captures drift. This script is the honest way to
 * re-shoot them; a hand-made mockup is not (see the component comment).
 *
 * Every capture is REAL: each preview flag is turned on in the pod's
 * localStorage, the theme is set through the pod's own `PUT /api/config/theme`,
 * and the page is driven with Playwright. Every capture today is a PNG still;
 * the dialog also renders GIFs (`kind: 'gif'`), and the last one — the create
 * menu opening on "New Crew Mode chat" — went with Crew Mode. Recording one
 * again means `recordVideo` + a palette-quantised ffmpeg pass (12 fps, 800 px
 * wide), the recipe the browser-recording skill documents.
 *
 * Usage (from website/, with the pod up and this branch's dist provisioned):
 *
 *   kirocrew pod up <worktree> --seed rich --json | tail -1 > /tmp/pod.json
 *   POD_INFO=/tmp/pod.json node scripts/capture-feature-previews.mjs [outDir]
 *
 * `outDir` defaults to `public/app-assets/feature-previews`. Budget the PR
 * agreed to: PNG <= 200 KB — the script fails loudly past it.
 * Adding a preview: add a `shoot*` step below AND a builder in
 * `pages/settings/FeaturePreviewsSection.tsx`; a preview with no honest capture
 * gets no builder and so no button.
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const info = JSON.parse(fs.readFileSync(process.env.POD_INFO, 'utf8').trim().split('\n').pop())
const base = info.base_url
const token = info.token
const out = path.resolve(process.argv[2] || 'public/app-assets/feature-previews')
fs.mkdirSync(out, { recursive: true })

const PNG_MAX = 200 * 1024
const VIEW = { width: 1200, height: 760 }

async function settle(page) {
  await page.waitForURL(u => !String(u).includes('token='), { timeout: 20_000 }).catch(() => {})
  await page.waitForTimeout(1200)
}

/** Fresh context: sign in with the pod token, set the theme server-side (the
 *  boot fetch overrides localStorage otherwise), turn the given flags on. */
async function session(browser, theme, flags) {
  const ctx = await browser.newContext({ viewport: VIEW })
  const page = await ctx.newPage()
  await page.goto(`${base}/?token=${token}`, { waitUntil: 'load' })
  await settle(page)
  const status = await page.evaluate(async (mode) => {
    const r = await fetch('/api/config/theme', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }),
    })
    return r.status
  }, theme)
  if (status !== 200) throw new Error(`theme PUT ${status}`)
  await page.evaluate(({ theme, flags }) => {
    localStorage.setItem('mc-theme', theme)
    for (const f of flags) localStorage.setItem(f, '1')
  }, { theme, flags })
  return { ctx, page }
}

function check(file, max) {
  const size = fs.statSync(file).size
  if (size > max) throw new Error(`${path.basename(file)} is ${size} bytes, over the ${max} budget`)
  console.log(`${path.basename(file)}  ${size} B`)
}

async function shoot(page, file) {
  await page.screenshot({ path: file })
  check(file, PNG_MAX)
}

const browser = await chromium.launch()

for (const theme of ['light', 'dark']) {
  // Webhooks: the page is the flag's only door.
  {
    const { ctx, page } = await session(browser, theme, ['mc-preview-webhooks'])
    await page.goto(`${base}/webhooks`, { waitUntil: 'load' })
    await settle(page)
    await shoot(page, path.join(out, `webhooks-page-${theme}.png`))
    await ctx.close()
  }
  // Crew Members: the page is the flag's only door (Crew Mode, the second
  // door this flag used to open, retired — see `utils/previewFlags.ts`).
  {
    const { ctx, page } = await session(browser, theme, ['mc-preview-crew'])
    await page.goto(`${base}/members`, { waitUntil: 'load' })
    await settle(page)
    await shoot(page, path.join(out, `crew-members-${theme}.png`))
    await ctx.close()
  }
}

await browser.close()
console.log(`media set written to ${out}`)
