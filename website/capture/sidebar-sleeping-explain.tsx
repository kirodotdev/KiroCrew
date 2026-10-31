/**
 * Capture page for the "Show 1 sleeping session" expander in a folder: the REAL
 * `ChatSidebar` in the tree lane, a "Demos" folder holding one fresh session and
 * one untouched for ten days, so the default 7-day collapse folds the old one
 * behind the expander. The before/after difference comes from which version of
 * the sidebar is on disk; this page only supplies the rows and the folder.
 *
 * Query string: ?theme=dark|light
 */
import { MIN, at, localRow, mountLocalSidebar, prepareLocalSidebar } from './localSidebarFixture'

const params = new URLSearchParams(location.search)
prepareLocalSidebar(params)
localStorage.setItem('mc-sidebar-lane', 'tree')
localStorage.setItem('mc-sidebar-width', '320')
// The subject is the collapse, so it runs at its default threshold.
localStorage.removeItem('mc-session-stale-collapse-ms')

const DAY = 24 * 60 * MIN
const FOLDERS = [{ id: 'f-demos', name: 'Demos', order: 0 }]

// Every API read is answered here, so the page never reaches a gateway the dev
// server may proxy to: the folder tree, and an empty answer for the rest.
const inner = window.fetch.bind(window)
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }))
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = url.startsWith('http') ? new URL(url).pathname : url.split('?')[0]
  if (path.endsWith('/api/chat/folders')) return json(FOLDERS)
  if (path.startsWith('/api/shared-crew/') || path.startsWith('/api/auth/')) return json({})
  if (path.startsWith('/api/')) return json([])
  return inner(input, init)
}) as typeof window.fetch

mountLocalSidebar([
  localRow({ key: 'c-1', title: 'Fix the login redirect', last_ts: at(2 * MIN), last_message: 'Tests pass.' }),
  { ...localRow({ key: 'c-2', title: 'Record the onboarding demo', last_ts: at(20 * MIN), last_message: 'Take two is ready.' }), folder_id: 'f-demos' },
  { ...localRow({ key: 'c-3', title: 'Demo script for the launch', last_ts: at(10 * DAY), last_message: 'Outline drafted.' }), folder_id: 'f-demos' },
  localRow({ key: 'c-4', title: 'Explain the CDK stack', last_ts: at(45 * MIN), last_message: 'Four stacks, one account each.' }),
], ['kirocrew'])
