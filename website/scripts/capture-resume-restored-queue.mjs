/**
 * Screenshot harness for the Resume button on an interrupted chat that also
 * has a restored message queue (#16780).
 *
 * Two scenarios in one run:
 *   clean   — interrupted chat, no restored queue → Resume IS shown (control)
 *   queued  — interrupted chat + restored queue   → before the fix: no Resume
 *                                                    after the fix:  Resume shows
 *
 * The "clean" frames prove the Resume button renders correctly on any interrupted
 * slot. The "queued" frames show the new combination. Run this against a build
 * that carries the fix to produce the "after" shots.
 *
 *   node scripts/capture-resume-restored-queue.mjs [outDir] [--dist <dist>]
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')

const args = process.argv.slice(2)
const FLAGS = new Set(['--dist'])
const flag = name => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined }
const OUT = args.find((a, i) => !FLAGS.has(a) && !FLAGS.has(args[i - 1])) || '../temp-screenshots/resume-restored-queue'
const DIST = flag('--dist')
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

const RESTART_ROW = 'This turn was interrupted when the app restarted. Review the partial output above, then resume to continue.'

mkdirSync(OUT, { recursive: true })
const now = Date.now() / 1000

/** Common transcript: user row + gateway_restart_interruption error row. */
const baseMessages = [
  {
    role: 'user',
    ts: now - 300,
    content: 'Add the config-reload endpoint as discussed.',
    meta: { mid: 'u-1' },
  },
  {
    role: 'error',
    ts: now - 60,
    content: RESTART_ROW,
    cls: 'msg msg-err',
    meta: { kind: 'gateway_restart_interruption', mid: 'e-1' },
  },
]

const scenes = [
  {
    name: 'clean',
    suffix: 'no-queue',
    slot: 'chat-resume-clean',
    queue: [],
    interrupted: true,
    totalMessages: 2,
    assertResume: true,
  },
  {
    name: 'with restored queue',
    suffix: 'with-queue',
    slot: 'chat-resume-with-queue',
    queue: [
      { id: 'q-1', content: 'Also add a test for the reload path', meta: { restored: true } },
    ],
    interrupted: true,
    totalMessages: 2,
    // On an old build (before the fix) selectContinuable returns false → no Resume.
    // On a build carrying the fix it returns true → Resume shows.
    assertResume: true,   // fixed build: Resume shows on interrupted + queued slot
  },
]

async function runScene(scene, theme) {
  const slotRow = {
    key: scene.slot,
    title: 'Add the config-reload endpoint',
    running: false,
    interrupted: scene.interrupted,
    last_message: RESTART_ROW,
    messages: scene.totalMessages,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(now),
    source_links: [],
    source_links_total: 0,
  }
  const detail = {
    running: false,
    has_more: false,
    total: scene.totalMessages,
    project: PROJECT,
    queue: scene.queue,
    messages: baseMessages,
  }
  const h = await openTranscriptHarness({
    slot: scene.slot,
    project: PROJECT,
    slots: [slotRow],
    detail,
    viewport: { width: 1400, height: 950 },
    dist: DIST,
  })
  const { page } = h
  let failures = 0
  const assert = (label, ok) => {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  const shot = async name => {
    const file = `${OUT}/${name}.png`
    await page.screenshot({ path: file })
    console.log('  wrote', file)
  }

  await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1500 })

  const card = page.locator('[data-testid="error-card"]').filter({ hasText: /interrupted when the app restarted/ })
  assert(`error card present`, await card.count() >= 1)

  const resume = page.locator('[data-testid="composer-continue"]')
  const resumeCount = await resume.count()
  if (scene.assertResume) {
    assert(`composer offers Resume`, resumeCount === 1)
  } else {
    console.log(`  INFO: composer Resume count = ${resumeCount} (expected 1 on fixed build, 0 on old build)`)
  }

  await shot(`${scene.suffix}-${theme}`)
  await h.close()
  return failures
}

async function main() {
  let total = 0
  for (const scene of scenes) {
    for (const theme of ['dark', 'light']) {
      console.log(`\n=== ${scene.name} / ${theme} ===`)
      total += await runScene(scene, theme)
    }
  }
  if (total) { console.error(`\n${total} assertion(s) failed`); process.exit(1) }
  console.log('\nAll assertions passed.')
}

main().catch(err => { console.error(err); process.exit(1) })
