/**
 * Screenshot harness for the folder modal's INHERITED-agent orphan state.
 *
 * UX Review blocks on an evidence gap: the flagged inherited-agent trigger —
 * "Inherit (repo-dev — not in this project)" — appears in no attachment. Every
 * scene in `website/capture/folder-agent-picker.tsx` passes either an explicit
 * `default_agent` or an empty `folders: []` with `parentId: ''`, so
 * `inheritedAgent` is always empty there and this branch has never rendered in
 * any capture. This is a NEW frame, not a replacement of a stale one.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is
 * needed. A parent folder pins `default_agent: 'repo-dev'`; a child folder
 * (parent_id set, no own pick) is opened in EDIT mode via the sidebar's own
 * "Folder settings" menu item, then re-scoped from a directory that has
 * repo-dev to one that scans fine but lacks it.
 *
 * Two frames:
 *   01 inherit-named  — child modal open, trigger reads "Inherit (repo-dev)",
 *                       Save enabled
 *   02 inherit-orphan-flagged — after re-scoping to a directory that scans but
 *                       lacks repo-dev: trigger reads
 *                       "Inherit (repo-dev — not in this project)", the notice
 *                       is visible, Save disabled
 *
 * The frames cannot lie. Frame 2 asserts, any of which failing exits non-zero
 * so the PNGs are not citable:
 *   - the trigger contains "Inherit" and "not in this project"
 *   - the trigger uses the EM DASH "—" and NOT the nested "() (" spelling —
 *     that flatten is the whole point of this round
 *   - Save is disabled
 *   - the notice is present and does not contradict the label ("isn't
 *     installed" would be false here — the agent IS installed, just out of scope)
 *   - the notice sits inside the modal's visible box, not below the scroll fold
 * Frame 1 asserts the trigger names the inherited agent WITHOUT the orphan
 * flag, proving the flag is caused by the re-scope rather than present all
 * along.
 *
 * Two more frames close a later coverage gap on the same two committed
 * attachments (UX Review named both by number, so the delivery path is
 * settled — this is coverage, not a defect):
 *   03 inherit-not-available — the child's OWN project dir is cleared while it
 *                       still inherits repo-dev, and no ancestor supplies a
 *                       directory either, so `effectiveProjectDir === ''`.
 *                       `orphanDirless` requires exactly that (`orphanAgent
 *                       && orphanDirless`, see FolderConfigModal.tsx's
 *                       `effectiveInheritedAgentLabel`) — repo-dev is absent
 *                       from the GLOBAL fallback roster (stubDashboardApi's
 *                       fixture only ever serves kirocrew/oncall), so clearing
 *                       the dir turns the valid inherited pick into a dir-less
 *                       orphan. Trigger reads
 *                       "Inherit (repo-dev — needs a project directory)".
 *   04 inherit-unverified — the same inheriting child re-scoped to a directory
 *                       whose scan FAILS (any path this harness's own route
 *                       stub doesn't recognise 503s; used here as `/repo/err`
 *                       to name the state, matching the sibling
 *                       capture-folder-agent-roster-scan-error.mjs pattern).
 *                       `unvalidatableAgent` requires `rosterScanError` with
 *                       `orphanAgent` FALSY — a scan error suppresses
 *                       `orphanAgent` via `rosterUnsettled` regardless (an
 *                       unknown roster cannot prove absence), which is exactly
 *                       why this is a separate gate from frame 03's. Trigger
 *                       reads "Inherit (repo-dev — can't verify)".
 *
 * Frames 3-4 assert the same shape as frame 2 (trigger contains "Inherit" +
 * em dash + that state's own exact cause text; no nested "() (" spelling; no
 * OTHER state's cause text; Save disabled; the state's notice visible and not
 * clipped) — except frame 4's notice is the roster scan-error row
 * (`folder-config-agent-roster-error`), not `folder-config-agent-notice`:
 * that span only renders while `orphanAgent` is truthy (see the modal's own
 * `{orphanAgent ? (...) : rosterScanError ? null : (...)}` branch), and
 * `orphanAgent` is false for the whole `unvalidatableAgent` state.
 *
 * A fifth frame closes a later UX Review evidence gap on the same head: the
 * diff adds FOUR inherit flag variants (not-in-project, dir-less, unverified,
 * and not-installed), but only three had a captured trigger. The fourth —
 *   05 inherit-not-installed — trigger reads "Inherit (repo-dev — not installed)"
 * — is the NARROW state per `effectiveInheritedAgentLabel` in
 * FolderConfigModal.tsx: it needs `orphanAgent` truthy with `orphanIsRescopeOnly`
 * FALSY and `orphanDirless` FALSY. `orphanIsRescopeOnly` is false only when BOTH
 * `effectiveProjectDir === seededEffectiveDir` (no re-scope in this session) AND
 * `orphanAgent === seedRef.current.defaultAgent` (the orphan is the folder's OWN
 * saved pick, unchanged). Since the label only ever appears when the picker is
 * cleared back to Inherit (`draft.defaultAgent === ''`), that second equality can
 * only hold if the folder's ORIGINAL saved `default_agent` coincidentally names
 * the same agent its ancestor pins — i.e. seed child `f2` with its OWN
 * `default_agent: 'repo-dev'` under a directory that scans but lacks it
 * (`/repo/other`, already an orphan on open — the ordinary explicit-orphan
 * round-trip), then clear the picker to Inherit in-session without touching the
 * directory. The inherited resolution walks to the parent's pinned `repo-dev`,
 * same absent name, same unmoved directory: `orphanIsRescopeOnly` and
 * `orphanDirless` both stay false, and the label falls through to
 * `inherit_named_not_installed`.
 *
 * Frame 5 asserts trigger shape like frames 3-4 (contains "Inherit" + em dash +
 * "not installed"; no nested "() (" spelling; no OTHER state's cause text; the
 * `folder-config-agent-notice` present, unclipped, and not contradicting the
 * label with the not-in-project framing) — but NOT the same Save state: this
 * is the round-trip exception (`blockingOrphan` requires `orphanAgent !==
 * seedRef.current.defaultAgent || effectiveProjectDir !== seededEffectiveDir`,
 * both false here, by construction), so Save stays ENABLED, per the
 * component's own comment on that branch ("The orphan is round-tripped, not
 * blocked"). The footer-token assertion below reflects that: frame 5 asserts
 * the ENABLED token `text-muted-strong`, not the dimmed `text-muted` the other
 * disabled-Save frames carry.
 *
 * Footer-token assertion (all frames, added this round): a separate copy
 * change now dims the modal footer's "Enter to submit" hint from
 * `text-muted-strong` to `text-muted` whenever `canSubmit` is false — see the
 * `footer` prop in FolderConfigModal.tsx. Frames 02-04 have Save disabled and
 * must show the dimmed `text-muted` token; frames 01 and 05 (Save enabled)
 * must keep `text-muted-strong`. This is the one part of every frame that
 * proves it was taken AFTER that copy change landed, rather than being a
 * stale image re-saved under a new name — a stale frame would still pass
 * every label/notice assertion (the label text is unrelated to the footer)
 * but carry the WRONG footer token.
 *
 * Usage: node scripts/capture-folder-agent-inherited-orphan.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MODAL, ORPHAN_NOTICE, SUBMIT, PROJECT_DIR, assertFooterToken } from './lib/folder-agent-orphan-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-agent-inherited-orphan'
const ROSTER_ERROR_ROW = '[data-testid="folder-config-agent-roster-error"]'
// Unique to this scene (frame 04's scan-error state) — not part of the
// shared dirless-pick harness, so it stays local rather than in
// lib/folder-agent-orphan-harness.mjs.
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

// Parent pins repo-dev; the child has no own pick, so it INHERITS the parent's
// agent (resolveFolderAgent walks parent_id). Both live under the same "Kiro"
// top-level entry the sibling script uses, kept minimal.
//
// `f3` is a SEPARATE child, used only for frame 05: unlike `f2` (no own pick,
// used for frames 01-04), `f3` carries its OWN `default_agent: 'repo-dev'` —
// the same name its parent pins — seeded under `/repo/other` (scans fine, lacks
// repo-dev). That is what makes the narrow not-installed state reachable: the
// folder's ORIGINAL saved pick must equal the agent it would inherit, so that
// clearing the picker to Inherit in-session does not count as a rescope (see
// the file-header comment on frame 05).
const folders = [
  { id: 'f1', name: 'Payments', icon: '💳', order: 0, collapsed: false, default_agent: 'repo-dev' },
  { id: 'f2', name: 'Backend', icon: '⚙️', order: 0, collapsed: false, parent_id: 'f1' },
  { id: 'f3', name: 'Worker', icon: '🛠️', order: 1, collapsed: false, parent_id: 'f1', default_agent: 'repo-dev', project_dir: '/repo/other' },
]

const slots = [
  {
    key: 's1', title: 'Inherited agent states', messages: 4, running: false,
    agent: 'kirocrew', created: '2026-07-20T01:00:00Z',
    last_ts: '2026-08-01T20:00:00Z', folder_id: 'f2',
  },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2, // 11-13px modal type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { folders, slots })
  logPageProblems(page)

  // One directory scans and declares repo-dev, the other scans fine but lacks
  // it. Registered AFTER stubDashboardApi so this wins for the scoped call;
  // the unscoped /api/agents keeps the fixture's global roster.
  await page.route('**/api/agents?*project_path=*', async route => {
    const url = route.request().url()
    if (url.includes('%2Frepo%2Fok') || url.includes('/repo/ok')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agents: [
            { name: 'repo-dev', source: 'project', scope: 'project' },
            { name: 'kirocrew', source: 'builtin' },
          ],
          default_agent: '',
        }),
      })
      return
    }
    if (url.includes('%2Frepo%2Fother') || url.includes('/repo/other')) {
      // Scans fine, but does NOT declare repo-dev: that is what makes the
      // INHERITED agent an orphan whose absence the roster can actually assert,
      // rather than an unverifiable scan error.
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agents: [
            { name: 'other-dev', source: 'project', scope: 'project' },
            { name: 'kirocrew', source: 'builtin' },
          ],
          default_agent: '',
        }),
      })
      return
    }
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'project roster scan refused: audit write failed' }),
    })
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Open the CHILD folder's own settings — not "New folder" — via the sidebar's
  // "Folder settings" menu item, the real click path a user takes to edit an
  // existing folder.
  await page.click('[data-testid="folder-menu-f2"]')
  await page.click('[data-testid="folder-settings-f2"]')
  await page.waitForSelector(MODAL, { timeout: 8000 })

  const agent = page.getByRole('combobox', { name: 'Default agent' })
  await agent.waitFor({ state: 'visible', timeout: 8000 })

  // Seed the child's project dir to the directory that HAS repo-dev, so the
  // inherited pick is valid before the re-scope that flags it.
  await page.fill(PROJECT_DIR, '/repo/ok')
  await page.waitForTimeout(700) // debounce + scan settle

  // ── 01: inherited agent named, no flag ──
  const named = await agent.textContent()
  if (!named || !named.includes('Inherit') || !named.includes('repo-dev')) {
    throw new Error(`expected the trigger to name the inherited agent, got: ${named}`)
  }
  if (named.includes('not in this project')) {
    throw new Error(`the trigger is already flagged before any re-scope: ${named}`)
  }
  if (await page.isDisabled(SUBMIT)) {
    throw new Error('Save is disabled on a valid inherited pick — the frame would document the wrong state')
  }
  await assertFooterToken(page, '01', false)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-01-inherit-named.png` })
  console.log('wrote', `${OUT}/${PREFIX}-01-inherit-named.png`)

  // ── 02: re-scope to a directory that scans fine but lacks the inherited agent ──
  await page.fill(PROJECT_DIR, '/repo/other')
  await page.waitForSelector(ORPHAN_NOTICE, { timeout: 8000 })
  await page.waitForTimeout(400)

  const flagged = await agent.textContent()
  if (!flagged || !flagged.includes('Inherit') || !flagged.includes('not in this project')) {
    throw new Error(`expected the flagged inherited-orphan label, got: ${flagged}`)
  }
  if (!flagged.includes('—')) {
    throw new Error(`expected the em dash before "not in this project", got: ${flagged}`)
  }
  if (flagged.includes(') (not in this project')) {
    throw new Error(`the trigger uses the retired nested-paren spelling: ${flagged}`)
  }
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('Save is enabled on an inherited-agent orphan — the frame would document the wrong state')
  }
  const notice = await page.textContent(ORPHAN_NOTICE)
  if (!notice || /isn.t installed/i.test(notice)) {
    throw new Error(`the notice contradicts the label by claiming the agent is not installed: ${notice}`)
  }
  // The clipping Watch: the notice is the only thing naming why Save is dead,
  // so it must sit INSIDE the modal's visible box, not below its scroll fold.
  const noticeBox = await page.locator(ORPHAN_NOTICE).boundingBox()
  const modalBox = await page.locator(MODAL).boundingBox()
  if (!noticeBox || !modalBox) throw new Error('could not measure the notice against the modal')
  if (noticeBox.y + noticeBox.height > modalBox.y + modalBox.height + 1) {
    throw new Error(
      `the notice is clipped at the modal fold: notice ends at ${noticeBox.y + noticeBox.height}, `
      + `modal ends at ${modalBox.y + modalBox.height}`,
    )
  }
  await assertFooterToken(page, '02', true)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-02-inherit-orphan-flagged.png` })
  console.log('wrote', `${OUT}/${PREFIX}-02-inherit-orphan-flagged.png`)

  // ── 03: clear the child's own project dir entirely — the dir-less inherited
  // orphan. No ancestor supplies one either (parent f1 sets none), so
  // effectiveProjectDir becomes '' and orphanDirless requires exactly that.
  // repo-dev is absent from the GLOBAL fallback roster (stubDashboardApi's
  // fixture only ever serves kirocrew/oncall), so the previously-valid
  // inherited pick becomes a dir-less orphan the instant the dir is cleared. ──
  await page.fill(PROJECT_DIR, '')
  await page.waitForSelector(ORPHAN_NOTICE, { timeout: 8000 })
  await page.waitForTimeout(400)

  const dirless = await agent.textContent()
  if (!dirless || !dirless.includes('Inherit') || !dirless.includes('needs a project directory')) {
    throw new Error(`expected the dir-less inherited-orphan label, got: ${dirless}`)
  }
  if (!dirless.includes('—')) {
    throw new Error(`expected the em dash before "needs a project directory", got: ${dirless}`)
  }
  if (dirless.includes(') (')) {
    throw new Error(`the trigger uses the retired nested-paren spelling: ${dirless}`)
  }
  if (dirless.includes('not in this project') || dirless.includes("can't verify")) {
    throw new Error(`the trigger carries another state's cause text: ${dirless}`)
  }
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('Save is enabled on a dir-less inherited orphan — the frame would document the wrong state')
  }
  const dirlessNotice = await page.textContent(ORPHAN_NOTICE)
  if (!dirlessNotice) throw new Error('the dir-less orphan notice is empty')
  const dirlessNoticeBox = await page.locator(ORPHAN_NOTICE).boundingBox()
  const modalBoxForDirless = await page.locator(MODAL).boundingBox()
  if (!dirlessNoticeBox || !modalBoxForDirless) throw new Error('could not measure the dir-less notice against the modal')
  if (dirlessNoticeBox.y + dirlessNoticeBox.height > modalBoxForDirless.y + modalBoxForDirless.height + 1) {
    throw new Error(
      `the dir-less orphan notice is clipped at the modal fold: notice ends at ${dirlessNoticeBox.y + dirlessNoticeBox.height}, `
      + `modal ends at ${modalBoxForDirless.y + modalBoxForDirless.height}`,
    )
  }
  await assertFooterToken(page, '03', true)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-03-inherit-not-available.png` })
  console.log('wrote', `${OUT}/${PREFIX}-03-inherit-not-available.png`)

  // ── 04: re-scope to a directory whose scan FAILS — the inherited-unverified
  // state. This harness's own route stub already 503s any path other than
  // /repo/ok and /repo/other; name it /repo/err to match the sibling
  // capture-folder-agent-roster-scan-error.mjs convention. A scan error
  // suppresses orphanAgent via rosterUnsettled regardless of roster content,
  // which is why unvalidatableAgent — and its "can't verify" wording — is a
  // separate gate from frame 03's, and why the notice to check is the roster
  // scan-error row, not the orphan-notice span (that span only renders while
  // orphanAgent is truthy; it is false for the whole rest of this frame). ──
  await page.fill(PROJECT_DIR, '/repo/err')
  await page.waitForSelector(ROSTER_ERROR_ROW, { timeout: 8000 })
  await page.waitForTimeout(400)

  const unverified = await agent.textContent()
  if (!unverified || !unverified.includes('Inherit') || !unverified.includes("can't verify")) {
    throw new Error(`expected the unverified inherited label, got: ${unverified}`)
  }
  if (!unverified.includes('—')) {
    throw new Error(`expected the em dash before "can't verify", got: ${unverified}`)
  }
  if (unverified.includes(') (')) {
    throw new Error(`the trigger uses the retired nested-paren spelling: ${unverified}`)
  }
  if (unverified.includes('not in this project') || unverified.includes('needs a project directory')) {
    throw new Error(`the trigger carries another state's cause text: ${unverified}`)
  }
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('Save is enabled on an unverifiable inherited agent — the frame would document the wrong state')
  }
  const rosterErrorText = await page.textContent(ROSTER_ERROR_ROW)
  if (!rosterErrorText) throw new Error('the roster scan-error row is empty')
  const rosterErrorBox = await page.locator(ROSTER_ERROR_ROW).boundingBox()
  const modalBoxForUnverified = await page.locator(MODAL).boundingBox()
  if (!rosterErrorBox || !modalBoxForUnverified) throw new Error('could not measure the roster error row against the modal')
  if (rosterErrorBox.y + rosterErrorBox.height > modalBoxForUnverified.y + modalBoxForUnverified.height + 1) {
    throw new Error(
      `the roster scan-error row is clipped at the modal fold: row ends at ${rosterErrorBox.y + rosterErrorBox.height}, `
      + `modal ends at ${modalBoxForUnverified.y + modalBoxForUnverified.height}`,
    )
  }
  await assertFooterToken(page, '04', true)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-04-inherit-unverified.png` })
  console.log('wrote', `${OUT}/${PREFIX}-04-inherit-unverified.png`)

  // ── 05: the not-installed inherited state. A SEPARATE folder (f3, not f2) —
  // its OWN saved default_agent is 'repo-dev', the same name its parent (f1)
  // pins, seeded under '/repo/other' (scans fine, lacks repo-dev — so the
  // explicit pick is already an orphan on open, the ordinary round-trip case).
  // Clearing the picker to Inherit in-session, WITHOUT touching the directory,
  // makes the effective agent the inherited 'repo-dev' — same absent name, same
  // unmoved dir — so orphanIsRescopeOnly stays false (orphanAgent ===
  // seedRef.current.defaultAgent, effectiveProjectDir === seededEffectiveDir)
  // and orphanDirless stays false (the dir is still set). Falls through to
  // inherit_named_not_installed. Close this modal and open a fresh one on f3
  // rather than continuing to mutate f2, so frame 04's re-scoped state cannot
  // leak into this one. Frame 04 leaves f2's draft dirty (re-scoped to
  // /repo/err), which arms `guardAccidentalDismiss` — Escape is a no-op while
  // guarded (see Modal.tsx's `softDismiss`), so close via the explicit Cancel
  // button instead, the one dismissal path the guard never blocks. ──
  await page.getByRole('button', { name: 'Cancel' }).click()
  await page.waitForSelector(MODAL, { state: 'detached', timeout: 8000 })

  await page.click('[data-testid="folder-menu-f3"]')
  await page.click('[data-testid="folder-settings-f3"]')
  await page.waitForSelector(MODAL, { timeout: 8000 })

  const agent3 = page.getByRole('combobox', { name: 'Default agent' })
  await agent3.waitFor({ state: 'visible', timeout: 8000 })

  // On open, f3's own explicit pick (repo-dev) is already absent from
  // /repo/other's roster — confirm that pre-clear state before touching the
  // picker, so a later assertion failure cannot be confused with a fixture
  // mistake made further up.
  await page.waitForSelector(ORPHAN_NOTICE, { timeout: 8000 })
  const preClear = await agent3.textContent()
  if (!preClear || !preClear.includes('repo-dev')) {
    throw new Error(`expected f3 to open on its own orphaned explicit pick, got: ${preClear}`)
  }

  // Clear the picker back to Inherit — the real interaction path (open the
  // popup, pick the "Inherit (repo-dev)" option), matching how the test suite
  // drives this same combobox.
  await agent3.click()
  await page.getByRole('option', { name: /^Inherit \(repo-dev\)$/ }).click()
  await page.waitForTimeout(400)

  const notInstalled = await agent3.textContent()
  if (!notInstalled || !notInstalled.includes('Inherit') || !notInstalled.includes('not installed')) {
    throw new Error(`expected the not-installed inherited label, got: ${notInstalled}`)
  }
  if (!notInstalled.includes('—')) {
    throw new Error(`expected the em dash before "not installed", got: ${notInstalled}`)
  }
  if (notInstalled.includes(') (')) {
    throw new Error(`the trigger uses the retired nested-paren spelling: ${notInstalled}`)
  }
  if (
    notInstalled.includes('not in this project')
    || notInstalled.includes('needs a project directory')
    || notInstalled.includes("can't verify")
  ) {
    throw new Error(`the trigger carries another state's cause text: ${notInstalled}`)
  }
  // Unlike frames 02-04, this state is the ROUND-TRIP exception
  // (`blockingOrphan` requires `orphanAgent !== seedRef.current.defaultAgent ||
  // effectiveProjectDir !== seededEffectiveDir`, both false here): Save stays
  // ENABLED, per the component's own comment ("The orphan is round-tripped,
  // not blocked"). Assert the state this predicate actually produces rather
  // than the disabled Save the brief assumed for this frame — see the report.
  if (await page.isDisabled(SUBMIT)) {
    throw new Error('Save is disabled on the not-installed round-trip state — the frame would document the wrong state')
  }
  const notInstalledNotice = await page.textContent(ORPHAN_NOTICE)
  if (!notInstalledNotice) throw new Error('the not-installed orphan notice is empty')
  // The notice must not contradict the label with the OTHER states' framing —
  // this state's own copy speaks to installation, not project scope.
  if (/not in this project/i.test(notInstalledNotice)) {
    throw new Error(`the notice contradicts the label with the not-in-project framing: ${notInstalledNotice}`)
  }
  const notInstalledBox = await page.locator(ORPHAN_NOTICE).boundingBox()
  const modalBoxForNotInstalled = await page.locator(MODAL).boundingBox()
  if (!notInstalledBox || !modalBoxForNotInstalled) throw new Error('could not measure the not-installed notice against the modal')
  if (notInstalledBox.y + notInstalledBox.height > modalBoxForNotInstalled.y + modalBoxForNotInstalled.height + 1) {
    throw new Error(
      `the not-installed orphan notice is clipped at the modal fold: notice ends at ${notInstalledBox.y + notInstalledBox.height}, `
      + `modal ends at ${modalBoxForNotInstalled.y + modalBoxForNotInstalled.height}`,
    )
  }
  // Save is enabled here (see above), so the footer hint must carry the
  // ENABLED token, text-muted-strong — NOT the dimmed text-muted the brief
  // expected for this frame.
  await assertFooterToken(page, '05', false)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-05-inherit-not-installed.png` })
  console.log('wrote', `${OUT}/${PREFIX}-05-inherit-not-installed.png`)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
