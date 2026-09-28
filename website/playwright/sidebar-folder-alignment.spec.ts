import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * Measured-x assertions for the chat sidebar's three folder alignment guides.
 *
 * `ChatSidebar.folderAlignment.test.tsx` (jsdom) asserts the INPUTS to the
 * geometry — class tokens plus an arithmetic identity between constants — and
 * jsdom has no layout engine, so it stayed green through every one of the four
 * regressions that actually moved the guides (#1211, #3766, #3903, and twice
 * on paper inside #3905). Each of those omitted the 2px `FOLDER_BODY_INSET_PX`
 * container differential between the folder header's box and the nested body's
 * box, which is invisible in the class list. This spec closes that gap: it
 * runs in the `E2E (stub ACP backend, offline)` gate against a real browser
 * and asserts the OUTPUT — `getBoundingClientRect().left` of the rendered
 * elements — so a broken guide fails on measured pixels, not derivations.
 *
 * The three guides (see the algebra comment on renderFolderHeader in
 * src/pages/ChatSidebar.tsx, and website/scripts/capture-folder-glyph.mjs
 * under MEASURE=1 for the manual probe this automates):
 *   1. the `border-l` connector line runs down UNDER its folder's GLYPH: the
 *      line's x lies inside the glyph's 12px span (asserted at depth 1 AND
 *      depth 2). The glyph is the head of its own subtree, not pinned to the
 *      line's exact x, which is what lets it outdent (guide 3);
 *   2. a folder NAME and the agent label / title of every session inside it
 *      share ONE left edge (depth 1 AND depth 2). The tool-call subtitle is
 *      not seedable without a live agent turn; it is a sibling of the title
 *      inside the same block container, so its left edge equals the title's
 *      by construction and its class parity is pinned by the jsdom test.
 *   3. a folder GLYPH hangs a fixed distance LEFT of the content column of
 *      the sessions filed beside it (tree-view outdent): 6px for a nested
 *      folder beside in-folder rows (R_in - P = 9 - 3), 7px for a root folder
 *      beside ungrouped root-lane rows (R - P = 10 - 3).
 *   Depth invariance: the algebra has no per-depth term, so each nesting
 *   level shifts name, content and connector by exactly D + M + B + p = 10px.
 *
 * Equalities are asserted within 0.5px (sub-pixel rounding headroom only —
 * every historical break was ≥1px, most were 2px); the guide-1 "inside the
 * glyph span" check is a strict range.
 *
 * SERIAL-RUN DEPENDENCY: session-tags-folders.spec.ts wipes ALL folders (and,
 * under KIROCREW_E2E_EPHEMERAL=1, all slots) in its beforeEach. The E2E gate
 * (test/test_playwright_e2e.py) runs with CI=1 → workers: 1, so the files
 * never interleave there; an ad-hoc fully-parallel run against a shared
 * gateway can race this spec's fixtures against those wipes.
 */

const TOLERANCE = 0.5

// Mirrors of the algebra's constants (renderFolderHeader comment in
// src/pages/ChatSidebar.tsx; the jsdom test imports FOLDER_BODY_INSET_PX and
// pins the class tokens these come from). Relative distances only — never
// absolute page x, which shifts with the viewport and the sidebar's width.
/** G: the folder collapse glyph's rendered width. */
const GLYPH_WIDTH_PX = 12
/** D + M + B + p = 2 + 4 + 1 + 3: what one nesting level costs. */
const LEVEL_INDENT_PX = 10
/** R - P = 10 - 3: a root folder's glyph hangs this far left of root-lane content. */
const ROOT_GLYPH_OUTDENT_PX = 7
/** R_in - P = 9 - 3: a nested folder's glyph hangs this far left of sibling content. */
const NESTED_GLYPH_OUTDENT_PX = 6

async function primeBrowser(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
  })
  await page.setViewportSize({ width: 1400, height: 1000 })
}

// Everything this spec seeds, torn down in afterEach. Only resources created
// by THIS spec are deleted — no harness-wide wipes — so a developer pointing
// the suite at a personal gateway loses nothing of their own.
const seeded = { folders: [] as string[], slots: [] as string[] }

async function seedFolder(request: APIRequestContext, name: string, parentId?: string) {
  const body: Record<string, string> = { name }
  if (parentId) body.parent_id = parentId
  const res = await request.post('/api/chat/folders', { data: body })
  expect(res.ok(), `POST /api/chat/folders "${name}" should succeed`).toBe(true)
  const folder = await res.json()
  expect(folder.id, `folder "${name}" should be created`).toBeTruthy()
  seeded.folders.push(folder.id)
  return folder as { id: string }
}

