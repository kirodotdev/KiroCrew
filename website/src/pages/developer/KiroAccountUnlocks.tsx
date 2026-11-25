import { useQuery } from '@tanstack/react-query'
import { CheckCircle2, Dot, Minus, Sparkles } from 'lucide-react'
import { api } from '../../api/client'
import { ACP_BACKEND_KAS, acpBackendName, configuredAcpBackend, kasInUse, type AcpBackendConfig } from '../../api/acpBackend'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsLink } from '../../components/SettingsLink'
import { reportForError } from '../../utils/errorReport'
import { Badge, Card, CardTitle } from '../../components/ui'
import { useKasLoginStatus } from '../../hooks/useKasLogin'
import { i18nT } from '../../i18n/t'
import { AGENT_HARNESS_SETTINGS_TAB } from './kiroSignInLink'
import { signInLapsed } from './kiroSignInState'

/**
 * The features a Kiro sign-in unlocks. Only ones every build that offers the
 * sign-in can use: a feature that needs extra setup (an account type, an admin
 * switch) or is not shipped yet is left out, never shown as "coming soon".
 * Both rows need a working sign-in. Kiro Agent also counts Bots that run on it;
 * Kiro models needs Kiro Agent as the main harness, the one the picker reads.
 */
const ROWS = [
  {
    id: 'kiro-agent',
    name: 'pages.developer.kiroAccountUnlocks.kiro_agent',
    pitch: 'pages.developer.kiroAccountUnlocks.kiro_agent_pitch',
    on: 'pages.developer.kiroAccountUnlocks.kiro_agent_on',
    missing: 'pages.developer.kiroAccountUnlocks.kiro_agent_missing',
  },
  {
    id: 'kiro-models',
    name: 'pages.developer.kiroAccountUnlocks.kiro_models',
    pitch: 'pages.developer.kiroAccountUnlocks.kiro_models_pitch',
    on: 'pages.developer.kiroAccountUnlocks.kiro_models_on',
    missing: 'pages.developer.kiroAccountUnlocks.kiro_models_missing',
  },
] as const

/** Signed out: the short list that sits under the sign-in choices. */
export function KiroAccountUnlocksList() {
  return (
    <aside
      aria-labelledby="kiro-account-unlocks-heading"
      data-testid="kiro-account-unlocks-list"
      className="min-w-0 md:max-w-xs"
    >
      <p id="kiro-account-unlocks-heading" className="mb-2 text-[12px] font-semibold text-muted">
        {i18nT('pages.developer.kiroAccountUnlocks.list_heading')}
      </p>
      <ul className="space-y-2.5">
        {ROWS.map(row => (
          <li key={row.id} className="flex items-start gap-2">
            <Dot className="lucide-inline mt-0.5 shrink-0 text-muted" aria-hidden="true" />
            <span className="min-w-0">
              <span className="block text-[13px] font-medium text-text-strong">{i18nT(row.name)}</span>
              <span className="block text-[12px] leading-relaxed text-muted">{i18nT(row.pitch)}</span>
            </span>
          </li>
        ))}
      </ul>
    </aside>
  )
}

/**
 * Signed in: one row per feature, reading what works with the current settings.
 * A row reads Active only when it works now; otherwise it reads Not active and
 * says what is missing. Shown once both the sign-in status and the config have
 * loaded, so no row is a guess.
 * The sign-in card above owns the status poll and every sign-in error.
 */
export function KiroAccountUnlocksCard() {
  const { data: status } = useKasLoginStatus()
  const cfgQuery = useQuery<AcpBackendConfig>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const cfg = cfgQuery.data
  if (!status?.authenticated) return null
  const lapsed = signInLapsed(status)
  if (cfg === undefined) {
    // A failed read with nothing cached says so: the app-level config notice is
    // hidden on this tab. A lapsed sign-in leaves it to the sign-in card above,
    // which shows the same notice then. askAgent on: this card holds no draft.
    return cfgQuery.isError && !lapsed ? (
      <ErrorNotice
        // The width of the Settings cards beside it (Card's grouped frame).
        className="mb-4 max-w-[720px]"
        askAgent
        title={i18nT('components.kiroSignInNotice.config_unavailable')}
        message={i18nT('components.kiroSignInNotice.config_unavailable_detail')}
        report={reportForError(cfgQuery.error)}
        testId="kiro-account-unlocks-config-error"
      />
    ) : null
  }
  const backend = configuredAcpBackend(cfg)
  const mainOnKas = backend === ACP_BACKEND_KAS
  const harness = acpBackendName({ id: backend })
  // Kiro Agent counts Bots on KAS too (`kasInUse`, the sign-in card's rule);
  // Kiro models follows the main harness, the one the model picker reads.
  // A row is on when it would be with a working sign-in and the sign-in works.
  const wouldBeOn = (id: string) => (id === 'kiro-agent' ? kasInUse(cfg) : mainOnKas)
  const rowOn = (id: string) => !lapsed && wouldBeOn(id)
  const onLine = (row: (typeof ROWS)[number]) =>
    row.id === 'kiro-agent' && !mainOnKas
      ? i18nT('pages.developer.kiroAccountUnlocks.kiro_agent_bots', { harness })
      : i18nT(row.on)
  return (
    <Card data-testid="kiro-account-unlocks-card">
      <CardTitle>
        <Sparkles className="lucide-inline" aria-hidden="true" />
        {i18nT('pages.developer.kiroAccountUnlocks.card_title')}
      </CardTitle>
      <ul className="divide-y divide-border">
        {ROWS.map(row => { const on = rowOn(row.id); return (
          <li
            key={row.id}
            data-testid={`kiro-account-unlocks-${row.id}`}
            data-state={on ? 'on' : 'missing'}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5 first:pt-0 last:pb-0"
          >
            {on
              ? <CheckCircle2 className="lucide-inline mt-0.5 shrink-0 self-start text-ok-text" aria-hidden="true" />
              : <Minus className="lucide-inline mt-0.5 shrink-0 self-start text-muted" aria-hidden="true" />}
            <span className="min-w-0 flex-[1_1_14rem]">
              <span className="block text-[13px] font-medium text-text-strong">{i18nT(row.name)}</span>
              <span className="block text-[12px] leading-relaxed text-muted">
                {/* Sign in again only where a fresh sign-in alone turns the row on;
                    otherwise the harness is still missing, so say that. */}
                {on
                  ? onLine(row)
                  : lapsed && wouldBeOn(row.id)
                    ? i18nT('pages.developer.kiroAccountUnlocks.sign_in_again')
                    : i18nT(row.missing, { harness })}
              </span>
            </span>
            {!mainOnKas && row.id === 'kiro-agent' && (
              // One link, on the row that names the main harness: Kiro models
              // only follows from it.
              <SettingsLink tab={AGENT_HARNESS_SETTINGS_TAB} className="rounded text-[13px] font-medium text-accent hover:underline focus-ring-accent">
                {i18nT('pages.developer.kiroAccountUnlocks.change_in_agent_harness')}
              </SettingsLink>
            )}
            {/* Every row carries a status word, so the off state never reads as a feature. */}
            {on
              ? <Badge variant="ok">{i18nT('pages.developer.kiroAccountUnlocks.on')}</Badge>
              : <Badge variant="muted">{i18nT('pages.developer.kiroAccountUnlocks.not_active')}</Badge>}
          </li>
        ) })}
      </ul>
    </Card>
  )
}
