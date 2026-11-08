/**
 * Screenshots of the clear-context states this change adds (#11157), via the
 * capture/channel-clear-context harness. Each shot ASSERTS the pictured state
 * rendered before shooting, so a frame cannot show the pre-change surface.
 *
 * Usage: node scripts/capture-channel-clear-context.mjs <viteBase> <outDir>
 *
 * Preload the system libstdc++ where a version-manager Node shadows it, or
 * Chromium children die on GLIBCXX_3.4.29:
 *   LD_PRELOAD=/lib64/libstdc++.so.6 node scripts/capture-...mjs
 *
 * Playwright DISMISSES native dialogs, so the per-agent clear's confirm() must
 * be accepted or its request is never sent. Its isDisabled() counts
 * aria-disabled as disabled, so the busy shot reads the DOM property instead.
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/channel-clear-context'

const CONFIRM_BUTTON = 'Clear context and delete messages'

const b = await chromium.launch()

async function open(scene, theme) {
  const p = await (await b.newContext({ viewport: { width: 1100, height: 560 }, deviceScaleFactor: 2 })).newPage()
  await p.goto(`${base}/capture/channel-clear-context.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
  await p.getByTitle('Clear all context').waitFor({ timeout: 20_000 })
  return p
}

async function shoot(p, name) {
  const file = `${out}/${name}.png`
  await p.locator('[data-capture-root]').screenshot({ path: file })
  console.log(`captured ${file}`)
}

for (const theme of ['dark', 'light']) {
  {
    const p = await open('confirm', theme)
    await p.getByTitle('Clear all context').click()
    await p.getByRole('button', { name: CONFIRM_BUTTON }).waitFor({ timeout: 20_000 })
    await shoot(p, `confirm-${theme}`)
    await p.close()
  }

  {
    const p = await open('confirm', theme)
    await p.getByTitle('Clear all context').click()
    await p.getByRole('button', { name: CONFIRM_BUTTON }).click()
    await p.locator('[data-testid="clear-context-done"]').waitFor({ timeout: 20_000 })
    await shoot(p, `done-line-${theme}`)
    await p.close()
  }

  {
    const p = await open('confirm', theme)
    p.on('dialog', d => d.accept())
    await p.getByRole('button', { name: /^\d+ agents?$/ }).click()
    await p.getByTitle('Clear context').first().click()
    await p.locator('[data-testid="agent-clear-done"]').waitFor({ timeout: 20_000 })
    const marks = await p.locator('[data-testid="agent-clear-done"]').count()
    if (marks !== 1) throw new Error(`the mark must belong to ONE row; found ${marks}`)
    await shoot(p, `row-mark-${theme}`)
    await p.close()
  }

  {
    const p = await open('busy', theme)
    await p.getByTitle('Clear all context').click()
    await p.getByRole('button', { name: CONFIRM_BUTTON }).click()
    const opener = p.getByTitle('Clear all context')
    await opener.and(p.locator('[aria-disabled="true"]')).waitFor({ timeout: 20_000 })
    if (await opener.evaluate(el => el.disabled)) {
      throw new Error('the opener is truly disabled, which breaks the dialog focus restore')
    }
    await shoot(p, `busy-${theme}`)
    await p.close()
  }

  {
    const p = await open('row-busy', theme)
    p.on('dialog', d => d.accept())
    await p.getByRole('button', { name: /^\d+ agents?$/ }).click()
    const rowClear = p.getByTitle('Clear context').first()
    await rowClear.click()
    await rowClear.and(p.locator('[aria-busy="true"]')).waitFor({ timeout: 20_000 })
    await shoot(p, `row-busy-${theme}`)
    await p.close()
  }

  {
    const p = await open('error', theme)
    await p.getByTitle('Clear all context').click()
    await p.getByRole('button', { name: CONFIRM_BUTTON }).click()
    await p.locator('[data-testid="clear-context-error"]').waitFor({ timeout: 20_000 })
    await shoot(p, `error-${theme}`)
    await p.close()
  }
}

await b.close()
