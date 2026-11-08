/**
 * Screenshot harness for the blocked-link card on a DNS host and an IP host.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness,
 * with every /api/** call answered from fixtures. The reply carries two
 * blocked links: one to a DNS host, where the card offers Allow for this host,
 * and one to an IP literal, where the allowed-host list refuses the host and
 * the card says so in Allow's place.
 *
 * Usage: node scripts/capture-blocked-link-allow-host.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/blocked-link-allow-host'
const SLOT = 'chat-blocked-link'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const CHIP = { selector: '[data-testid="blocked-link-inspect"]' }
const HOSTS = { dns: 'reviews.corp.example', ip: '10.0.0.5' }
const record = domain => ({
  domain,
  rule: 'exfil_query_length',
  path: '/reviews',
  query_chars: 290,
  url: `https://${domain}/reviews?filter=${'a'.repeat(40)}`,
  url_withheld: null,
})

const t0 = Date.now() / 1000 - 600
const slots = [
  {
    key: SLOT,
    title: 'Find the open reviews',
    running: false,
    last_message: 'Both review links are below.',
    messages: 2,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Link me the open reviews on both servers.' },
    {
      role: 'assistant',
      ts: t0 + 10,
      content: `The wiki server: [REDACTED: suspicious URL to ${HOSTS.dns}]\n\nThe lab server: [REDACTED: suspicious URL to ${HOSTS.ip}]`,
      meta: { blocked_links: [record(HOSTS.dns), record(HOSTS.ip)] },
    },
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({ slot: SLOT, project: PROJECT, slots, detail })
  let localePinned = false
  async function loadInEnglish(theme) {
    await load(theme, CHIP)
    if (!localePinned) {
      await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
      localePinned = true
    }
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector(CHIP.selector, { timeout: 20000 })
    await page.waitForTimeout(800)
  }
  for (const theme of ['light', 'dark']) {
    for (const [name, index] of [['dns', 0], ['ip', 1]]) {
      await loadInEnglish(theme)
      await page.locator(CHIP.selector).nth(index).evaluate(el => { el.scrollIntoView({ block: 'center' }); el.click() })
      await page.waitForTimeout(600)
      const bubble = page.locator('.message-bubble').filter({ has: page.locator('[data-testid="blocked-link-entry"]') }).first()
      const path = `${OUT}/after-${theme}-${name}-host.png`
      await bubble.screenshot({ path })
      console.log('wrote', path)
    }
  }
  await close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
