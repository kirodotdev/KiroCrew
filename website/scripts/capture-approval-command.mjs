/**
 * Screenshot harness for an approval notification whose body carries the
 * command as FENCED CODE (`src/lib/approvalNotificationBody.ts`).
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend).
 *
 * What the change is about, and therefore what each frame has to prove:
 * `tool_input` is raw shell text, and rendering it as markdown displayed
 * `rm -rf *cache*` as `rm -rf cache` — a narrower command than the one being
 * authorized. The body now fences the command, and the excerpt flattener
 * (`stripMd`) keeps code contents and unpaired markers literal. Two in-browser
 * surfaces changed shape:
 *
 * Frames (captured once per theme, named <prefix>-<dark|light>-<frame>.png):
 *   01-feed-row      activity feed row: an actionable approval renders its
 *                    whole command as a code block -- both lines, `*cache*`
 *                    intact, no fence leak -- with Approve / Reject beside it
 *   02-bell-popover  the bell popover's mac card for the same approval: the
 *                    shared card's two-line clamp is replaced by the whole
 *                    command, wrapped, with Approve / Reject beneath it
 *   03-detail-panel  detail view: the command rendered as ONE code block, the
 *                    source label and the purpose as separate paragraphs, and
 *                    the metadata row labelled "Kind" (it prints the note's
 *                    kind), distinct from the body's "Source:" line
 *
 * The seeded body is not hand-typed: the harness bundles the real helper with
 * esbuild (i18n stubbed to the English catalog) and refuses to run when the
 * helper's output differs from the inlined expectation, so the fixture cannot
 * drift from what the app actually emits.
 *
 * Usage: node scripts/capture-approval-command.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import * as esbuild from 'esbuild'
import { mkdirSync, readdirSync, readFileSync, rmSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/approval-command'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })
// Deterministic: a failed run must not leave last run's image behind to be
// mistaken for this run's evidence.
for (const f of readdirSync(OUT)) if (f.startsWith(`${PREFIX}-`) && f.endsWith('.png')) rmSync(resolve(OUT, f))

const SRC = fileURLToPath(new URL('../src/', import.meta.url))
const EN = JSON.parse(readFileSync(resolve(SRC, 'i18n/locales/en.json'), 'utf8'))
const en = key => key.split('.').reduce((o, k) => o?.[k], EN)

// The command holds a THREE-backtick run, so the helper must pick a
// four-backtick fence; the `*cache*` glob is the pair the old excerpt deleted.
const SOURCE = 'agent'
const COMMAND = 'echo safe\nrm -rf *cache*; echo ```; ls ~/.ssh/id_rsa'
const PURPOSE = 'clearing the build cache'
const EXPECTED_BODY = [
  '**Source:** agent',
  '',
  '````approval-command',
  COMMAND,
  '````',
  '',
  PURPOSE,
].join('\n')

/** Bundle the real helper, aliasing its i18n import to the English catalog. */
async function realApprovalBody() {
  const built = await esbuild.build({
    entryPoints: [resolve(SRC, 'lib/approvalNotificationBody.ts')],
    bundle: true, write: false, format: 'esm', platform: 'node',
    plugins: [{
      name: 'stub-i18n',
      setup(b) {
        b.onResolve({ filter: /\/i18n\/t$/ }, () => ({ path: 'i18n-stub', namespace: 'stub' }))
        // Same lookup the English catalog resolves to at runtime; the catalog
        // itself is handed over via globalThis so it is not inlined into code.
        b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
          contents: 'export const i18nT = k => k.split(".").reduce((o, x) => o?.[x], globalThis.__EN__)',
        }))
      },
    }],
  })
  globalThis.__EN__ = EN
  const code = built.outputFiles[0].text
  const mod = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
  return mod.approvalNotificationBody(SOURCE, COMMAND, PURPOSE)
}

const BODY = await realApprovalBody()
console.log('helper output:', JSON.stringify(BODY))
if (BODY !== EXPECTED_BODY) {
  console.log('expected    :', JSON.stringify(EXPECTED_BODY))
  throw new Error('inlined seed does not match approvalNotificationBody() — fix the fixture')
}
console.log('ASSERT ok: inlined seed byte-identical to approvalNotificationBody() output')

// The note as `useWebSocket` adds it on a live tool_call approval: kind
// approval, unacked, with an approval_id for decisions in the detail panel.
const APPROVAL_NOTE = {
  kind: 'approval',
  source: 'agent',
  channel: 'agent.approval',
  priority: 'default',
  title: 'Tool approval: shell',
  body: BODY,
  ts: '2026-09-20T16:40:12.000000+00:00',
  approval_id: 'appr-7f3c2a',
  acked: false,
}

