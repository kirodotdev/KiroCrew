/**
 * Isolated capture entry for the conductor lane over a HUB-DRIVEN crew: a LOCAL lead
 * whose turns execute on a connected crew ("worker-1"), and the workers that lead
 * opened -- which live ON the peer and are listed back through
 * `GET /api/instances/{id}/chat-slots`. Photographed through the real `ChatSidebar`
 * and the real `useInstanceSessions`.
 *
 * WHY ISOLATED: the defect needs a second gateway with a local session bound to it
 * for execution, mid-dispatch, behind a live tunnel. What stays faithful is the wire:
 * the harness answers the hub's chat-slots route with the shape the hub sends the
 * browser, and nothing downstream is stubbed -- the hook maps the rows, the lane
 * resolves `parent.hub_key` among LOCAL rows and draws the chevron and count itself.
 *
 * Query string: ?theme=dark|light
 *               &hub=1  -- each worker's citation arrives as `parent: {slot, hub_key}`
 *                          naming the LOCAL lead, as the hub now rewrites a citation of
 *                          a slot it drives. Without it the reply carries no `parent`
 *                          on those rows, which is what `_clean_peer_parent` sent
 *                          before this change (the citation named the driven peer
 *                          slot and was dropped whole): every worker a top-level stray
 *                          beside the lead that opened it.
 */
import {
  MIN, at, installPeerFetch, mountPeerSidebar, prepareSidebarPreferences, stripParent,
  type LocalRow, type PeerRow,
} from './peerSidebarFixture'

const params = new URLSearchParams(location.search)
prepareSidebarPreferences(params)

const PEER = 'worker-1'
/** The LOCAL lead: a hub slot bound to the peer for execution. Its peer-side twin is
 *  filtered out of the chat-slots reply by the hub, so it never appears as a peer row. */
const LEAD = 'chat-2201'
const WORKERS = ['chat-2202', 'chat-2203', 'chat-2204', 'chat-2205']

const peerRow = (key: string, title: string, msAgo: number, extra: Partial<PeerRow> = {}): PeerRow => ({
  key, title, agent: 'kirocrew-worker', running: false, pending_approval: false,
  last_turn_ts: at(msAgo), last_ts: at(msAgo), created: at(msAgo + 30 * MIN),
  row_identity: `${PEER}:${key}`,
  ...extra,
})
const cite = { parent: { slot: LEAD, hub_key: LEAD } }
const PEER_ROWS: PeerRow[] = [
  peerRow(WORKERS[0], 'Windows backend test shard failure', 40_000, { running: true, ...cite }),
  peerRow(WORKERS[1], 'Backend adoption provenance fix', 3 * MIN, { pending_approval: true, ...cite }),
  peerRow(WORKERS[2], 'Fix PR Readiness commit status', 6 * MIN, cite),
  peerRow(WORKERS[3], 'Stale CSP on cached assets', 9 * MIN, cite),
]

/** The hub's own local sessions: the remote-executed lead, and one unrelated chat. */
const LOCAL: LocalRow[] = [
  {
    key: LEAD, title: 'Dispatch subsession workers from briefs', messages: 10, running: true, agent: 'kirocrew-lead',
    last_ts: at(MIN), last_message: 'Loop 81/220 · four workers out, waiting on their reports.',
    executor: 'remote', instance_id: PEER,
  },
  { key: 'chat-2188', title: 'Fargate feature progress check', messages: 17, running: false, agent: 'kirocrew', last_ts: at(12 * MIN), last_message: 'Loop 17/40 · Update monitor.' },
]

installPeerFetch(PEER, params.get('hub') === '1' ? PEER_ROWS : stripParent(PEER_ROWS))
mountPeerSidebar(LOCAL)
