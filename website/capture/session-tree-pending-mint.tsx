/**
 * Isolated capture entry for the conductor lane over a JUST-DISPATCHED crew: workers a
 * lead minted that have not had a turn yet, photographed through the real `ChatSidebar`.
 *
 * WHY ISOLATED: the window being photographed is the one between `session_create` and a
 * worker's first turn, which lasts as long as that worker's runtime and MCP startup
 * take. It cannot be posed against a live gateway without racing that startup. What
 * stays faithful is the wire: the harness hands the sidebar the rows the slots payload
 * carries, and nothing downstream is stubbed -- the lane resolves `parent.key`, draws
 * the chevron, the count and the glyphs on its own.
 *
 * `parent` on these rows is what `_attach_slot_parents` computes server-side. For a
 * worker with no crew log yet it comes from the row's own `created_by` gated on
 * `lineage_minted`, which is the change under review; the rows below carry both fields
 * as the payload sends them, so what the query parameter switches is the server's
 * ANSWER rather than the renderer's reading of it. That the answer is produced is
 * pinned in `test/test_slot_payload_lineage.py`.
 *
 * Query string: ?theme=dark|light
 *               &minted=1 -- the payload resolves a parent for each worker from its
 *                            mint witness, which is this change: the lead is ONE row
 *                            with its worker count, SHUT by default.
 *                            Without it no worker has a parent, which is what the
 *                            sidebar received before: four top-level strays while the
 *                            lead that minted them sits beside them.
 *               &ran=1    -- the workers have had their first turn, so each one now has
 *                            a crew log of its own and the fold answers for it. Used
 *                            with `&minted=1` to show the row does not MOVE when the
 *                            authority changes hands.
 *
 * Usage: node scripts/capture-session-tree-pending-mint.mjs [devBase] [outDir]
 */
import {
  MIN, at, localRow, mountLocalSidebar, prepareLocalSidebar, type LocalSidebarRow,
} from './localSidebarFixture'

const params = new URLSearchParams(location.search)
prepareLocalSidebar(params)

const LEAD = 'chat-2481'

/** The lead that dispatched them, mid-dispatch. */
const LEAD_ROW: LocalSidebarRow = localRow({
  key: LEAD,
  title: 'Session tree: nest a dispatched child',
  agent: 'kirocrew-lead',
  running: true,
  messages: 212,
  last_ts: at(20_000),
  last_message: 'Four workers dispatched. Watching the board.',
})

/** The four workers, as the slots payload carries them the instant they are minted:
 *  attributed to the lead by this process, with no turn and so no crew log. */
const MINTED: Array<{ key: string; title: string }> = [
  { key: 'chat-2486', title: 'A1: pending layer and the display join' },
  { key: 'chat-2487', title: 'A2: the mint witness on the wire' },
  { key: 'chat-2488', title: 'A3: adopt and release keep their answer' },
  { key: 'chat-2489', title: 'A4: evidence and the capture harness' },
]

/** One unrelated chat, for scale: it was nobody's child and stays a root in every frame. */
const UNRELATED: LocalSidebarRow = localRow({
  key: 'chat-2475',
  title: 'Dynamic dashboard progress check',
  messages: 64,
  last_ts: at(26 * MIN),
  last_message: 'Phase 2 capture landed.',
})

const minted = params.get('minted') === '1'
const ran = params.get('ran') === '1'

const workers: LocalSidebarRow[] = MINTED.map(({ key, title }, i) =>
  localRow({
    key,
    title,
    agent: 'kirocrew-worker',
    // Before its first turn a worker has sent nothing and is not running: that is
    // precisely why it has no crew log, and so no node in the fold.
    messages: ran ? 6 + i : 0,
    running: ran,
    last_ts: at(ran ? 15_000 + i * 4000 : 8_000 + i * 900),
    last_message: ran ? 'Working. First turn underway.' : '',
    // Both fields ride on the row either way; what changes is the server's answer.
    created_by: LEAD,
    lineage_minted: true,
    // `_attach_slot_parents` resolves this. Before the change a worker with no node
    // got null however it was minted; after it, the mint witness answers -- and once
    // the worker has run, the SAME edge comes from its own crew log instead.
    parent: minted ? { slot: LEAD, key: LEAD } : null,
  }),
)

mountLocalSidebar(
  [LEAD_ROW, ...workers, UNRELATED],
  ['kirocrew', 'kirocrew-worker', 'kirocrew-lead'],
)
