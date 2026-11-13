import { useQuery } from '@tanstack/react-query'
import { KeyRound } from 'lucide-react'

import { api, type KasBundleStatus } from '../api/client'
import { ACP_BACKEND_KAS, type AcpBackendConfig } from '../api/acpBackend'
import { useKasLoginStatus } from '../hooks/useKasLogin'
import { i18nT } from '../i18n/t'
import { KasLoginEmbedded } from './KasLoginGate'

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
 * The body is the shared sign-in flow (`KasLoginEmbedded`, the one Settings >
 * Agent Harness renders), so the person signs in right here. It goes away the
 * moment the stored sign-in reads as authenticated.
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
  // Observed, not polled: the flow below polls it while it is mounted.
  const status = useKasLoginStatus({ enabled: needed })
  if (!needed || status.data?.authenticated === true) return null
  return (
    // Its own layer, the same as the status stack's bars beside it
    // (ChatPage.statusStackLayering.test.tsx pins this wrapper's shape).
    <div className="px-4 mx-auto w-full relative z-[2]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <section
        data-testid="kiro-sign-in-prompt"
        className="mb-1.5 rounded-xl border border-border bg-card p-4"
        aria-label={i18nT('components.kiroSignInPrompt.title')}
      >
        <h2 className="m-0 flex items-center gap-1.5 text-[14px] font-semibold text-text-strong">
          <KeyRound className="lucide-inline" aria-hidden="true" />
          {i18nT('components.kiroSignInPrompt.title')}
        </h2>
        <div className="mt-2">
          <KasLoginEmbedded
            intro={i18nT('components.kiroSignInPrompt.intro')}
            renderPending={() => null}
            renderAuthenticated={() => null}
          />
        </div>
      </section>
    </div>
  )
}
