import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Cpu } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { api, type AcpBackendProbe } from '../api/client'
import { ACP_BACKEND_KIRO, type AcpBackendConfig } from '../api/acpBackend'
import { ApiError } from '../api/apiError'
import ErrorNotice from './ErrorNotice'
import SimpleSelect from './SimpleSelect'

/** Option value for "no pick": Kiro's own id is `''`, so the empty value is taken. */
const NO_PICK = '__default__'
/** Option value for Kiro, whose wire id `''` SimpleSelect reserves for "nothing". */
const KIRO_OPTION = '__kiro__'

/** Backend ids this frontend has a translated name for; any other shows its policy id. */
const NAME_KEYS: Record<string, string> = {
  kiro: 'pages.developer.agentBackendTab.kiro_cli',
  claude: 'pages.developer.agentBackendTab.claude_code',
  kas: 'pages.developer.agentBackendTab.kas_kiro_agent',
}

/** A probe failure that is an ANSWER rather than an outage: see the render below. */
function probeAnswerIsPermanent(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 403 || err.status === 404)
}

/** A backend a chat can be switched to: selectable here AND able to start a session. */
export function pickableBackends(rows: readonly AcpBackendProbe[]): AcpBackendProbe[] {
  return rows.filter(b => b.selectable && b.installed !== 'missing' && !b.restart_required)
}

/** Whether a switch that answered *changed* just cleared a model the user had pinned.
 *  A switch always clears the chat's model (an id belongs to one backend's catalog);
 *  `''` and `'auto'` were not pins, so losing them needs no notice. */
export function switchClearedModelPin(changed: boolean | undefined, priorModel: string | undefined): boolean {
  return !!changed && !!priorModel && priorModel !== 'auto'
}

/** The notice a finished switch shows: every real switch starts a fresh session,
 *  and one that cleared a pin says so too. `null` when nothing changed. */
export function switchNoticeKey(changed: boolean | undefined, priorModel: string | undefined): string | null {
  if (!changed) return null
  return switchClearedModelPin(changed, priorModel)
    ? 'components.backendPicker.model_pin_cleared'
    : 'components.backendPicker.switched_fresh_session'
}

interface BackendPickerProps {
  /** The chat's own pick: `null` = none (follows the default), `''` = Kiro. */
  value: string | null
  /** True when the pick is outside the selectable set, so the gateway runs the chat on
   *  Kiro (the slot's `acp_backend_degraded`). The pick's row then says so. */
  degraded?: boolean
  /** True while a turn runs: a switch starts a fresh session, so it waits. */
  disabled: boolean
  onChange: (backend: string | null) => void
}

/** The picker's two queries, shared by the control and its notices (one cache entry each). */
function useBackendPickerQueries() {
  const probeQ = useQuery<{ backends: AcpBackendProbe[] }>({
    queryKey: ['acpBackends'],
    // The cache entry is shared with the onboarding gate and the Agent Backend
    // tab, which read `data.backends` directly: store a body without a list as
    // an empty list, never as the bare body.
    queryFn: async () => {
      const body = await api.acpBackends()
      return { ...body, backends: Array.isArray(body?.backends) ? body.backends : [] }
    },
    retry: false,
  })
  // The configured default, so "Default" can say which backend it is. Same query
  // and cache entry as the top bar and Settings -> Developer -> Agent Backend.
  const cfgQ = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  // A failed probe must say so: falling through to the hidden control would read
  // as a missing feature, not a failure. The two PERMANENT answers stay silent,
  // as on the Agent Backend tab: 403 (this caller may not read the probe, so has
  // nothing to pick) and 404 (a gateway that predates the endpoint, so has no
  // per-chat backend either). The same two stay silent for the config read.
  const probeFailed = probeQ.isError && !probeAnswerIsPermanent(probeQ.error)
  const cfgFailed = cfgQ.isError && !probeAnswerIsPermanent(cfgQ.error)
  return { probeQ, cfgQ, probeFailed, cfgFailed }
}

/** Whether the picker control renders: there is something to choose, or a pick to show. */
function pickerShown(rows: readonly AcpBackendProbe[], value: string | null): boolean {
  return rows.length >= 2 || value !== null
}

/**
 * The composer's AI backend picker for one chat.
 *
 * Same interaction as the model picker beside it: changeable between turns,
 * locked while a turn runs. The choices are the backends `GET /api/acp-backends`
 * reports as selectable and startable on this machine -- the same rows Settings
 * → Developer → Agent Backend offers, under the same `['acpBackends']` cache
 * entry. Renders nothing on an install with a single startable backend and no
 * pick of its own: there is nothing to choose. A pick the list no longer offers
 * still shows as itself, so the control never claims a chat is on something else.
 * Its errors render separately, through {@link BackendPickerNotices}.
 */
