/**
 * Screenshot harness for the meeting-title rename focus restore (#22901).
 * Serves the isolated capture entry (capture/meeting-title-focus.html) with
 * `vite` in dev on 127.0.0.1 and drives the REAL MeetingTitle with Playwright.
 * Nothing listens beyond loopback and no gateway is involved.
 *
 * Frames, per theme:
 *   meeting-title-enter-<theme>   after opening the editor, typing a new title
 *                                 and pressing Enter: the editor closes and the
 *                                 focus ring is back on the title trigger.
 *   meeting-title-escape-<theme>  after opening the editor and pressing Escape:
 *                                 the editor closes and the focus ring is back
 *                                 on the (unchanged) title trigger.
 *
 * Usage (from website/):
 *   node capture/shoot-meeting-title-focus.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/22901-meeting-title-focus'
mkdirSync(OUT, { recursive: true })

const PORT = 5231
const vite = spawn('node', ['node_modules/vite/bin/vite.js', '--port', String(PORT), '--strictPort'], {
  stdio: 'ignore',
  detached: true,
})

async function waitForServer(base, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(base + '/capture/meeting-title-focus.html')
      if (r.ok) return
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 500))
  }
  throw new Error('vite dev server did not start')
}

const base = `http://127.0.0.1:${PORT}`
const url = (theme) => `${base}/capture/meeting-title-focus.html?theme=${theme}`

try {
  await waitForServer(base)
  const browser = await chromium.launch({
    executablePath: chromiumExecutable(),
    env: { ...process.env, LD_LIBRARY_PATH: '' },
  })

  for (const theme of ['dark', 'light']) {
    // Enter frame: open, type, Enter -> ring back on the renamed trigger.
    {
      const page = await browser.newPage({ viewport: { width: 760, height: 220 }, reducedMotion: 'reduce' })
      await page.goto(url(theme), { waitUntil: 'networkidle' })
      const trigger = page.getByRole('button', { name: 'Weekly standup' })
      await trigger.waitFor({ timeout: 15000 })
      await trigger.click()
      const input = page.getByRole('textbox', { name: 'Meeting title' })
      await input.fill('Sprint review')
      await input.press('Enter')
      // Editor closes; focus lands on the trigger (now named for the new title).
      await page.getByRole('button', { name: 'Sprint review' }).waitFor({ timeout: 5000 })
      await page.waitForTimeout(400)
      await page.screenshot({ path: `${OUT}/meeting-title-enter-${theme}.png` })
      await page.close()
    }
    // Escape frame: open, Escape -> ring back on the unchanged trigger.
    {
      const page = await browser.newPage({ viewport: { width: 760, height: 220 }, reducedMotion: 'reduce' })
      await page.goto(url(theme), { waitUntil: 'networkidle' })
      const trigger = page.getByRole('button', { name: 'Weekly standup' })
      await trigger.waitFor({ timeout: 15000 })
      await trigger.click()
      const input = page.getByRole('textbox', { name: 'Meeting title' })
      await input.fill('Discarded draft')
      await input.press('Escape')
      await page.getByRole('button', { name: 'Weekly standup' }).waitFor({ timeout: 5000 })
      await page.waitForTimeout(400)
      await page.screenshot({ path: `${OUT}/meeting-title-escape-${theme}.png` })
      await page.close()
    }
  }

  await browser.close()
  console.log('wrote', OUT)
} finally {
  try { process.kill(-vite.pid) } catch { /* already gone */ }
}