// A neighbour so the row is judged beside an ordinary one, not in isolation.
const CRON_NOTE = {
  kind: 'cron',
  source: 'system',
  channel: 'system.cron',
  priority: 'default',
  title: 'Nightly registry sweep',
  body: 'Checked 41 entries, nothing to do.',
  ts: '2026-09-20T06:05:00.000000+00:00',
  acked: true,
}

/** Route /api/** — MUST return truthy once fulfilled (see stubDashboardApi). */
const api = async (path, route) => {
  if (path === '/api/notifications') {
    await json(route, { notifications: [CRON_NOTE, APPROVAL_NOTE], unread: 1 })
    return true
  }
  return false
}

let failures = 0
const assert = (ok, label, detail) => {
  console.log(`ASSERT ${ok ? 'ok  ' : 'FAIL'}: ${label}${detail !== undefined ? ` — ${JSON.stringify(detail)}` : ''}`)
  if (!ok) failures++
}

const shot = async (page, name) => {
  const path = resolve(OUT, `${PREFIX}-${name}.png`)
  await page.screenshot({ path, animations: 'disabled' })
  console.log(`frame: ${path}`)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await stubDashboardApi(page, { theme, extra: api })
    await page.goto(`${base}/notifications`, { waitUntil: 'networkidle' })

    // ── Frame 01: the feed row ──
    const row = page.locator('[data-notif-row]').filter({ hasText: APPROVAL_NOTE.title }).first()
    await row.waitFor()
    // An actionable approval row renders its body in full: the fenced command
    // arrives as a code block, every line present, no clamp. Same lazy
    // highlighter as the panel, so read it row by row.
    const rowBody = row.locator('[data-testid="approval-body"]').first()
    const rowLines = rowBody.locator('.code-block [role="region"] [data-content] [data-line]')
    await rowLines.first().waitFor()
    const rowCommand = (await rowLines.allTextContents()).join('\n')
    assert(rowCommand === COMMAND, `[${theme}] feed row shows the command byte-for-byte, both lines`, rowCommand)
    assert(await rowLines.count() === 2 && await rowLines.nth(1).isVisible(), `[${theme}] the command's second line is rendered and visible (not clamped)`, await rowLines.count())
    const rowText = await rowBody.textContent()
    assert(!rowText.includes('````'), `[${theme}] no four-backtick fence leaks into the feed row`, rowText)
    // Wrapped, not clipped: the code region must not scroll horizontally, and
    // the tail of the long line must be inside the row's box.
    const rowRegion = rowBody.locator('.code-block [role="region"]').first()
    const overflow = await rowRegion.evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth }))
    assert(overflow.scroll <= overflow.client + 1, `[${theme}] the command wraps instead of scrolling off the row's edge`, overflow)
    const tailLine = rowLines.nth(1)
    const tailBox = await tailLine.boundingBox()
    const rowBox = await row.boundingBox()
    assert(tailBox && rowBox && tailBox.x + tailBox.width <= rowBox.x + rowBox.width + 1, `[${theme}] the second line ends inside the row`, { tailBox, rowBox })
    assert(overflow.scroll <= overflow.client + 1 && (await tailLine.textContent()).endsWith('id_rsa'), `[${theme}] the destructive tail \`ls ~/.ssh/id_rsa\` is on screen`, await tailLine.textContent())
    const approve = row.getByRole('button', { name: en('components.notifications.notificationFeed.approve'), exact: true })
    const reject = row.getByRole('button', { name: en('components.notifications.notificationFeed.reject'), exact: true })
    assert(await approve.count() === 1 && await approve.isVisible(), `[${theme}] feed row renders Approve beside the whole command`)
    assert(await reject.count() === 1 && await reject.isVisible(), `[${theme}] feed row renders Reject beside the whole command`)
    const rowEdit = rowBody.getByRole('button', { name: en('components.monacoCodeBlock.edit_code_block'), exact: true })
    assert(await rowEdit.count() === 0, `[${theme}] the command awaiting authorization has no edit affordance on the row`)
    await shot(page, `${theme}-01-feed-row`)

    // ── Frame 02: the bell popover (mac variant) ──
    // The popover renders rows through the shared NotificationCard, whose
    // default excerpt is a 140-char two-line clamp. An approval card must take
    // the full read-only body instead: it keeps one-click Approve/Reject.
    await page.getByRole('button', { name: en('app.notifications'), exact: true }).first().click()
    const card = page.locator('[data-notification-card]').filter({ hasText: APPROVAL_NOTE.title }).first()
    await card.waitFor()
    const cardBody = card.locator('[data-testid="approval-body"]').first()
    const cardLines = cardBody.locator('.code-block [role="region"] [data-content] [data-line]')
    await cardLines.first().waitFor()
    const cardCommand = (await cardLines.allTextContents()).join('\n')
    assert(cardCommand === COMMAND, `[${theme}] bell popover card shows the command byte-for-byte, both lines`, cardCommand)
    assert(await card.locator('.line-clamp-2').count() === 0, `[${theme}] the approval card carries no two-line clamp`)
    const cardRegion = cardBody.locator('.code-block [role="region"]').first()
    const cardOverflow = await cardRegion.evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth }))
    assert(cardOverflow.scroll <= cardOverflow.client + 1, `[${theme}] the popover command wraps instead of scrolling off the card's edge`, cardOverflow)
    const cardTail = cardLines.nth(1)
    assert(await cardTail.isVisible() && (await cardTail.textContent()).endsWith('id_rsa'), `[${theme}] the destructive tail is on screen in the popover card`, await cardTail.textContent())
    const cardApprove = card.getByRole('button', { name: en('components.notifications.notificationFeed.approve'), exact: true })
    const cardReject = card.getByRole('button', { name: en('components.notifications.notificationFeed.reject'), exact: true })
    assert(await cardApprove.count() === 1 && await cardApprove.isVisible(), `[${theme}] popover card renders Approve beside the whole command`)
    assert(await cardReject.count() === 1 && await cardReject.isVisible(), `[${theme}] popover card renders Reject beside the whole command`)
    const cardEdit = cardBody.getByRole('button', { name: en('components.monacoCodeBlock.edit_code_block'), exact: true })
    assert(await cardEdit.count() === 0, `[${theme}] the popover command has no edit affordance`)
    await shot(page, `${theme}-02-bell-popover`)
    await page.keyboard.press('Escape')
    await card.waitFor({ state: 'detached' })

    // ── Frame 03: the detail panel ──
    await row.locator('[data-testid="approval-body"]').first().click()
    const bodyBox = page.locator('.msg-content.bg-card').first()
    const region = bodyBox.locator('.code-block [role="region"]').first()
    // The highlighter is a lazy chunk behind Suspense and paints its rows into
    // a shadow root; Playwright locators pierce it, `textContent()` on the
    // region does not. Wait for the rows, then read them line by line.
    const lines = region.locator('[data-content] [data-line]')
    await lines.first().waitFor()
    const blocks = await bodyBox.locator('.code-block').count()
    assert(blocks === 1, `[${theme}] detail panel has exactly one .code-block`, blocks)
    const codeText = (await lines.allTextContents()).join('\n')
    assert(codeText === COMMAND, `[${theme}] code block text is byte-identical to the seeded command`, codeText)
    const paras = await bodyBox.locator('p').allTextContents()
    assert(paras.length === 2 && paras[0] === 'Source: agent' && paras[1] === PURPOSE,
      `[${theme}] source label and purpose render as two separate paragraphs`, paras)
    // The metadata row is labelled by what it prints (the note's kind), so a
    // reader cannot take it and the body's "Source:" for one field. Scoped to
    // the panel: the row is the sibling directly above the body's scroll pane.
    const panel = bodyBox.locator('xpath=ancestor::div[contains(@class,"border-l")][1]')
    const kindLabel = await panel.locator('.uppercase.tracking-\\[\\.04em\\]').first().textContent()
    assert(kindLabel === en('pages.artifactsPage.kind') && kindLabel !== en('components.notifications.notificationDetailPanel.source'),
      `[${theme}] the metadata row is labelled Kind, distinct from the body's Source label`, kindLabel)
    const panelApprove = panel.getByRole('button', { name: en('components.notifications.notificationDetailPanel.approve'), exact: true })
    const panelReject = panel.getByRole('button', { name: en('components.notifications.notificationDetailPanel.reject'), exact: true })
    assert(await panelApprove.count() === 1 && await panelApprove.isVisible(), `[${theme}] detail panel renders Approve beside the full command`)
    assert(await panelReject.count() === 1 && await panelReject.isVisible(), `[${theme}] detail panel renders Reject beside the full command`)
    // Selecting the row acked it. The approval is still undecided (the panel
    // offers both controls), so the row must still show the whole command.
    const rowAfter = (await rowLines.allTextContents()).join('\n')
    assert(rowAfter === COMMAND, `[${theme}] after being read, the row still shows both lines of the command`, rowAfter)
    const panelEdit = bodyBox.getByRole('button', { name: en('components.monacoCodeBlock.edit_code_block'), exact: true })
    assert(await panelEdit.count() === 0, `[${theme}] the detail panel's command has no edit affordance either`)
    await shot(page, `${theme}-03-detail-panel`)
    await page.close()
  }
  console.log(`wrote frames to ${resolve(OUT)} (prefix ${PREFIX}); ${failures} assertion failure(s)`)
} finally {
  await browser.close()
  srv.close()
}
if (failures) process.exit(1)