export default function BackendPicker({ value, degraded = false, disabled, onChange }: BackendPickerProps) {
  const { t } = useTranslation()
  const { probeQ, cfgQ, probeFailed } = useBackendPickerQueries()
  // The failure itself is reported by BackendPickerNotices, on its own row.
  if (probeFailed) return null
  const rows = pickableBackends(probeQ.data?.backends ?? [])
  if (!pickerShown(rows, value)) return null
  const nameOf = (row: AcpBackendProbe | undefined, id: string): string => {
    const policyId = row?.policy_id || id || 'kiro'
    const key = NAME_KEYS[policyId]
    return key ? t(key) : policyId
  }
  // SimpleSelect treats '' as "nothing picked", and '' is Kiro's own id, so the
  // options carry an option value per backend with Kiro spelled KIRO_OPTION.
  const toOption = (id: string) => (id === '' ? KIRO_OPTION : id)
  const fromOption = (opt: string) => (opt === KIRO_OPTION ? '' : opt)
  const ids = rows.map(r => r.id)
  if (value !== null && !ids.includes(value)) ids.unshift(value)
  const options = [NO_PICK, ...ids.map(toOption)]
  const configured = cfgQ.data === undefined ? undefined : (cfgQ.data.agent?.acp_backend ?? ACP_BACKEND_KIRO)
  const defaultLabel = configured === undefined
    ? t('components.backendPicker.default')
    : t('components.backendPicker.default_named', { name: nameOf(rows.find(r => r.id === configured), configured) })
  const optionLabels = [
    defaultLabel,
    ...ids.map(id => {
      const name = nameOf(rows.find(r => r.id === id), id)
      // The gateway runs a no-longer-selectable pick on Kiro: name both, so the
      // chip never claims the chat is on a backend it is not.
      return degraded && id === value ? t('components.backendPicker.unavailable_using_kiro', { name }) : name
    }),
  ]
  const label = disabled
    ? t('components.backendPicker.locked_while_running')
    : t('components.backendPicker.label')
  // A degraded pick's chip can be truncated on a narrow shelf, which cuts off the
  // "(unavailable, using Kiro CLI)" half; the icon carries that state on its own.
  const degradedLabel = degraded && value !== null ? optionLabels[ids.indexOf(value) + 1] : null
  return (
    <span data-testid="composer-backend-picker" className="inline-flex items-center gap-1.5 min-w-0 text-[12px] text-muted">
      {degradedLabel
        ? <span data-testid="composer-backend-picker-degraded" role="img" aria-label={degradedLabel} title={degradedLabel} className="inline-flex shrink-0 text-warn"><AlertTriangle size={13} aria-hidden /></span>
        : <Cpu size={13} className="shrink-0 opacity-70" aria-hidden />}
      <span className="flex-1 min-w-0 max-w-[160px]">
      <SimpleSelect
        aria-label={label}
        title={label}
        options={options}
        optionLabels={optionLabels}
        value={value === null ? NO_PICK : toOption(value)}
        disabled={disabled}
        onChange={opt => onChange(opt === NO_PICK ? null : fromOption(opt))}
      />
      </span>
    </span>
  )
}

interface BackendPickerNoticesProps {
  /** The chat's own pick, as given to the picker: decides whether it is shown. */
  value: string | null
  /** Why the last switch was refused; `null` when it was not. */
  switchError?: string | null
  /** Clears `switchError` (the notice's dismiss control). */
  onDismissSwitchError?: () => void
}

/**
 * The picker's errors, as ErrorNotices with the ask-agent hand-off. Rendered on
 * the composer's own row under the shelf (ChatInput's `backendPickerNotice`),
 * not inside the picker: the picker's shelf group is width-capped on a narrow
 * pane, which would crush a notice's message and controls.
 */
export function BackendPickerNotices({ value, switchError = null, onDismissSwitchError }: BackendPickerNoticesProps) {
  const { t } = useTranslation()
  const { probeQ, probeFailed, cfgFailed } = useBackendPickerQueries()
  // A failed config read only matters while the Default row is on screen.
  const cfgNotice = cfgFailed && !probeFailed && pickerShown(pickableBackends(probeQ.data?.backends ?? []), value)
  if (!probeFailed && !cfgNotice && !switchError) return null
  return (
    <div className="flex flex-col gap-1 min-w-0 w-full text-[12px]">
      {probeFailed && (
        <div data-testid="composer-backend-picker-error" className="min-w-0">
          <ErrorNotice variant="inline" message={t('components.backendPicker.could_not_load')} askAgent />
        </div>
      )}
      {cfgNotice && (
        <div data-testid="composer-backend-config-error" className="min-w-0">
          <ErrorNotice variant="inline" message={t('components.backendPicker.could_not_load_default')} askAgent />
        </div>
      )}
      {switchError && (
        <div data-testid="composer-backend-switch-error" className="min-w-0">
          <ErrorNotice variant="inline" message={switchError} askAgent onDismiss={onDismissSwitchError} />
        </div>
      )}
    </div>
  )
}
