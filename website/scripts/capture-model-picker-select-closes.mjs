/**
 * Evidence harness for the composer model picker's select-and-close behaviour.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * One click sequence, four frames:
 *   1. the user is mid-sentence in the composer; the picker is opened from the chip
 *   2. the instant after a different row is clicked — the picker is gone, the chip
 *      already names the new model, and focus is back in the composer (the frame
 *      shows keystrokes typed AFTER the pick landing in the draft)
 *   3. the chip re-opens the picker, which now marks the new model active
 *   4. a pick made with the composer NOT focused beforehand — the picker closes
 *      and the composer is left unfocused
 *
 * Focus is asserted in the DOM (`document.activeElement`), not read off pixels,
 * so a regression fails the run rather than producing a misleading frame.
 *
 * `POST /api/chat/slots/:slot/model` answers with the stored model so the chip
 * follows the pick the way it does against a real gateway (the switch callback
 * writes the store from the response; no slots broadcast exists here).
 *
 * Usage: node scripts/capture-model-picker-select-closes.mjs [outDir]
 *        RECORD_VIDEO=1 node scripts/capture-model-picker-select-closes.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/model-picker-select-closes-shots'
mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const FROM = 'claude-opus-5'
const TO = 'claude-sonnet-5'
const MODELS = [
  { model_name: 'auto', description: 'Models chosen by task' },
  { model_name: FROM, description: 'Claude Opus 5' },
  { model_name: TO, description: 'Claude Sonnet 5' },
  { model_name: 'gpt-5.6-sol', description: 'GPT 5.6 Sol' },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const recordVideo = process.env.RECORD_VIDEO === '1'
const context = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  deviceScaleFactor: recordVideo ? 1 : 2,
  ...(recordVideo ? { recordVideo: { dir: join(OUT, 'video-raw'), size: { width: 1280, height: 800 } } } : {}),
})
const page = await context.newPage()

let slotModel = FROM
/** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
const extra = async (path, route) => {
  if (path === '/api/models') { await json(route, MODELS); return true }
  if (path === `/api/chat/slots/${SLOT}/model` && route.request().method() === 'POST') {
    slotModel = JSON.parse(route.request().postData() || '{}').model || slotModel
    await json(route, { ok: true, model: slotModel })
    return true
  }
  return false
}

await stubDashboardApi(page, {
  slots: [{ key: SLOT, messages: 0, running: false, agent: 'kirocrew', model: FROM, mode: '' }],
  theme: 'kiro-dark',
  extra,
  preserveStorage: true,
  localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
})
await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

const shoot = async (name) => {
  const out = join(OUT, name)
  await page.screenshot({ path: out })
  console.log('wrote', out)
}
const chip = (model) => page.getByRole('button', { name: `Model: ${model}` })
const list = page.getByRole('listbox', { name: 'Model list' })

// 1 — the user is typing; the composer holds focus when the chip is pressed.
const composer = page.locator('textarea[data-composer-input]').first()
await composer.click()
await composer.pressSequentially('Draft a release note for ', { delay: 20 })
await chip(FROM).first().click()
await list.waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(600)
await shoot('01-picker-open.png')

// 2 — click a different row: the picker closes on the click, the chip follows,
// and focus comes back to the composer, so typing carries straight on.
await page.getByRole('option', { name: new RegExp(TO) }).click()
await list.waitFor({ state: 'hidden', timeout: 5000 })
await chip(TO).first().waitFor({ state: 'visible', timeout: 5000 })
await page.waitForFunction(() => document.activeElement?.matches('textarea[data-composer-input]'), null, { timeout: 5000 })
await page.keyboard.type('the picker fix', { delay: 20 })
if (!(await composer.inputValue()).endsWith('release note for the picker fix')) {
  throw new Error('keystrokes after the pick did not land in the composer')
}
await page.waitForTimeout(600)
await shoot('02-picker-closed-chip-updated-composer-focused.png')

// 3 — re-open: the new model is the active row.
await chip(TO).first().click()
await list.waitFor({ state: 'visible', timeout: 5000 })
const active = list.getByRole('option', { selected: true })
if ((await active.textContent()) === null || !(await active.textContent()).includes(TO)) {
  throw new Error(`expected the active row to be ${TO}`)
}
await page.waitForTimeout(600)
await shoot('03-reopened-new-model-active.png')

// 4 — pick back with the composer NOT focused beforehand: the picker closes
// and the composer is left alone (focus is not pulled into it).
await page.keyboard.press('Escape')
await list.waitFor({ state: 'hidden', timeout: 5000 })
await page.mouse.click(640, 200) // the transcript — anywhere that is not the composer
await page.waitForFunction(() => !document.activeElement?.matches('textarea[data-composer-input]'), null, { timeout: 5000 })
await chip(TO).first().click()
await list.waitFor({ state: 'visible', timeout: 5000 })
await page.getByRole('option', { name: new RegExp(FROM) }).click()
await list.waitFor({ state: 'hidden', timeout: 5000 })
await chip(FROM).first().waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(300)
if (await page.evaluate(() => document.activeElement?.matches('textarea[data-composer-input]'))) {
  throw new Error('a pick from an unfocused composer must not focus it')
}
await page.waitForTimeout(400)
await shoot('04-pick-without-prior-focus-composer-left-alone.png')
await page.waitForTimeout(400)

await context.close()
await browser.close()
srv.close()
