/**
 * Evidence for the share dialog's `sites` list (`capabilities.social_share`).
 *
 * Mounts the REAL `ShareMessageModal` through the REAL api client. Only the
 * transport is stubbed: one `fetch` shim answers `GET /api/dashboard/config`,
 * because there is no gateway behind a capture page.
 *
 * Scenes, selected with `?scene=`:
 *
 *   ?scene=all     -- no policy list: both site buttons, as before the list existed.
 *   ?scene=one     -- the policy names `linkedin` only: the X button is not drawn.
 *   ?scene=failed  -- the config read fails: no site button, and the inline notice
 *                     says why. Download and copy stay available.
 *   ?scene=loading -- the read has not answered yet: both buttons drawn disabled.
 *
 * Query string also takes ?theme=dark|light.
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import ShareMessageModal from '../src/pages/chat/share/ShareMessageModal'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
const scene = params.get('scene') || 'all'

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/dashboard/config')) {
    if (scene === 'loading') return new Promise<Response>(() => {})
    if (scene === 'failed') return new Response('{"error":"unavailable"}', { status: 503 })
    const sites = scene === 'one' ? ['linkedin'] : ['x', 'linkedin']
    return new Response(JSON.stringify({ social_share_enabled: true, social_share_sites: sites }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  return realFetch(input, init)
}

createRoot(document.getElementById('root')!).render(
  <ShareMessageModal
    onClose={() => {}}
    messageText="Triaged 47 issues overnight and opened two PRs."
    prevUserText="How did tonight go?"
    shareEnabled
  />,
)
