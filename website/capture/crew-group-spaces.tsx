/**
 * Capture page: a crew group in the Sessions list, with the crew's own folders
 * (its spaces) nested inside it and its unfiled chats last.
 *
 * `?flat=1` sends the rows without `folder_id`, which is what the hub's
 * chat-slots route sent before this change: the same chats as one flat list.
 * `?folders=fail` answers the crew's folder read with a 502: the chats list
 * flat under the group's failure notice.
 */
import {
  MIN, at, installPeerFetch, mountPeerSidebar, prepareSidebarPreferences,
  type LocalRow, type PeerRow,
} from './peerSidebarFixture'

const params = new URLSearchParams(location.search)
prepareSidebarPreferences(params)
// The plain folder lane, not the conductor lane the shared prep picks.
localStorage.removeItem('mc-sidebar-lane')

const PEER = 'astro'
const flat = params.get('flat') === '1'
const foldersFail = params.get('folders') === 'fail'

const peerRow = (key: string, title: string, msAgo: number, folder?: string): PeerRow & { folder_id?: string } => ({
  key, title, agent: 'kirocrew', running: false, pending_approval: false,
  last_turn_ts: at(msAgo), last_ts: at(msAgo), created: at(msAgo + 30 * MIN),
  row_identity: `${PEER}:${key}`,
  ...(folder && !flat ? { folder_id: folder } : {}),
})

const PEER_ROWS = [
  peerRow('chat-31', 'Draft the REST auth guide', 2 * MIN, 'f-api'),
  peerRow('chat-32', 'Review webhook examples', 8 * MIN, 'f-api'),
  peerRow('chat-33', 'Docs site nav cleanup', 12 * MIN, 'f-docs'),
  peerRow('chat-34', 'Refund request triage', 15 * MIN, 'f-support'),
  peerRow('chat-36', 'Reply to login ticket', 22 * MIN, 'f-support'),
  peerRow('chat-35', 'Quick question about pnpm', 40 * MIN),
]

/** The crew's own folder tree, as its `api/chat/folders` lists it. */
const PEER_FOLDERS = [
  { id: 'f-docs', name: 'Docs', order: 0 },
  { id: 'f-api', name: 'API guides', order: 0, parent_id: 'f-docs' },
  { id: 'f-support', name: 'Customer Support', order: 1 },
  { id: 'f-empty', name: 'Archive', order: 2 },
]

// One LOCAL space too, so the shot sets a crew space beside a local one.
const LOCAL = [
  { key: 'chat-1', title: 'Local planning notes', messages: 4, running: false, agent: 'kirocrew', last_ts: at(5 * MIN), last_message: 'ok', folder_id: 'l-plan' },
] as LocalRow[]
const LOCAL_FOLDERS = [{ id: 'l-plan', name: 'Planning', order: 0 }]

// Answer the folder read first; the shared shim wraps this one for the rest.
const inner = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = url.split('?')[0]
  const peerFolders = path.endsWith(`/api/instances/${PEER}/proxy/api/chat/folders`)
  if (peerFolders && foldersFail) {
    return Promise.resolve(new Response(JSON.stringify({ error: 'the crew did not answer', code: 'proxy_upstream_error' }), { status: 502, headers: { 'content-type': 'application/json' } }))
  }
  const reply = peerFolders ? PEER_FOLDERS
    : path.endsWith('/api/chat/folders') ? LOCAL_FOLDERS : null
  if (reply) return Promise.resolve(new Response(JSON.stringify(reply), { status: 200, headers: { 'content-type': 'application/json' } }))
  return inner(input, init)
}) as typeof window.fetch

installPeerFetch(PEER, PEER_ROWS)
mountPeerSidebar(LOCAL)
