/**
 * Screenshots of the Dev Fleet release-channel worktree rows (PR #10066).
 *
 * Drives the ISOLATED capture entry (website/capture/devfleet-release-channel.html),
 * which mounts the REAL DevFleetPage with `fetch` stubbed at the network seam to
 * serve the `/fleet` payload. Every state under review is decided by that payload,
 * so each page load here IS the scenario the reviewer asked to see.
 *
 * Each scene asserts its headline state — and, where the claim is an ABSENCE,
 * also asserts the thing that must not be there — before shooting, so this can
 * never quietly emit a screenshot of an error boundary, a prerequisite gate, or
 * the wrong row class. The last three scenes click Create first, since its
 * outcomes are the one thing the payload cannot decide. The frames are then
 * compared for distinct byte sizes: two identical frames mean the fixture
 * failed, not that the states match.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-devfleet-release-channel.mjs http://127.0.0.1:6812 ../temp-screenshots/devfleet-release-channel-10066
 */
import { chromium } from 'playwright'
import { mkdirSync, statSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/devfleet-release-channel-10066'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

const shot = []

// Whether Create is live is the entire difference between the benign row and the
// blocked one, and the two look alike in a still. Returns an assertion the scene
// runs before shooting, so a frame can never claim a state the run did not check.
// A tooltip is an attribute, not text, so `text=` cannot see it: assert it by title.
const titled = (title, want) => async (page, scene) => {
  const n = await page.getByTitle(title, { exact: true }).count()
  if (n !== want) throw new Error(`scene=${scene}: expected ${want} element(s) titled "${title}", found ${n}`)
}
// Every channel row renders BEHIND as the n/a marker with one tooltip, whatever
// its state, so the same assertion runs on the adopted, at-tip, unresolved and
// off-tag frames: exactly two n/a cells on the page (PR and Behind of the one
// channel row) and exactly one carrying the Behind tooltip.
const behindNa = async (page, scene) => {
  const na = await page.getByText('n/a', { exact: true }).count()
  if (na !== 2) throw new Error(`scene=${scene}: expected 2 n/a cells (PR + Behind), found ${na}`)
  await titled('Release rows are measured by version — see the badge', 1)(page, scene)
}
const createEnabled = (want) => async (page, scene) => {
  const btn = page.getByRole('button', { name: 'Create worktree' })
  const disabled = await btn.isDisabled()
  if (disabled === want) {
    throw new Error(`scene=${scene}: Create worktree must be ${want ? 'enabled' : 'disabled'}`)
  }
}

async function shoot(scene, mustSee, mustNotSee, name, assert) {
  await page.goto(`${BASE}/capture/devfleet-release-channel.html?scene=${scene}&theme=dark`)
  for (const text of mustSee) {
    await page.waitForSelector(`text=${text}`, { timeout: 15000 })
  }
  // Absence is only meaningful once the row itself is on screen, which the
  // mustSee loop above has just established.
  for (const text of mustNotSee) {
    const n = await page.locator(`text=${text}`).count()
    if (n !== 0) throw new Error(`scene=${scene}: "${text}" must NOT be present, found ${n}`)
  }
  // Some claims are about a control's STATE, not about text: whether Create is
  // live is the whole difference between the benign and blocked rows, and a
  // screenshot of a disabled button is indistinguishable from a muted one at a
  // glance. Assert it here so the frame is evidence of a state the run verified.
  if (assert) await assert(page, scene)
  // Park the pointer away from the table: a hovered row raises its action
  // toolbar over the row name in every one of these harnesses.
  await page.mouse.move(4, 4)
  await page.waitForTimeout(400) // let the relative timestamps settle
  const path = `${OUT}/${name}`
  await page.screenshot({ path, fullPage: false })
  shot.push({ name, size: statSync(path).size })
  console.log(`captured ${name}`)
}

// The lane is materialized: the badge carries the release this checkout sits on
// AND, in its text, the newer release (`0.5.0 · latest 0.5.3`) — behind-the-tip
// is legible without the warn colour or a hover, and "latest" is the word an
// update banner uses, not git's "tip", which read as a branch head. The Behind
// cell is the n/a marker — never `↓3`, `↓3 newer` or `↓412`: the column counts
// commits behind main, a release row is measured by version, and the badge says so.
await shoot('adopted', ['release-channel-stable', '0.5.0 · latest 0.5.3'], ['↓3', '↓412', 'newer', 'tip 0.5'],
  '01-release-channel-adopted-dark.png', behindNa)

// The lane exists with no worktree: a muted placeholder row is the only surface
// the feature is discoverable from, and its Create button must read as live. The
// status line SAYS what Create does, naming the version in visible text, rather
// than leaving that to the version pill's tooltip.
await shoot('placeholder', ['release-channel-stable', 'No worktree yet — Create checks out 0.5.0', 'Create'], [],
  '02-release-channel-placeholder-create-dark.png')

// A BRANCH checkout occupying the reserved basename is NOT adopted: ordinary
// controls, an ordinary behind-main count, and in the name cell a muted status
// SENTENCE rather than a pill. Three pill wordings ("Not a release worktree", "Not
// the channel", "Name reserved — on a branch") each failed a cold read: a pill holds
// a verdict but not the situation and the way out, and the verdict alone read as
// the pin degrading. The sentence sits in the slot the placeholder row uses for its
// own status line and says all three things in visible text; the tooltip names the
// controls that exist (Prune merged, or git worktree move/remove). No warn Badge on
// the row: nothing is broken, it is a feature worktree with an unlucky name.
await shoot('taken',
  ['release-channel-stable', '↓5', "On a branch, so the stable release can't use this name — rename or remove it to free it"],
  ['0.5.0', 'Not a release worktree', 'Name reserved', 'Not the channel'],
  '03-release-channel-name-taken-branch-dark.png',
  async (page, scene) => {
    const sentence = page.getByTestId('release-channel-name-taken-stable')
    if ((await sentence.count()) !== 1) throw new Error(`scene=${scene}: expected one name-taken status sentence`)
    const title = await sentence.getAttribute('title')
    for (const want of ['Prune merged', 'git worktree remove']) {
      if (!title || !title.includes(want)) throw new Error(`scene=${scene}: tooltip must name "${want}", got "${title}"`)
    }
    // The status sentence is a bare span, never inside a Badge: the row must carry
    // no warn pill at all (a warn pill on this row is the verdict reading that
    // failed). `bg-warn-subtle` is the warn variant's class in components/ui.tsx;
    // the row is the sentence's nearest grid ancestor.
    const row = sentence.locator('xpath=ancestor::div[contains(@style, "grid")][1]')
    const warn = await row.locator('.bg-warn-subtle').count()
    if (warn !== 0) throw new Error(`scene=${scene}: the name-taken row must carry no warn badge, found ${warn}`)
    const inBadge = await sentence.locator('xpath=ancestor-or-self::*[contains(@class, "rounded-full")]').count()
    if (inBadge !== 0) throw new Error(`scene=${scene}: the status sentence must not be rendered inside a Badge`)
  })

// The steady state: the tree is AT the channel tip, so the badge takes its ok
// variant and the bare version — no `latest` suffix, which is the behind state's
// word (and no `tip`, the word it replaced). The Behind cell is the SAME n/a
// marker as in shot 01 — not a dash, which on every other row means "up to date
// with main" — so at-tip is legible from the badge alone; the warn variant in
// shot 01 cannot stand in for this.
await shoot('at-tip', ['release-channel-stable', '0.5.3'], ['↓0', '↓412', 'newer', 'latest 0.5', 'tip 0.5'],
  '04-release-channel-at-tip-dark.png', behindNa)

// Benign and documented, NOT an incident: nothing fetched here yet, said as plain
// information that names Create as the fetch, with Create still live and carrying
// the same answer as its tooltip, NO version pill (the status line is the whole
// statement; a "not on a release" pill restated it in the adopted off-tag
// badge's words), and no ErrorNotice anywhere.
await shoot('unpublished', ['release-channel-stable', 'Create fetches the newest one'],
  ['could not be resolved', 'not on a release'], '05-release-channel-unpublished-create-live-dark.png',
  async (page, scene) => {
    await createEnabled(true)(page, scene)
    await titled('Create fetches the stable release tags and checks out the newest one, detached', 1)(page, scene)
  })

// The failure variant of that same row: Create disabled, the short label in the
// cell (the lane framed as a channel, not a bare `stable`), and the full git
// message in the row-scoped ErrorNotice below it.
await shoot('blocked-placeholder',
  ['release-channel-stable', 'release channel could not be resolved', 'Could not resolve host'], [],
  '06-release-channel-blocked-placeholder-dark.png', createEnabled(false))

// The second variant the placeholder frame cannot show: an ADOPTED row whose
// resolve() failed. It keeps the release its tree holds and routes the failure to
// the same ErrorNotice rather than to a Badge title, which is not keyboard-reachable.
// The badge is the bare version — no `latest ?`, no `latest` at all, since the
// tip is unknown and the tooltip says so. Its Behind cell is the same n/a marker
// as every other channel state — never a `?` cell and never a dash.
await shoot('blocked-adopted',
  ['release-channel-stable', '0.5.0', 'Could not resolve host'], ['latest 0.5', 'latest ?', 'tip 0.5', 'tip ?'],
  '07-release-channel-blocked-adopted-dark.png',
  async (page, scene) => {
    await behindNa(page, scene)
    await titled('On 0.5.0; the stable channel tip could not be resolved', 1)(page, scene)
  })

// Adoption is by shape, so a lane tree detached at NO release tag is still the
// lane's row. The version slot reads "not on a release" — the TREE's condition,
// so it is not read as the resolver-error row's worry, and never the bare lane
// word, which on a row already named `release-channel-stable` reads as a
// version — and the tip is named only in the tooltip; Behind is the n/a marker
// here too.
await shoot('no-release',
  ['release-channel-stable', 'not on a release'], ['0.5.0', '0.5.3', '↓7', '↓412'],
  '08-release-channel-no-release-dark.png', behindNa)

// ── Create's three outcomes ──────────────────────────────────────────────────
// Every frame above is a payload the page rendered; these three are the CLICK.
// The button is the placeholder's only control and the feature's only entry
// point, so what it does while working, when it works, and when git refuses is
// the half of the row a still of the resting state cannot show. Each scene loads
// the placeholder with `create=<mode>` (the entry answers the POST per mode), waits
// for the row, clicks the real button, asserts the outcome, then shoots.
const CREATE_URL = `${BASE}/capture/devfleet-release-channel.html?scene=placeholder&theme=dark`
const CREATE_BTN = { name: 'Create worktree' }

// The create=* fixture resolves the placeholder AT the tip: Create checks out the
// newest release, so a fresh worktree is never behind, and the row it produces is
// the at-tip one (bare `0.5.3`, no `latest`) with Provision on it.
async function clickCreate(mode) {
  await page.goto(`${CREATE_URL}&create=${mode}`)
  await page.waitForSelector('text=No worktree yet — Create checks out 0.5.3', { timeout: 15000 })
  const btn = page.getByRole('button', CREATE_BTN)
  if (await btn.isDisabled()) throw new Error(`create=${mode}: Create must be live before the click`)
  await btn.click()
}

async function shootAfterCreate(mode, name, assert) {
  await assert(page, `placeholder&create=${mode}`)
  await page.mouse.move(4, 4)
  const path = `${OUT}/${name}`
  await page.screenshot({ path, fullPage: false })
  shot.push({ name, size: statSync(path).size })
  console.log(`captured ${name}`)
}

// In flight: the POST never settles, so the frame holds the busy state as long as
// it likes. The same button, now disabled, reads "Creating…" — and "Create
// worktree" is gone from the page, so a double click has nothing to land on.
await clickCreate('busy')
await shootAfterCreate('busy', '09-release-channel-create-busy-dark.png', async (page, scene) => {
  const busy = page.getByRole('button', { name: 'Creating…', exact: true })
  await busy.waitFor({ timeout: 15000 })
  if (!(await busy.isDisabled())) throw new Error(`scene=${scene}: the busy button must be disabled`)
  const live = await page.getByRole('button', CREATE_BTN).count()
  if (live !== 0) throw new Error(`scene=${scene}: "Create worktree" must be gone while creating, found ${live}`)
})

// Success: the toast names the release the lane was created at and the next
// CONTROL by its own label ("next: Provision"), and the /fleet refetch that
// follows serves the freshly-materialized row — detached AT the tip (bare `0.5.3`,
// no `latest`: a just-created worktree cannot be behind the release it was just
// created at) and NOT yet provisioned, so the Provision button the toast points at
// is on the row under it. ToastHost dismisses after 4 s; the assertions run inside
// that window, so the toast is in the frame.
await clickCreate('ok')
await shootAfterCreate('ok', '10-release-channel-create-success-dark.png', async (page, scene) => {
  await page.waitForSelector('text=Created stable worktree at 0.5.3', { timeout: 15000 })
  const toast = await page.locator('[role="status"]', { hasText: 'Created stable worktree' }).innerText()
  if (!toast.includes('next: Provision')) throw new Error(`scene=${scene}: toast must name the next control, got "${toast}"`)
  const badge = page.getByText('0.5.3', { exact: true })
  await badge.waitFor({ timeout: 15000 })
  if ((await badge.count()) !== 1) throw new Error(`scene=${scene}: expected one bare 0.5.3 badge, found ${await badge.count()}`)
  // `latest 0.5`, not bare `latest`: the page's help paragraph uses the word.
  for (const text of ['latest 0.5', 'tip 0.5', '0.5.0']) {
    const n = await page.locator(`text=${text}`).count()
    if (n !== 0) throw new Error(`scene=${scene}: "${text}" must NOT be present on the at-tip row, found ${n}`)
  }
  // The control the toast names must exist on the page, else "next: Provision"
  // points at nothing.
  const provision = await page.getByRole('button', { name: 'Provision', exact: true }).count()
  if (provision !== 1) throw new Error(`scene=${scene}: expected one Provision button (the toast's "next"), found ${provision}`)
  const placeholder = await page.getByTestId('release-channel-placeholder-stable').count()
  if (placeholder !== 0) throw new Error(`scene=${scene}: the placeholder must be replaced by the materialized row, found ${placeholder}`)
})

// Refusal: git's own message, prefixed by what did not happen, in a row-scoped
// ErrorNotice — not a toast, which would self-dismiss and leave the unchanged
// placeholder as the only record. Dismiss and the agent hand-off are on it, and
// Create is live again so the retry is one click away.
await clickCreate('fail')
await shootAfterCreate('fail', '11-release-channel-create-failed-dark.png', async (page, scene) => {
  const notice = page.getByTestId('release-channel-create-error-stable')
  await notice.waitFor({ timeout: 15000 })
  const text = await notice.innerText()
  for (const want of ['Could not create the release-channel worktree', 'missing but already registered worktree']) {
    if (!text.includes(want)) throw new Error(`scene=${scene}: notice must contain "${want}", got "${text}"`)
  }
  for (const name of ['Dismiss', 'Ask the agent']) {
    const n = await notice.getByRole('button', { name }).count()
    if (n !== 1) throw new Error(`scene=${scene}: expected one "${name}" button on the notice, found ${n}`)
  }
  await createEnabled(true)(page, scene)
})

// ── Two more row states ──────────────────────────────────────────────────────

// The reserved directory exists but its HEAD could not be read: neither adopted
// (no `worktree`) nor branch-occupied (no `name_taken_by_branch`). The placeholder
// is keyed off the NAME being in the fleet, so the directory is ONE ordinary row —
// no version badge (nothing is known about what it holds), no Create (the
// directory exists), no placeholder testid — and the git failure reaches the
// row's ErrorNotice with the agent hand-off, never a Badge title.
await shoot('unreadable',
  ['release-channel-stable', 'not a git repository'],
  ['0.5.0', '0.5.3', 'latest 0.5', 'No worktree yet', 'not on a release', 'Create worktree'],
  '12-release-channel-unreadable-head-dark.png',
  async (page, scene) => {
    const names = await page.getByText('release-channel-stable', { exact: true }).count()
    if (names !== 1) throw new Error(`scene=${scene}: one directory is one row; found ${names} elements named release-channel-stable`)
    const create = await page.getByRole('button', { name: 'Create worktree' }).count()
    if (create !== 0) throw new Error(`scene=${scene}: Create must not be offered for a directory that exists, found ${create}`)
    const placeholder = await page.getByTestId('release-channel-placeholder-stable').count()
    if (placeholder !== 0) throw new Error(`scene=${scene}: no placeholder row beside the real directory, found ${placeholder}`)
    const notice = page.getByTestId('release-channel-error-stable')
    if ((await notice.count()) !== 1) throw new Error(`scene=${scene}: expected the row-scoped ErrorNotice`)
    const text = await notice.innerText()
    if (!text.includes('git rev-parse HEAD: fatal: not a git repository')) {
      throw new Error(`scene=${scene}: the notice must carry the backend's error text, got "${text}"`)
    }
  })

// The release row's "..." menu, open: Rebase onto main is SUPPRESSED on a release
// worktree (rebasing a detached tag onto main is not a coherent request, and the
// backend refuses it anyway), so the open menu is the only frame that shows the
// item's absence rather than the closed trigger. The other items stay, so the
// frame also shows this is a pruned menu, not an empty one. No mouse-park after
// opening: MenuBtn closes on an outside click/pointer move, and a closed menu is
// not the evidence.
await page.goto(`${BASE}/capture/devfleet-release-channel.html?scene=adopted&theme=dark`)
await page.waitForSelector('text=0.5.0 · latest 0.5.3', { timeout: 15000 })
{
  const scene = 'menu-open'
  // The release row is the grid row whose name cell reads release-channel-stable;
  // its trailing actions cell holds the one "More actions" trigger on that row.
  const row = page.locator('div', { has: page.getByText('release-channel-stable', { exact: true }) }).filter({ has: page.getByRole('button', { name: 'More actions' }) }).last()
  const trigger = row.getByRole('button', { name: 'More actions' })
  if ((await trigger.count()) !== 1) throw new Error(`scene=${scene}: expected one "More actions" trigger on the release row, found ${await trigger.count()}`)
  await trigger.click()
  const menu = page.getByRole('menu', { name: 'More actions' })
  await menu.waitFor({ timeout: 15000 })
  // Menu items are Clickable divs with role="button".
  const itemCount = await menu.getByRole('button').count()
  if (itemCount < 1) throw new Error(`scene=${scene}: the open menu must contain at least one item`)
  const rebase = await menu.getByText('Rebase onto main').count()
  if (rebase !== 0) throw new Error(`scene=${scene}: Rebase onto main must be suppressed on the release row, found ${rebase}`)
  const name = '13-release-channel-row-menu-open-dark.png'
  const path = `${OUT}/${name}`
  await page.waitForTimeout(300)
  await page.screenshot({ path, fullPage: false })
  shot.push({ name, size: statSync(path).size })
  console.log(`captured ${name}`)
}

// Two frames of identical size are what a silently-failed fixture looks like.
const sizes = new Set(shot.map((s) => s.size))
if (sizes.size !== shot.length) {
  throw new Error(`frames are not distinct: ${shot.map((s) => `${s.name}=${s.size}`).join(', ')}`)
}
console.log(shot.map((s) => `${s.name} ${s.size}B`).join('\n'))

await browser.close()
