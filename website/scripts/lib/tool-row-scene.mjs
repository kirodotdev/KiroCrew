/**
 * The shared "mid-turn shell tool" scene for the tool-row harnesses.
 *
 * One session, a few turns of scrollback, and a turn already two tool calls in
 * with the second one just finished -- enough transcript ABOVE the working end
 * to overflow the viewport, which is what makes the transcript bottom-pinned.
 * The harnesses that use it then drive a THIRD shell tool over the WebSocket
 * (row mounts, status line, completion) and observe the transcript around it.
 *
 * `makeToolRowScene` returns the `/api/chat/slots` list and the slot detail the
 * static server answers with; `openToolRowScene` binds the routes, seeds the
 * SPA's localStorage, navigates, and waits for the scene to be on screen. The
 * WebSocket handle is returned so the caller can push frames as the backend
 * would.
 */
import { json, makeFixedApi, handleBootRoute } from './boot-api.mjs'

const TOOL_ROW_PROJECT = '/home/user/workspace/demo-app'
export const TOOL_ROW_VIEW = { width: 1180, height: 720 }

/** Purpose text of the last tool bubble in the fixture; the scene is on screen
 *  once this is visible (the pill shows the agent's PURPOSE, not the raw tool
 *  label, since `simplifiedToolNames` defaults on). */
const TOOL_ROW_LAST_PURPOSE = 'Start a clean npm ci in the Node 22 container'

/** A tool bubble as the backend persists one: 🔧 + label, id on the meta. */
const tool = (id, label, purpose, ts) => ({
  role: 'tool',
  ts,
  content: `🔧 ${label}`,
  meta: { tool_call_id: id, purpose, output: 'done' },
})

export function makeToolRowScene(slotKey) {
  const now = Math.floor(Date.now() / 1000)

  const slots = [{
    key: slotKey,
    title: 'Reinstall the toolchain in the container',
    running: true,
    last_message: 'The shared node_modules is largely root-owned.',
    messages: 5,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: TOOL_ROW_PROJECT,
    modified: now,
    source_links: [],
    source_links_total: 0,
  }]

  const filler = [
    ['Set up the container toolchain for this worktree.', 'Host Node is 16 and the package floor is 22, so every install and test run goes through the container. I will keep the host untouched.'],
    ['Where do the dependencies come from?', 'The lockfile is identical to a sibling worktree, so the fastest path is reusing its install rather than downloading the tree again.'],
    ['Try that first then.', 'Attempting a hardlinked copy: same inodes, no extra disk, and it lands in seconds instead of minutes.'],
  ].flatMap(([q, a], i) => [
    { role: 'user', ts: now - 400 + i * 40, content: q },
    { role: 'assistant', ts: now - 390 + i * 40, content: a },
  ])

  const detail = {
    running: true,
    has_more: false,
    total: filler.length + 4,
    queue: [],
    project: TOOL_ROW_PROJECT,
    messages: [
      ...filler,
      { role: 'user', ts: now - 90, content: 'The worktree needs its own toolchain — set it up and keep me posted.' },
      tool('tc-a', 'Inspect the sibling worktree', 'Give the worktree a writable hardlinked node_modules', now - 70),
      {
        role: 'assistant',
        ts: now - 55,
        content: 'The shared `node_modules` in a sibling worktree is largely root-owned, so hardlinking into it is refused. Falling back to a clean install in the container.',
      },
      tool('tc-b', 'Reset the install directory', TOOL_ROW_LAST_PURPOSE, now - 30),
    ],
  }

  return { slots, detail }
}

/**
 * Bind the fixture routes on `page`, open the SPA at `base`, and wait for the
 * scene. Resolves with a `send(type, data)` that pushes one WebSocket frame
 * stamped with the slot, exactly as the gateway would.
 */
export async function openToolRowScene(page, { base, slotKey, scene }) {
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  const fixedApi = makeFixedApi(TOOL_ROW_PROJECT)
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, scene.slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, scene.detail)
    return handleBootRoute(route, path, { project: TOOL_ROW_PROJECT, theme: 'light', fixedApi })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  await page.addInitScript(s => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', s)
  }, slotKey)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`text=${TOOL_ROW_LAST_PURPOSE}`, { timeout: 20000 })
  await page.waitForTimeout(1500)
  if (!wsServer) throw new Error('websocket route never bound')

  return (type, data) => wsServer.send(JSON.stringify({ type, data: { slot: slotKey, ...data } }))
}
