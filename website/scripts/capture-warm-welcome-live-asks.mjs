/** Real-browser evidence for the warm welcome's links and buttons.
 *
 * The gateway re-emits the owner's pending choices as one [OPTIONS:] line at
 * the end of the catch-up row's content, and adds a link for each pull
 * request it read live. Frames:
 *
 *   01-buttons-<theme>-<width>.png  the card with its Links row and the
 *                                   composer's option buttons under it
 *
 * Every frame asserts its state before it is written: one card, real links
 * only for github.com pull requests, one button per choice (a choice with a
 * comma stays whole), and no page errors. Mounts capture/members-page.html
 * with route-intercepted, gateway-free API answers; serves the dev build
 * in-process on loopback.
 *
 * Usage (in website/):
 *   node scripts/capture-warm-welcome-live-asks.mjs ../temp-screenshots/warm-welcome-live-asks
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/warm-welcome-live-asks')
mkdirSync(OUT, { recursive: true })

const CHOICES = ['Merge #26100 now', 'Hold #26100 until Monday', 'Tell me what Fix login, signup needs from me']
const BRIEF = {
  status: 'needs_you',
  headline: 'Goal: land the sign-in fix. #26100 waits on you.',
  needs_you: ['Merge #26100 or hold it until Monday', 'Answer the open question on the login item'],
  body: [
    'Since you left (as of 09:30):',
    '- GitHub 09:28: #26100 is green and approved',
    '',
    '1. #26100. Only you can merge it. Merge now ships the fix today; hold keeps one more nightly as proof.',
    '   I recommend merging: all checks passed on the current head.',
  ].join('\n'),
  links: [
    { label: '#26100', url: 'https://github.com/kirodotdev/KiroCrew/pull/26100' },
    { label: '#1', url: 'https://evil.example/pull/1' },
  ],
}
const RESULT = {
  role: 'assistant', cls: 'msg msg-a', ts: '2026-10-05T09:30:09Z',
  content: `Waiting on you: ${BRIEF.headline}\n\n[OPTIONS: ${CHOICES.join(' | ')}]`,
  meta: { mid: 'w2', kind: 'warm_task_brief_result', fingerprint: 'fp', brief: BRIEF },
}

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5201, strictPort: false }, logLevel: 'warn' })
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)

for (const theme of ['dark', 'light']) {
  for (const width of [1280, 360]) {
    const ctx = await browser.newContext({ viewport: { width, height: 1000 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    const errors = []; page.on('pageerror', e => errors.push(String(e)))
    await routeRadarDm(page, { thread: [THREAD[3], THREAD[4], RESULT] })
    await openRadarDm(page, BASE, theme)
    const card = page.getByTestId('warm-brief-card')
    await card.waitFor()
    const tag = `[01/${theme}/${width}]`
    check(`${tag} one card`, (await card.count()) === 1)
    const links = card.getByTestId('warm-brief-links').locator('a')
    check(`${tag} one real link, to the github pull request`, (await links.count()) === 1
      && (await links.first().getAttribute('href')) === 'https://github.com/kirodotdev/KiroCrew/pull/26100')
    for (const choice of CHOICES) {
      const button = page.getByRole('button', { name: choice, exact: true })
      await button.waitFor()
      check(`${tag} button "${choice}"`, (await button.count()) === 1)
    }
    check(`${tag} the comma choice is not split`, (await page.getByRole('button', { name: 'Tell me what Fix login', exact: true }).count()) === 0)
    check(`${tag} no page errors`, errors.length === 0)
    await page.waitForTimeout(400)
    await page.screenshot({ path: resolve(OUT, `01-buttons-${theme}-${width}.png`) })
    await ctx.close()
  }
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
