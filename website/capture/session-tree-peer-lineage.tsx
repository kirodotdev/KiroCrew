/**
 * Isolated capture entry for the conductor lane over FEDERATED rows: sessions a
 * connected crew ("worker-1") lists through `GET /api/instances/{id}/chat-slots`,
 * photographed through the real `ChatSidebar` and the real `useInstanceSessions`.
 *
 * WHY ISOLATED: the defect needs a second gateway mid-dispatch behind a live tunnel.
 * What stays faithful is the wire: the harness answers the hub's chat-slots route with
 * the shape the hub sends the browser, and nothing downstream is stubbed -- the hook
 * maps the rows, the lane resolves `parent.key` within the peer's own origin, draws
 * the chevron, the count and the glyphs on its own.
 *
 * Query string: ?theme=dark|light
 *               &parent=1  -- the chat-slots reply carries `parent: {slot, key}` as the
 *                             hub now forwards it: the peer conductor is one row with its
 *                             worker count, and opens to nest them. Without it the reply
 *                             carries no `parent`, which is what `_clean_peer_slot` sent
 *                             before this change: every remote worker a top-level stray.
 *                             A query parameter rather than a window hook because the two
 *                             payloads never meet in one product session, and swapping
 *                             them on a mounted page reads as a re-parent (see the
 *                             harness).
 */
import {
  MIN, at, installPeerFetch, mountPeerSidebar, prepareSidebarPreferences, stripParent,
  type LocalRow, type PeerRow,
} from './peerSidebarFixture'

const params = new URLSearchParams(location.search)
prepareSidebarPreferences(params)

const PEER = 'worker-1'
const LEAD = 'chat-2201'
const WORKERS = ['chat-2202', 'chat-2203', 'chat-2204']
const peerRow = (key: string, title: string, agent: string, msAgo: number, extra: Partial<PeerRow> = {}): PeerRow => ({
  key, title, agent, running: false, pending_approval: false,
  last_turn_ts: at(msAgo), last_ts: at(msAgo), created: at(msAgo + 30 * MIN),
  row_identity: `${PEER}:${key}`,
  ...extra,
})
const cite = { parent: { slot: LEAD, key: LEAD } }
const PEER_ROWS: PeerRow[] = [
  peerRow(LEAD, 'Fix PR readiness commit status', 'kirocrew-lead', 2 * MIN, { running: true }),
  peerRow(WORKERS[0], 'Resolving issue with PR readiness check', 'kirocrew-worker', 40_000, { running: true, ...cite }),
  peerRow(WORKERS[1], 'PR Readiness Status Fix', 'kirocrew-worker', 3 * MIN, { pending_approval: true, ...cite }),
  peerRow(WORKERS[2], 'Fix PR Readiness commit status', 'kirocrew-worker', 6 * MIN, cite),
]

/** The hub's own two local sessions, for scale. */
const LOCAL: LocalRow[] = [
  { key: 'chat-2190', title: 'Session tree mode worker grouping bug', messages: 41, running: false, agent: 'kirocrew', last_ts: at(MIN), last_message: 'Both hops now forward the citation.' },
  { key: 'chat-2188', title: 'Fargate feature progress check', messages: 17, running: true, agent: 'kirocrew', last_ts: at(4 * MIN), last_message: 'Loop 17/40 · Update monitor.' },
]

installPeerFetch(PEER, params.get('parent') === '1' ? PEER_ROWS : stripParent(PEER_ROWS))
mountPeerSidebar(LOCAL)