async function seedSlot(request: APIRequestContext, folderId?: string) {
  const res = await request.post('/api/chat/slots', { data: { agent: 'default' } })
  expect(res.ok(), 'POST /api/chat/slots should succeed').toBe(true)
  const slot = await res.json()
  expect(slot.key, 'slot should be created').toBeTruthy()
  seeded.slots.push(slot.key)
  if (folderId) {
    const assign = await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folderId } })
    expect(assign.ok(), `folder assignment for slot ${slot.key} should succeed`).toBe(true)
  }
  return slot as { key: string }
}

test.afterEach(async ({ request }) => {
  for (const key of seeded.slots.splice(0)) await request.delete(`/api/chat/slots/${key}`)
  for (const id of seeded.folders.splice(0).reverse()) await request.delete(`/api/chat/folders/${id}`)
})

/** One measurement pass over every element the guides reference.
 *  `missing` names the elements not yet in the DOM (still rendering, or a
 *  selector broken by a refactor); `lefts` is non-null only when empty. */
type Lefts = {
  fGlyph: number; fConnector: number; fName: number
  sGlyph: number; sConnector: number; sName: number
  aAgent: number; aTitle: number
  bAgent: number; bTitle: number
  uAgent: number
}
type Measurement = { lefts: Lefts | null; missing: string[] }

async function measureLefts(page: Page, ids: { fid: string; sid: string; aKey: string; bKey: string; uKey: string }): Promise<Measurement> {
  return page.evaluate(({ fid, sid, aKey, bKey, uKey }) => {
    const left = (el: Element | null | undefined) => (el ? el.getBoundingClientRect().left : null)
    const glyph = (id: string) => document.querySelector(`[data-testid="folder-collapse-${id}"]`)
    const nameOf = (id: string) => glyph(id)?.closest('button')?.querySelector('span.flex-1')
    // The connector is the `border-l` on the folder-children wrapper; its
    // border-box left IS the line's x. querySelector inside the folder's own
    // drop container returns the wrapper in document order — the folder's own
    // wrapper precedes any nested folder's.
    const connector = (id: string) => document.querySelector(`[data-folder-drop="${id}"]`)?.querySelector('.border-l.border-border')
    // A slot key can legally appear on more than one node (#912); scope folder
    // rows to their folder's container and the root-lane row to "not inside
    // any folder container".
    const rowIn = (folderId: string, key: string) =>
      document.querySelector(`[data-folder-drop="${folderId}"]`)?.querySelector(`[data-slot-key="${window.CSS.escape(key)}"]`)
    const rootRow = (key: string) =>
      Array.from(document.querySelectorAll(`[data-slot-key="${window.CSS.escape(key)}"]`)).find(el => !el.closest('[data-folder-drop]'))
    const agentOf = (row: Element | null | undefined) => row?.querySelector('.session-agent-label')
    const titleOf = (row: Element | null | undefined) => agentOf(row)?.nextElementSibling
    const rowA = rowIn(fid, aKey)
    const rowB = rowIn(sid, bKey)
    const rowU = rootRow(uKey)
    const m: Record<string, number | null> = {
      fGlyph: left(glyph(fid)), fConnector: left(connector(fid)), fName: left(nameOf(fid)),
      sGlyph: left(glyph(sid)), sConnector: left(connector(sid)), sName: left(nameOf(sid)),
      aAgent: left(agentOf(rowA)), aTitle: left(titleOf(rowA)),
      bAgent: left(agentOf(rowB)), bTitle: left(titleOf(rowB)),
      uAgent: left(agentOf(rowU)),
    }
    const missing = Object.keys(m).filter(k => m[k] === null)
    return { lefts: missing.length ? null : m, missing }
  }, ids) as Promise<Measurement>
}

/** Rows animate in (framer-motion x: -12 → 0), so wait until two consecutive
 *  frames measure identically before asserting. This poll is also the wait
 *  for the elements themselves — a persistent `missing:` list in its failure
 *  message names exactly which selector never resolved. */
async function settleAndMeasure(page: Page, ids: Parameters<typeof measureLefts>[1]): Promise<Lefts> {
  let prev: Measurement = { lefts: null, missing: ['<no measurement yet>'] }
  await expect
    .poll(async () => {
      const cur = await measureLefts(page, ids)
      const stable = cur.lefts !== null && prev.lefts !== null
        && JSON.stringify(cur.lefts) === JSON.stringify(prev.lefts)
      prev = cur
      return stable ? 'settled' : `missing: [${cur.missing.join(', ')}]`
    }, { timeout: 15000, message: 'sidebar rows should render and settle (a persistent missing: list names the broken selector)' })
    .toBe('settled')
  return prev.lefts as Lefts
}

function expectAligned(m: Lefts, a: keyof Lefts, b: keyof Lefts, guide: string) {
  const detail = `${guide}: ${a}=${m[a]} vs ${b}=${m[b]} (all: ${JSON.stringify(m)})`
  expect(Math.abs(m[a] - m[b]), detail).toBeLessThanOrEqual(TOLERANCE)
}

