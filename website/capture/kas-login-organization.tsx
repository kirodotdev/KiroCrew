/**
 * Isolated capture entry for the Kiro sign-in chooser (the organization sign-in).
 *
 * WHY ISOLATED: the chooser only appears to a signed-out user on a gateway
 * that answers the sign-in status, which needs a live gateway plus a dashboard
 * credential. This mounts the REAL sign-in flow in both chromes against the
 * real stylesheet and theme tokens; every /api/ call is answered by the capture
 * script's route interception (scripts/capture-24639-organization-sign-in.mjs).
 *
 * `chrome=account` mounts the real account modal for a stored organization
 * sign-in, so the label it shows can be compared with the chooser's.
 *
 * Query string: ?theme=dark|light&chrome=card|gate|account
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import KasLoginGate, { KasLoginEmbedded } from '../src/components/KasLoginGate'
import KiroAccountModal from '../src/components/KiroAccountModal'
import { parseKiroUsagePayload } from '../src/api/kiroUsage'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const chrome = params.get('chrome') || 'card'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <div className="min-h-screen bg-bg p-8 text-text" data-capture-root>
      {chrome === 'account' ? (
        <KiroAccountModal
          open
          onClose={() => undefined}
          usage={parseKiroUsagePayload({
            usage: { available: false, reason: 'account_unproven', account_type: 'ExternalIdP' },
          })}
        />
      ) : chrome === 'gate' ? (
        <KasLoginGate />
      ) : (
        <div className="max-w-2xl rounded-xl border border-border bg-bg-elevated p-5">
          <KasLoginEmbedded renderAuthenticated={() => null} />
        </div>
      )}
    </div>
  </QueryClientProvider>,
)
