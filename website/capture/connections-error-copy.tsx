/**
 * Isolated capture entry for the needs-attention banner copy fix.
 *
 * WHY ISOLATED: the banner renders only after a live provider probe fails, and
 * reaching it in the full app needs an authorized provider whose MCP endpoint
 * then times out — an unreproducible network state. The frames below render the
 * banner fragment with the REAL translation catalog and the REAL
 * `errorIndicatesProviderRejection` classifier, so what is captured is the
 * shipping copy path, not a mockup.
 *
 * Three labeled rows:
 *   BEFORE — origin/main behavior reconstructed verbatim: the provider-verdict
 *            copy rendered unconditionally, here over a literal "timeout"
 *            detail (the contradiction the user reported).
 *   AFTER (timeout) — the classifier routes transport noise to the honest
 *            could-not-reach copy; detail and Reconnect affordance unchanged.
 *   AFTER (invalid_grant) — auth-shaped evidence keeps the verdict copy.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { AlertTriangle } from 'lucide-react'

// `../src/i18n/all` registers every language catalog (plain `../src/i18n` is
// English-only), as the shared entry contract requires of every capture.
import { initI18n } from '../src/i18n/all'
import i18next from 'i18next'
import { errorIndicatesProviderRejection } from '../src/pages/connections/ConnectionsPage'
import ErrorNotice from '../src/components/ErrorNotice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

await initI18n()
const t = i18next.getFixedT(null, null)

function Banner({ headline, detail }: { headline: string; detail: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md bg-danger-subtle p-2.5 text-[12px] text-danger" style={{ width: 460 }}>
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>
        {headline}
        <span className="mt-1 block text-[11px] text-muted">{detail}</span>
      </span>
    </div>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
      {children}
    </div>
  )
}

/** The AFTER path: exactly the expression the card now evaluates. */
function afterHeadline(detail: string): string {
  return t(
    errorIndicatesProviderRejection(detail)
      ? 'pages.connectionsPage.connection_invalid'
      : 'pages.connectionsPage.connection_unreachable',
    { provider: 'GitLab' },
  )
}

createRoot(document.getElementById('root')!).render(
  <div data-capture-ready className="min-h-screen space-y-5 bg-surface p-6 text-fg">
    <Row label="Before (main): provider verdict claimed over a timeout">
      <Banner headline={t('pages.connectionsPage.connection_invalid', { provider: 'GitLab' })} detail="timeout" />
    </Row>
    <Row label="After: transport noise gets the honest could-not-reach copy (ErrorNotice)">
      <div style={{ width: 460 }}><ErrorNotice title={afterHeadline('timeout')} message="timeout" askAgent /></div>
    </Row>
    <Row label="After: auth-shaped evidence keeps the verdict copy (ErrorNotice)">
      <div style={{ width: 460 }}><ErrorNotice title={afterHeadline('invalid_grant')} message="invalid_grant" askAgent /></div>
    </Row>
  </div>,
)