/** `a` sits exactly `offset` px right of `b` (within TOLERANCE). */
function expectOffset(m: Lefts, a: keyof Lefts, b: keyof Lefts, offset: number, guide: string) {
  const detail = `${guide}: ${a}=${m[a]} - ${b}=${m[b]} should be ${offset} (all: ${JSON.stringify(m)})`
  expect(Math.abs((m[a] - m[b]) - offset), detail).toBeLessThanOrEqual(TOLERANCE)
}

/** The connector line's x lies inside the glyph's span: glyph.left <= line < glyph.left + G.
 *  Strict range — no tolerance — a line at the glyph's right edge is already off it. */
function expectUnderGlyph(m: Lefts, connector: keyof Lefts, glyph: keyof Lefts, guide: string) {
  const detail = `${guide}: ${connector}=${m[connector]} should be in [${m[glyph]}, ${m[glyph] + GLYPH_WIDTH_PX}) (all: ${JSON.stringify(m)})`
  expect(m[connector], detail).toBeGreaterThanOrEqual(m[glyph])
  expect(m[connector], detail).toBeLessThan(m[glyph] + GLYPH_WIDTH_PX)
}

test.describe('Sidebar folder alignment guides (measured x)', () => {
  test('glyph/connector, name/content, and glyph-outdent guides hold at depth 1 and 2', async ({ page, request }) => {
    await primeBrowser(page)
    // One root folder, one subfolder, a session in each, one ungrouped
    // session in the root lane — the minimal tree that makes every guide
    // measurable at two depths.
    const uniq = Date.now().toString(36)
    const parent = await seedFolder(request, `Align-parent-${uniq}`)
    const sub = await seedFolder(request, `Align-sub-${uniq}`, parent.id)
    const inParent = await seedSlot(request, parent.id)
    const inSub = await seedSlot(request, sub.id)
    const ungrouped = await seedSlot(request)

    await page.goto('/chat')

    // The settle poll below is also the render wait: it names any selector
    // that never resolves. Total budget: 10s navigation + 15s poll < the 30s
    // per-test timeout, so a failure keeps its per-step message.
    const m = await settleAndMeasure(page, {
      fid: parent.id, sid: sub.id,
      aKey: inParent.key, bKey: inSub.key, uKey: ungrouped.key,
    })

    // Coherence anchors — difference-only assertions would all hold vacuously at
    // left=0 if a regression left the nodes in the DOM without boxes
    // (display:none subtree). Anchor the frame: the tree renders at a
    // positive x, and indentation strictly increases with depth.
    expect(m.fGlyph, `root glyph should have a real box (all: ${JSON.stringify(m)})`).toBeGreaterThan(0)
    expect(m.sName, `depth-2 name should sit right of depth-1 name (all: ${JSON.stringify(m)})`).toBeGreaterThan(m.fName)
    expect(m.fName, `folder name should sit right of its glyph (all: ${JSON.stringify(m)})`).toBeGreaterThan(m.fGlyph)

    // GUIDE 1 — the connector line runs down under its folder's glyph: the
    // line's x lies inside the glyph span (P <= D + M < P + G).
    expectUnderGlyph(m, 'fConnector', 'fGlyph', 'guide 1 depth 1 (connector under glyph)')
    expectUnderGlyph(m, 'sConnector', 'sGlyph', 'guide 1 depth 2 (connector under glyph)')

    // GUIDE 2 — folder name shares one left edge with the agent label and
    // title of the sessions inside it.
    expectAligned(m, 'fName', 'aAgent', 'guide 2 depth 1 (name on agent label)')
    expectAligned(m, 'aAgent', 'aTitle', 'guide 2 depth 1 (agent label on title)')
    expectAligned(m, 'sName', 'bAgent', 'guide 2 depth 2 (name on agent label)')
    expectAligned(m, 'bAgent', 'bTitle', 'guide 2 depth 2 (agent label on title)')

    // GUIDE 3 — a folder's glyph hangs a fixed distance LEFT of the content
    // column of the sessions filed beside it: 6px for a nested folder beside
    // in-folder rows, 7px for the root folder beside ungrouped root-lane rows.
    expectOffset(m, 'aAgent', 'sGlyph', NESTED_GLYPH_OUTDENT_PX, 'guide 3 (nested glyph hangs left of sibling content)')
    expectOffset(m, 'uAgent', 'fGlyph', ROOT_GLYPH_OUTDENT_PX, 'guide 3 root lane (root glyph hangs left of ungrouped content)')

    // Depth invariance — the algebra has no per-depth term: every level costs
    // exactly D + M + B + p, for the name, the content column, and the
    // connector alike.
    expectOffset(m, 'sName', 'fName', LEVEL_INDENT_PX, 'depth invariance (name)')
    expectOffset(m, 'bAgent', 'aAgent', LEVEL_INDENT_PX, 'depth invariance (content column)')
    expectOffset(m, 'sConnector', 'fConnector', LEVEL_INDENT_PX, 'depth invariance (connector)')
  })
})
