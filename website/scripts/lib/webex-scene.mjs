/**
 * The Webex settings scene shared by this branch's two capture harnesses.
 *
 * One module rather than a copy in each script: the fixture, the governance
 * answer and the panel navigation are identical for the desktop and narrow
 * captures, and `jscpd` runs at a 0% threshold here — so a second copy is a
 * build failure, not a style note.
 */
import { json, handleBootRoute } from './boot-api.mjs'

/** Mutable scene the panel renders. Read at request time, so a harness can flip
 *  a field between shots without rebuilding the route table. */
export const scene = { theme: 'dark', allowGroupRooms: false, rooms: [] }

/** The `/api/webex/config` payload, reflecting the current scene. */
export const webexConfig = () => ({
  connected: true,
  connect_error: '',
  configured: true,
  read_only: false,
  bot_token_set: true,
  bot_token_preview: 'Yzg4…9f2a',
  enabled: true,
  allowed_emails: ['kyle@example.com'],
  allow_group_rooms: scene.allowGroupRooms,
  allowed_room_ids: scene.rooms,
  reply_in_thread: true,
  soft_threshold_pct: 80,
  hard_threshold_pct: 95,
  session_folder: '',
})

/** Every channel permitted, so the editable panel renders.
 *
 *  Load-bearing: the panel shows its config only on a CONFIRMED allow, and an
 *  unknown policy deliberately renders a "policy status unavailable" notice
 *  instead — which is what a capture without this would screenshot. */
const ALL_CHANNELS_ALLOWED = {
  slack: true, discord: true, telegram: true, webex: true,
  wecom: true, teams: true, weixin: true,
}

/** Install the route table: Webex config, governance, then the shared boot arm. */
export function installWebexRoutes(page) {
  return page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/webex/config') return json(route, webexConfig())
    if (path === '/api/governance/channels') return json(route, ALL_CHANNELS_ALLOWED)
    return handleBootRoute(route, path, { project: '/tmp/demo', theme: scene.theme })
  })
}

/** Open the Webex settings panel on a fresh page, fully stubbed. */
export async function openWebexPanel(context, base) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await installWebexRoutes(page)
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))
  await page.addInitScript(t => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-privacy-notice-v1', '1')
  }, scene.theme)
  await page.goto(`${base}/settings?tab=channels&channel=webex`, {
    waitUntil: 'domcontentloaded',
  })
  await page.waitForTimeout(2600)
  return page
}
