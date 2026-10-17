/**
 * Isolated capture entry for the conductor lane over a crew whose CONDUCTOR WAS CLOSED
 * while its workers kept running, photographed through the real `ChatSidebar`.
 *
 * WHY ISOLATED: posing this live means closing a middle session and leaving three
 * workers mid-turn under it, which is a state a capture run cannot hold still. What
 * stays faithful is the wire: the rows below are what the slots payload carries, and
 * the lane does its own work on them -- it resolves `parent.key`, places the rows,
 * draws the chevron, the count and the citation glyph.
 *
 * `parent` here is what `_attach_slot_parents` computes server-side. The conductor is
 * absent from the payload because it is closed, so its workers cite a slot no row
 * carries; what the query parameter switches is whether the server walked up to the
 * lead, which is the change under review. That the walk produces this answer is pinned
 * in `test/test_crew_log_session_tree.py` and `test/test_slot_payload_lineage.py`.
 *
 * Query string: ?theme=dark|light
 *               &reparent=1 -- the payload resolves each worker to the nearest ANCESTOR
 *                              still open and says so with `ancestor`. The lead is one
 *                              row with its worker count, SHUT by default, and each
 *                              worker keeps the closed-creator glyph once opened.
 *                              Without it every worker carries a null key, which is
 *                              what the sidebar received before: three top-level rows
 *                              wearing the orphan glyph while the lead that owns the
 *                              run sits two rows away.
 *
 * Usage: node scripts/capture-session-tree-closed-conductor.mjs [devBase] [outDir]
 */
import {
  MIN, at, localRow, mountLocalSidebar, prepareLocalSidebar, type LocalSidebarRow,
} from './localSidebarFixture'

const params = new URLSearchParams(location.search)
prepareLocalSidebar(params)

/** The run, as it was really shaped: a lead, a conductor under it, three workers. */
const LEAD = 'chat-2548'
const CONDUCTOR = 'chat-2552'

/** Still open, and still the session that owns the run. */
const LEAD_ROW: LocalSidebarRow = localRow({
  key: LEAD,
  title: 'Remote crew lane: drive the three PRs to green',
  agent: 'kirocrew-lead',
  running: true,
  messages: 164,
  last_ts: at(35_000),
  last_message: 'Conductor closed. Three workers still reporting.',
})

/** The workers the closed conductor dispatched. Each one cites it and nothing else. */
const CREW: Array<{ key: string; title: string; note: string; waiting?: boolean }> = [
  { key: 'chat-2554', title: 'I1: MicroVM boot path and the lease', note: 'Checks green. Waiting on review.' },
  { key: 'chat-2555', title: 'I2: Fargate task definition', note: 'Rebased onto main. Re-running CI.', waiting: true },
  { key: 'chat-2556', title: 'I3: token handoff to the remote crew', note: 'Working. Writing the harness.' },
]

/** A chat from another day, for scale: nobody opened it and it is a root in every frame. */
const SEPARATE: LocalSidebarRow = localRow({
  key: 'chat-2531',
  title: 'Crew log retention sweep',
  messages: 48,
  last_ts: at(52 * MIN),
  last_message: 'Sweep finished. 11 units removed.',
})

const reparented = params.get('reparent') === '1'

const crew: LocalSidebarRow[] = CREW.map(({ key, title, note, waiting }, i) =>
  localRow({
    key,
    title,
    agent: 'kirocrew-worker',
    running: !waiting,
    needs_input: waiting,
    messages: 24 + i * 7,
    last_ts: at(12_000 + i * 5_000),
    last_message: note,
    // The citation never moves: it is this worker's own crew-log fact, and the
    // conductor really did open it. Only `key` is the server's answer, and only
    // `ancestor` says the two now name different sessions.
    parent: reparented
      ? { slot: CONDUCTOR, key: LEAD, ancestor: true }
      : { slot: CONDUCTOR, key: null },
  }),
)

mountLocalSidebar(
  [LEAD_ROW, ...crew, SEPARATE],
  ['kirocrew', 'kirocrew-worker', 'kirocrew-lead'],
)
