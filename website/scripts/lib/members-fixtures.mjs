/**
 * Roster fixture shared by the Crew Members capture scripts
 * (capture-members-page.mjs, capture-members-steer-only.mjs): four members,
 * radar working. One copy, so the frames of different PRs photograph the
 * same crew and the copy-paste gate has nothing to find.
 */
export const MEMBERS = [
  { name: 'radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: true, kiro_agent: 'kirocrew-autofix', workspace: 'autofix', memory_store: 'default', model: '', last_active_ts: 1000, last_message: 'Six new issues: four covered by open PRs.' },
  { name: 'scout', slug: 'scout', bound: false, slot_key: '', running: false, kiro_agent: 'kirocrew-research', workspace: 'default', memory_store: 'scout-own', model: 'claude-opus-5' },
  { name: 'fixer', slug: 'fixer', bound: true, slot_key: 'member-fixer', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', last_active_ts: 900, last_message: 'Two PRs opened for the queue.' },
  { name: 'scribe', slug: 'scribe', bound: false, slot_key: '', running: false, kiro_agent: 'kirocrew-lite', workspace: 'docs', memory_store: 'default', model: '' },
]

/**
 * Gateway-free Members-page API stub shared by the capture harnesses: answers
 * every REAL call the page, its ChatPane and the drawer make. `slotDetail` is
 * what `/api/chat/slots/<key>` returns (the thread's messages + run state);
 * `members` overrides the roster (default: MEMBERS). Array-shaped endpoints
 * answer [] because `{}` crashes their `.map`.
 */
export function routeMembersApi(page, slotDetail, { members = MEMBERS } = {}) {
  return page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') return json({ members, default_agent: 'kirocrew' })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return json({ slot_key: `member-${slug}`, slug, member: slug, created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'radar', member: 'radar', capped: false, entries: [] })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) return json(slotDetail)
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
}
