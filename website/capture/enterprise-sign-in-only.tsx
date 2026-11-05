/**
 * Isolated capture entry for the enterprise sign-in-only screen.
 *
 * Mounts the REAL KiroPrerequisiteGate against the real stylesheet, theme tokens
 * and i18n catalog, with the prerequisite status a signed-out enterprise gateway's
 * sign-in-only app answers (`enterprise_sign_in_only: true`). Only the API calls
 * are stubbed.
 *
 * Scene from the query string: ?scene=signed-out|signed-in|restart&theme=dark|light&lang=en
 *   signed-out: the Company SSO form the user signs in with.
 *   signed-in:  the moment between the sign-in landing and the gateway saying so.
 *   restart:    "Signed in. Start Kiro Crew again." once the sign-in landed.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import KiroPrerequisiteGate from '../src/components/KiroPrerequisiteGate'
import { api } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'signed-out'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(lang)

// What the sign-in-only app's synthetic GET /api/kiro-prerequisite answers. The
// `restart` scene is its answer once a sign-in landed, just before it exits.
api.kiroPrerequisite = () =>
  Promise.resolve({
    ready: false,
    authenticated: false,
    installed: true,
    initial_setup_complete: false,
    setup_allowed: true,
    enterprise_login_required: true,
    enterprise_sign_in_only: true,
    enterprise_signed_in_restart: scene === 'restart',
    ...(scene === 'restart' ? { enterprise_start_command: '/usr/local/bin/kirocrew gateway' } : {}),
  }) as unknown as ReturnType<typeof api.kiroPrerequisite>
api.kasLoginStatus = () =>
  Promise.resolve(
    scene !== 'signed-out'
      ? { authenticated: true, provider: 'idc', identity: 'identity_center', transport: 'device' }
      : { authenticated: false, provider: null, identity: null, transport: 'device' },
  ) as unknown as ReturnType<typeof api.kasLoginStatus>
api.kasLoginPoll = () =>
  Promise.resolve({ status: 'pending' }) as ReturnType<typeof api.kasLoginPoll>

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <KiroPrerequisiteGate>
      <div>dashboard</div>
    </KiroPrerequisiteGate>
  </QueryClientProvider>,
)
