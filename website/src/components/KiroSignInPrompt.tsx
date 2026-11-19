import { useQuery } from '@tanstack/react-query'

import { api, type KasBundleStatus } from '../api/client'
import { ACP_BACKEND_KAS, type AcpBackendConfig } from '../api/acpBackend'
import { useKasLoginStatus } from '../hooks/useKasLogin'
import { i18nT } from '../i18n/t'
import { KiroSignInStrip } from './KiroSignInDialog'

/**
 * The Kiro sign-in, offered above the chat composer BEFORE a message fails.
 *
 * Shown only when the gateway says a new chat would fail for lack of Kiro
 * Crew's own sign-in (`chat_needs_sign_in` on `GET /api/kas-bundle`: KAS is the
 * main harness, its next spawn takes the bundle route, no usable sign-in is
 * stored, and the caller is the owner who can sign in). The dashboard never
 * guesses the route itself, so the kiro-cli relay, where kiro-cli's own login
 * starts the chat, never sees it.
 *
 * One line and a Sign in button that opens the shared sign-in dialog
 * (`KiroSignInDialog`). It goes away the moment the stored sign-in reads as
 * authenticated.
 */
export default function KiroSignInPrompt() {
  // The same entries the app-level engine notice reads (`KasEngineNotice`), so
  // this adds no request of its own while that notice is mounted.
  const cfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const kas = cfgQuery.data?.agent?.acp_backend === ACP_BACKEND_KAS
  const bundleQuery = useQuery<KasBundleStatus>({
    queryKey: ['kas-bundle'],
    queryFn: () => api.kasBundleStatus(),
    enabled: kas,
    retry: false,
  })
  const needed = kas && bundleQuery.data?.chat_needs_sign_in === true
  // A sign-in that lands in the dialog re-reads this entry; a terminal one is
  // seen within a minute.
  const status = useKasLoginStatus({ enabled: needed, poll: 60_000 })
  if (!needed || status.data?.authenticated === true) return null
  return (
    // Its own layer, the same as the status stack's bars beside it
    // (ChatPage.statusStackLayering.test.tsx pins this wrapper's shape).
    <div className="px-4 mx-auto w-full relative z-[2]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="mb-1.5" data-testid="kiro-sign-in-prompt">
        <KiroSignInStrip title={i18nT('components.kiroSignInPrompt.title')} />
      </div>
    </div>
  )
}
