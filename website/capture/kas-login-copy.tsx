/**
 * Isolated capture entry for the KAS login gate copy review.
 *
 * Mounts the REAL KasLoginGate against the real stylesheet, theme tokens and
 * live i18n catalog, stubbing only the three /api/kas-login calls so the gate
 * renders deterministically without a gateway.
 *
 * Scene from the query string: ?scene=chooser|device&theme=dark|light&lang=en|zh-CN
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import KasLoginGate from '../src/components/KasLoginGate'
import { api } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'chooser'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute(
  'data-theme',
  theme === 'light' ? 'kiro-light' : 'kiro-dark',
)

initI18n(lang)

// Stub the gate's API calls. The status answer keeps the gate closed
// (unauthenticated) and reports the transport the scene needs; the device
// answer is the frozen session the device scene renders; the loopback answer is
// the frozen listener the loopback scene waits on. `fallback` makes the loopback
// begin answer 409 so the gate degrades to the device flow with its notice.
const loopbackScene = scene === 'loopback' || scene === 'fallback'
const deviceSession = {
  login_id: 'capture-login-1',
  user_code: 'KWXR-VBTM',
  verification_uri_complete: 'https://app.kiro.dev/account/device?user_code=KWXR-VBTM',
  expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
}
const loopbackSession = {
  ...deviceSession,
  login_id: 'capture-loopback-1',
  user_code: '',
  verification_uri_complete: 'https://app.kiro.dev/signin?state=capture',
  auth_url: 'https://app.kiro.dev/signin?state=capture',
  port: 3128,
}
api.kasLoginStatus = () =>
  Promise.resolve({
    authenticated: false,
    identities: [],
    transport: loopbackScene ? 'loopback' : 'device',
  }) as unknown as ReturnType<typeof api.kasLoginStatus>
api.kasLoginBeginDevice = () =>
  Promise.resolve(deviceSession) as ReturnType<typeof api.kasLoginBeginDevice>
api.kasLoginBeginLoopback = () =>
  scene === 'fallback'
    ? Promise.reject(new Error('409 loopback_unavailable'))
    : (Promise.resolve(loopbackSession) as ReturnType<typeof api.kasLoginBeginLoopback>)
api.kasLoginCancel = () => Promise.resolve({ ok: true })
api.kasLoginPoll = () =>
  Promise.resolve({ status: 'pending' }) as ReturnType<typeof api.kasLoginPoll>
// The loopback scene would otherwise open a real portal tab from the capture browser.
window.open = () => null

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <KasLoginGate />
  </QueryClientProvider>,
)

// The device / loopback / fallback scenes are reached through the REAL wiring:
// the harness clicks the shipped Google button once the chooser is on screen.
if (scene === 'device' || loopbackScene) {
  const t = setInterval(() => {
    const btn = [...document.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Google'),
    )
    if (btn) {
      clearInterval(t)
      btn.click()
    }
  }, 100)
}
