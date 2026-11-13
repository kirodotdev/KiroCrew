import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import { ApiError, apiErrorCode, isNotFoundError } from '../../api/apiError'
import type {
  DecisionsLocalModel,
  DecisionsLocalRuntimeData,
  DecisionsProviderData,
  DecisionsRuntimeStatus,
  DecisionsSageMakerTarget,
  DecisionsSageMakerTestResult,
} from '../../api/client/decisions'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Input } from '../../components/ui'
import { fmtPercent, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { DecisionsHostedKeyField } from './DecisionsHostedKeyField'
import {
  DECISIONS_PROVIDER_QUERY_KEY,
  PRESET_HOSTED,
  PRESET_NONE,
  isRefusedHostedDefault,
  isRefusedSageMaker,
  providerPermitted,
} from './decisionsProviderQuery'

/** Preset id for the hosted provider; the gateway's `local_models.PRESET_JEV`. */
export const PRESET_JEV = PRESET_HOSTED
/** The picker's id for a SageMaker endpoint; the gateway's `PRESET_SAGEMAKER`. */
export const PRESET_SAGEMAKER = 'sagemaker'
/** What the SageMaker option asks to install when the gateway has no boto3. */
const BOTO3_INSTALL = 'pip install "boto3>=1.34,<2"'
const EMPTY_SAGEMAKER: DecisionsSageMakerTarget = { endpoint_name: '', region: '', profile: '' }

const sameSageMaker = (a: DecisionsSageMakerTarget, b: DecisionsSageMakerTarget | null | undefined) =>
  !!b && a.endpoint_name === b.endpoint_name && a.region === b.region && a.profile === b.profile

/** The field a refused SageMaker body names (`endpoint_name`, `region`, `profile`), or `''`. */
function refusedField(e: unknown): string {
  if (!(e instanceof ApiError)) return ''
  try {
    const field = (JSON.parse(e.body) as { field?: unknown }).field
    return typeof field === 'string' ? field : ''
  } catch {
    return ''
  }
}

/**
 * The sentence for a refused SageMaker save or test, from the gateway's code and
 * field, or `null` when the refusal is not about the SageMaker fields.
 */
function sageMakerRefusal(e: unknown): string | null {
  const code = apiErrorCode(e)
  if (code === 'decisions_provider_sagemaker_unknown_profile') {
    return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_unknown_profile')
  }
  if (code === 'decisions_provider_sagemaker_unavailable') {
    return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_boto3_missing', { command: BOTO3_INSTALL })
  }
  if (code !== 'decisions_provider_sagemaker_invalid') return null
  const field = refusedField(e)
  if (field === 'region') return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_invalid_region')
  if (field === 'profile') return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_invalid_profile')
  return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_invalid_name')
}

/** The sentence for a connection test that ran and got no answer, by the gateway's fixed word. */
function testFailure(error: string): string {
  switch (error) {
    case 'credentials':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_credentials')
    case 'not_found':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_not_found')
    case 'timeout':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_timeout')
    case 'throttled':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_throttled')
    case 'unavailable':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_unavailable')
    case 'model_error':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_model_error')
    case 'protocol':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_protocol')
    default:
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_failed')
  }
}

/**
 * The three fields that name a SageMaker endpoint, and the connection test.
 *
 * The card sends the parts, never an address: the gateway checks each one and
 * builds the `sagemaker://` address itself.
 */
function SageMakerFields({
  value,
  onChange,
  disabled,
  available,
  notSending,
}: {
  value: DecisionsSageMakerTarget
  onChange: (next: DecisionsSageMakerTarget) => void
  disabled: boolean
  available: boolean
  /** The saved endpoint is shown and has no consent yet, so nothing reaches it. */
  notSending: boolean
}) {
  const baseId = useId()
  const testMut = useMutation<DecisionsSageMakerTestResult, unknown, DecisionsSageMakerTarget>({
    mutationFn: target => api.testDecisionsSageMaker(target),
  })
  // A result describes the fields it was run with; editing them retires it.
  const edit = (patch: Partial<DecisionsSageMakerTarget>) => {
    testMut.reset()
    onChange({ ...value, ...patch })
  }
  const complete = value.endpoint_name.trim() !== '' && value.region.trim() !== ''
  const field = (key: keyof DecisionsSageMakerTarget, label: string, placeholder: string) => (
    <label htmlFor={`${baseId}-${key}`} className="flex flex-col gap-0.5 text-[12px] text-text">
      {label}
      <Input
        id={`${baseId}-${key}`}
        type="text"
        autoComplete="off"
        spellCheck={false}
        className="font-mono text-[12px]"
        placeholder={placeholder}
        value={value[key]}
        disabled={disabled || !available}
        onChange={e => edit({ [key]: e.target.value.trim() })}
      />
    </label>
  )
  const result = testMut.data
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-border bg-bg-accent px-2.5 py-1.5">
      {!available && (
        <p className="text-[12px] text-warn m-0" role="status">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_boto3_missing', { command: BOTO3_INSTALL })}
        </p>
      )}
      {/* The switch and the screenshot toggle are named by their own labels, so the
          line points at exactly what the reader sees on this card. */}
      {notSending && (
        <p className="text-[12px] text-warn m-0" role="status">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_not_sending', {
            switch: i18nT('pages.developer.featurePreviewsTab.decisions'),
            screenshots: i18nT('pages.developer.featurePreviewsTab.decisions_browser_images'),
            point: i18nT('pages.developer.featurePreviewsTab.decisions_point_browser_step'),
          })}
        </p>
      )}
      {field('endpoint_name', i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_name'), 'my-endpoint')}
      {field('region', i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_region'), 'us-west-2')}
      {/* No placeholder: a greyed "default" reads as a value already set. */}
      {field('profile', i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_profile'), '')}
      <p className="text-[12px] text-muted m-0">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_profile_hint')}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Btn disabled={disabled || !available || !complete || testMut.isPending} onClick={() => testMut.mutate(value)}>
          {testMut.isPending
            ? i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_testing')
            : i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test')}
        </Btn>
        {result?.ok && (
          <span className="text-[12px] text-accent" role="status">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_ok', {
              latency: fmtUnit(result.latency_ms / 1000, 'second', { maximumFractionDigits: 1 }),
            })}
          </span>
        )}
      </div>
      {result && !result.ok && (
        // The gateway's own fixed text goes below the translated reason: it names the
        // AWS error code, and it never quotes the request or a credential.
        // No hand-off: the typed SageMaker fields are an unsaved draft, and asking
        // the agent unmounts the card.
        <ErrorNotice
          variant="block"
          title={result.message ? testFailure(result.error) : undefined}
          message={result.message || testFailure(result.error)}
          messagePlacement="below"
          messageClassName="font-mono text-[11px] break-words"
        />
      )}
      {/* No hand-off, for the same unsaved draft as the notice above. */}
      {testMut.isError && (
        <ErrorNotice
          variant="inline"
          message={
            sageMakerRefusal(testMut.error) ??
            i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_request_failed')
          }
        />
      )}
    </div>
  )
}

// The gateway reports memory in GiB after firmware and kernel reserve, so a 24 GB
// machine reads about 23.5; the nominal threshold is met within this slack.
const RAM_REPORT_SLACK_GB = 1.5
/** How often the card re-reads the runtime while a model is being prepared. */
export const RUNTIME_POLL_MS = 2000
/** States in which the runtime is still working toward `running`. */
const RUNTIME_BUSY = new Set(['downloading', 'installing', 'starting'])

export { DECISIONS_PROVIDER_QUERY_KEY, PRESET_NONE }

const gigabytes = (bytes: number) => fmtUnit(bytes / 1e9, 'gigabyte', { maximumFractionDigits: 1 })

/**
 * The preset recommended for a machine with `totalGb` of memory: the first local
 * preset, in the gateway's order, whose threshold the machine meets, else the hosted
 * provider. An unknown size recommends the hosted provider, because suggesting a
 * model the machine cannot hold is worse than suggesting nothing local.
 */
export function recommendedPreset(presets: DecisionsLocalModel[], totalGb: number | null | undefined): string {
  if (typeof totalGb !== 'number' || !Number.isFinite(totalGb) || totalGb <= 0) return PRESET_JEV
  return presets.find(p => totalGb + RAM_REPORT_SLACK_GB >= p.recommended_total_ram_gb)?.id ?? PRESET_JEV
}

/** What the gateway is doing with the model it runs: progress, ready, or why it stopped. */
function RuntimeStatus({
  status,
  onRetry,
  retryDisabled,
  consentOn,
}: {
  status: DecisionsRuntimeStatus
  onRetry: () => void
  retryDisabled: boolean
  consentOn: boolean
}) {
  if (status.state === 'downloading') {
    const total = Math.max(status.bytes_total, 1)
    return (
      <div className="flex flex-col gap-1">
        <p className="text-[12px] text-text m-0">
          {i18nT('pages.developer.featurePreviewsTab.decisions_runtime_downloading', {
            done: gigabytes(status.bytes_done),
            total: gigabytes(status.bytes_total),
          })}
        </p>
        <progress
          className="w-full h-1.5 accent-[var(--accent)]"
          max={total}
          value={Math.min(status.bytes_done, total)}
          aria-label={i18nT('pages.developer.featurePreviewsTab.decisions_runtime_downloading_label')}
        />
        <p className="text-[12px] text-muted m-0">
          {i18nT('pages.developer.featurePreviewsTab.decisions_runtime_preparing_note')}
        </p>
      </div>
    )
  }
  if (status.state === 'installing') {
    return (
      <div className="flex flex-col gap-1" role="status">
        <p className="text-[12px] text-text m-0">
          {i18nT('pages.developer.featurePreviewsTab.decisions_runtime_installing')}
        </p>
        <p className="text-[12px] text-muted m-0">
          {i18nT('pages.developer.featurePreviewsTab.decisions_runtime_preparing_note')}
        </p>
      </div>
    )
  }
  if (status.state === 'starting') {
    return (
      <p className="text-[12px] text-text m-0" role="status">
        {i18nT('pages.developer.featurePreviewsTab.decisions_runtime_starting')}
      </p>
    )
  }
  if (status.state === 'running') {
    return (
      // Running is not answering: the server is up, but nothing reaches it while the
      // main switch is off, and the line must not read as if it were in use.
      <p className={`text-[12px] m-0 ${consentOn ? 'text-accent' : 'text-muted'}`} role="status">
        {consentOn
          ? i18nT('pages.developer.featurePreviewsTab.decisions_runtime_running')
          : i18nT('pages.developer.featurePreviewsTab.decisions_runtime_running_off')}
      </p>
    )
  }
  if (status.state === 'error') {
    return (
      <div className="flex flex-col gap-1">
        {/* Nothing to lose here -- the card holds no draft -- so the hand-off is
            offered, and the runtime's own reason (the server log's tail) is the
            message it carries, so the agent sees why the model would not start. */}
        <ErrorNotice
          variant="block"
          askAgent
          title={i18nT('pages.developer.featurePreviewsTab.decisions_runtime_error')}
          message={status.error || i18nT('pages.developer.featurePreviewsTab.decisions_runtime_error')}
          messagePlacement="below"
          messageClassName="max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px]"
        />
        <div>
          <Btn disabled={retryDisabled} onClick={onRetry}>
            {i18nT('pages.developer.featurePreviewsTab.decisions_runtime_retry')}
          </Btn>
        </div>
      </div>
    )
  }
  return null
}

/**
 * Which decision model answers: the hosted provider, a model on this machine, or the
 * owner's own SageMaker endpoint.
 *
 * Each local preset states, in the reader's terms, how close it comes to the
 * reference model, what memory it needs and how slow it is, and the card marks the
 * one this machine's memory suits. Choosing one writes a preset id -- never an address or a port --
 * through the owner-only provider route, which builds the loopback URL itself and
 * then downloads, installs and runs the model; the card follows that progress.
 * A SageMaker endpoint is written as its name, region and profile, from which the
 * same route builds the `sagemaker://` address.
 *
 * Every option the administrator's policy does not allow (`providerPermitted`: its
 * row, its host and its own entry in the `providers` allow list) is drawn, greyed,
 * with the administrator's wording, and cannot be chosen; "No model" never is. The
 * hosted provider's API key is part of that provider's own entry: choosing the entry
 * opens it (`DecisionsHostedKeyField`), and nothing outside the entry names it.
 */
export function DecisionsProviderPicker({
  frozen,
  cardReadFailed = false,
  consentOn = true,
  openHostedKey = false,
}: {
  frozen: boolean
  /** Settings search aimed at the hosted provider's API key: open that entry. */
  openHostedKey?: boolean
  /** Whether consent stands for the configured endpoint: a model only answers while it does. */
  consentOn?: boolean
  /** The card already shows its own read-failure notice: one failure, one notice. */
  cardReadFailed?: boolean
}) {
  const qc = useQueryClient()
  const headingId = useId()
  const providerQ = useQuery<DecisionsProviderData>({
    queryKey: DECISIONS_PROVIDER_QUERY_KEY,
    queryFn: () => api.getDecisionsProvider(),
    retry: false,
  })
  // Progress comes from the audit-free status route, every two seconds while the
  // gateway prepares a model and not at all once it settles. Armed by the provider
  // read, so nothing is polled on a gateway that is not preparing anything.
  const providerBusy = RUNTIME_BUSY.has(providerQ.data?.runtime?.state ?? '')
  const runtimeQ = useQuery<DecisionsLocalRuntimeData>({
    queryKey: [...DECISIONS_PROVIDER_QUERY_KEY, 'runtime'],
    queryFn: () => api.getDecisionsLocalRuntime(),
    enabled: providerBusy,
    retry: false,
    // A malformed answer reads as settled, which stops the poll rather than throwing.
    // A failed poll keeps polling: retry is off, so stopping here would freeze the
    // progress until the card remounts.
    refetchInterval: q =>
      q.state.status === 'error' || RUNTIME_BUSY.has(q.state.data?.runtime?.state ?? '') ? RUNTIME_POLL_MS : false,
  })
  // Total memory only: a server holds its weights resident, so what the machine
  // HAS decides whether a model fits, not what happens to be free this minute.
  // `null`, not `undefined`, for "no figure": react-query refuses an undefined result.
  const memQ = useQuery<number | null>({
    queryKey: ['decisionsHostMemory'],
    queryFn: () => api.system().then(d => (typeof d.mem_total_gb === 'number' ? d.mem_total_gb : null)),
    staleTime: 5 * 60_000,
  })
  const [selected, setSelected] = useState<string | null>(null)
  // The SageMaker fields as typed; null until edited, which shows the configured ones.
  const [smDraft, setSmDraft] = useState<DecisionsSageMakerTarget | null>(null)
  const saveMut = useMutation({
    mutationFn: (choice: { preset: string; sagemaker?: DecisionsSageMakerTarget }) =>
      choice.sagemaker ? api.saveDecisionsSageMaker(choice.sagemaker) : api.saveDecisionsProvider(choice.preset),
    onSuccess: () => {
      setSelected(null)
      setSmDraft(null)
    },
    // The endpoint moved, so the consent row's "sent to" line and the config read
    // both change with it.
    onSettled: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: DECISIONS_PROVIDER_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: ['decisionsConsent'] }),
        qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
      ]),
  })
  const removeMut = useMutation({
    mutationFn: (id: string) => api.removeDecisionsLocalModel(id),
    onSettled: () => qc.invalidateQueries({ queryKey: DECISIONS_PROVIDER_QUERY_KEY }),
  })

  const data = providerQ.data
  // An older gateway has no provider route: that 404 is an answer, not a failure,
  // and the card's own pointer already says where the address is set, so nothing
  // is drawn. Any other failed read says so, with the hand-off -- there is no draft
  // to lose before the list has loaded.
  if (providerQ.isError) {
    return isNotFoundError(providerQ.error) || cardReadFailed ? null : (
      <ErrorNotice
        variant="inline"
        askAgent
        message={i18nT('pages.developer.featurePreviewsTab.decisions_provider_unavailable')}
      />
    )
  }
  if (!data) return null

  // The administrator's policy decides which options may be chosen, per model; an
  // older gateway reports none of it, which is the permissive answer it always had.
  // A refused option is SHOWN, greyed and saying so, never hidden: a model that
  // silently vanished would read as one this build does not ship.
  const blockedId = (id: string) => !providerPermitted(data, id)
  // An older gateway reports no SageMaker fields and would refuse the body.
  const sageMakerOffered = typeof data.sagemaker_available === 'boolean'
  const hiddenId = (id: string) => id === PRESET_SAGEMAKER && !sageMakerOffered
  const sageMakerAvailable = data.sagemaker_available === true
  const smValue = smDraft ?? data.sagemaker ?? EMPTY_SAGEMAKER
  // The polled status is newer than the provider read while that read still shows
  // the model being prepared. Once a fresh provider read has settled, its own
  // runtime is current and the last poll -- kept by react-query -- is not.
  const polled = runtimeQ.data
  // A failed poll keeps react-query's last answer; that figure is no longer live.
  const pollFailed = providerBusy && runtimeQ.isError
  const live =
    providerBusy && !pollFailed && polled && typeof polled.runtime === 'object' && Array.isArray(polled.installed) ? polled : undefined
  const presets = live ? data.presets.map(p => ({ ...p, installed: live.installed.includes(p.id) })) : data.presets
  const totalGb = memQ.data
  const recommended = recommendedPreset(presets.filter(p => !blockedId(p.id)), totalGb)
  const configuredChoice = data.active === 'custom' || hiddenId(data.active) ? '' : data.active
  // A search hit on the hosted provider's key opens that provider's entry, so the
  // field it names is on screen; it writes nothing until the reader presses a button.
  const chosen =
    selected ?? (openHostedKey && !blockedId(PRESET_JEV) ? PRESET_JEV : configuredChoice)
  const chosenPreset = presets.find(p => p.id === chosen)
  const runtime = live?.runtime ?? data.runtime
  // The runtime's report applies only to the preset it is running.
  const chosenRuntime =
    runtime && chosenPreset && runtime.preset === chosenPreset.id && runtime.state !== 'idle' ? runtime : undefined
  const sageMakerChosen = chosen === PRESET_SAGEMAKER
  // Another endpoint while one is in use is a change too, not only another option.
  // A refused hosted default is reported as "No model" while the config still names
  // it, so choosing "No model" over it is a change that has to be written.
  const refusedDefault = isRefusedHostedDefault(data)
  const needsSave =
    chosen !== '' &&
    (chosen !== data.active ||
      (chosen === PRESET_NONE && refusedDefault) ||
      (sageMakerChosen && !sameSageMaker(smValue, data.sagemaker)))
  const sageMakerReady = !sageMakerChosen || (sageMakerAvailable && smValue.endpoint_name !== '' && smValue.region !== '')
  const canSave = needsSave && sageMakerReady && !frozen && !saveMut.isPending && !blockedId(chosen)
  const save = () => saveMut.mutate(sageMakerChosen ? { preset: chosen, sagemaker: smValue } : { preset: chosen })
  const removable = presets.filter(p => p.installed && p.id !== data.active && p.id !== runtime?.preset)
  // Every option the picker offers is refused; "No model" is then the only choice.
  const allBlocked =
    blockedId(PRESET_JEV) &&
    presets.every(p => blockedId(p.id)) &&
    (!sageMakerOffered || blockedId(PRESET_SAGEMAKER))

  // A preset that just failed to start here is not one to recommend for this machine.
  const failedHere = (id: string) => runtime?.preset === id && runtime.state === 'error'
  // Memory decides only between local models; the hosted provider is the fallback, not a fit.
  const recommendedBadge = (id: string) =>
    id === recommended && id !== PRESET_JEV && typeof totalGb === 'number' && !failedHere(id) ? (
      <span className="rounded bg-accent/15 px-1.5 text-[11px] text-accent">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_recommended', {
          memory: fmtUnit(totalGb, 'gigabyte', { maximumFractionDigits: 0 }),
        })}
      </span>
    ) : null
  // "In use" means answering: a local preset earns it only once its server runs,
  // and until then the status block below says what is happening instead.
  const answering = (id: string) =>
    id === PRESET_NONE ||
    (consentOn &&
      (id === PRESET_JEV || id === PRESET_SAGEMAKER || (runtime?.preset === id && runtime.state === 'running')))
  const activeBadge = (id: string) =>
    id === data.active && answering(id) ? (
      <span className="rounded bg-bg px-1.5 text-[11px] text-muted border border-border">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_active')}
      </span>
    ) : null

  const option = (id: string, name: string, details: ReactNode) => (
    <label
      key={id}
      data-provider-entry={id}
      data-provider-blocked={blockedId(id) ? 'true' : undefined}
      className={`flex items-start gap-2 rounded-md border px-2.5 py-1.5 ${
        blockedId(id) ? 'cursor-not-allowed' : 'cursor-pointer'
      } ${chosen === id ? 'border-accent bg-bg' : 'border-border bg-bg'}`}
    >
      <input
        type="radio"
        name={headingId}
        aria-label={name}
        className="mt-1"
        checked={chosen === id}
        disabled={frozen || saveMut.isPending || (blockedId(id) && chosen !== id)}
        onChange={() => setSelected(id)}
      />
      <span className="flex flex-col gap-0.5 min-w-0">
        {/* A refused model's name and figures fade; the line saying why does not, so
            the reason keeps its full contrast. */}
        <span className={`flex flex-col gap-0.5 min-w-0 ${blockedId(id) ? 'opacity-60' : ''}`}>
          <span className="flex flex-wrap items-center gap-1.5 text-[12px] font-medium text-text">
            {name}
            {activeBadge(id)}
            {recommendedBadge(id)}
          </span>
          {details}
        </span>
        {blockedId(id) && (
          <span className="text-[12px] text-warn">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_blocked')}
          </span>
        )}
      </span>
    </label>
  )

  return (
    <div className="flex flex-col gap-1.5" role="radiogroup" aria-labelledby={headingId}>
      <p id={headingId} className="text-[12px] font-medium text-text m-0">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_label')}
      </p>
      <p className="text-[12px] text-muted m-0">{i18nT('pages.developer.featurePreviewsTab.decisions_provider_desc')}</p>
      {/* Without the memory figure no preset can be marked recommended; say so
          rather than let the badge silently vanish. The picker itself still works. */}
      {memQ.isError && !cardReadFailed && (
        <ErrorNotice
          variant="inline"
          askAgent
          message={i18nT('pages.developer.featurePreviewsTab.decisions_provider_memory_unavailable')}
        />
      )}
      {/* Every option is refused: one plain sentence above them, so a list of greyed
          rows is never the only way to read it. */}
      {allBlocked && (
        <p className="text-[12px] text-warn m-0" role="status" data-testid="decisions-all-models-blocked">
          {i18nT('pages.developer.featurePreviewsTab.decisions_all_models_blocked')}
        </p>
      )}
      {refusedDefault && isRefusedSageMaker(data) && (
        <p className="text-[12px] text-warn m-0">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_blocked')}
        </p>
      )}
      {/* The hosted provider's ENTRY: its option, the line saying a configured hosted
          default is refused, and its API key. The only place its name is drawn. */}
      <div className="flex flex-col gap-1.5" data-testid="decisions-provider-hosted-entry">
        {option(
          PRESET_JEV,
          i18nT('pages.developer.featurePreviewsTab.decisions_provider_jev'),
          <span className="text-[12px] text-muted">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_jev_detail')}
          </span>,
        )}
        {refusedDefault && !isRefusedSageMaker(data) && (
          <p className="text-[12px] text-warn m-0">
            {i18nT('pages.developer.featurePreviewsTab.decisions_hosted_refused', {
              name: i18nT('pages.developer.featurePreviewsTab.decisions_provider_jev'),
            })}
          </p>
        )}
        {chosen === PRESET_JEV && !blockedId(PRESET_JEV) && (
          <DecisionsHostedKeyField disabled={frozen} />
        )}
      </div>
      {presets.map(p =>
        option(
          p.id,
          p.name,
          <>
            <span className="text-[12px] text-text">
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_quality', {
                percent: fmtPercent(p.jev_relative_pct / 100, { maximumFractionDigits: 0 }),
                hard: fmtPercent(p.hard_relative_pct / 100, { maximumFractionDigits: 0 }),
              })}
            </span>
            <span className="text-[12px] text-muted">
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_memory', {
                peak: fmtUnit(p.peak_ram_gb, 'gigabyte', { maximumFractionDigits: 0 }),
                total: fmtUnit(p.recommended_total_ram_gb, 'gigabyte', { maximumFractionDigits: 0 }),
              })}
            </span>
            <span className="text-[12px] text-muted">
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_speed', {
                p50: fmtUnit(p.p50_secs, 'second', { maximumFractionDigits: 1 }),
                p95: fmtUnit(p.p95_secs, 'second', { maximumFractionDigits: 1 }),
              })}
            </span>
          </>,
        ),
      )}
      {sageMakerOffered &&
        option(
          PRESET_SAGEMAKER,
          i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker'),
          <span className="text-[12px] text-muted">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_detail')}
          </span>,
        )}
      {sageMakerOffered && sageMakerChosen && (
        <SageMakerFields
          value={smValue}
          onChange={setSmDraft}
          disabled={frozen || saveMut.isPending || blockedId(PRESET_SAGEMAKER)}
          available={sageMakerAvailable}
          // Only once saved, and only while the fields still show that saved
          // endpoint: an unsaved draft is not "this endpoint" yet.
          notSending={!consentOn && data.active === PRESET_SAGEMAKER && sameSageMaker(smValue, data.sagemaker)}
        />
      )}
      {option(
        PRESET_NONE,
        i18nT('pages.developer.featurePreviewsTab.decisions_provider_none'),
        <span className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_none_detail')}
        </span>,
      )}
      {chosenPreset && (
        <div className="flex flex-col gap-1 rounded-md border border-border bg-bg-accent px-2.5 py-1.5">
          {pollFailed && (
            <ErrorNotice
              variant="inline"
              askAgent
              message={i18nT('pages.developer.featurePreviewsTab.decisions_runtime_poll_unavailable')}
            />
          )}
          {chosenRuntime ? (
            <RuntimeStatus
              status={chosenRuntime}
              onRetry={() => saveMut.mutate({ preset: chosenPreset.id })}
              retryDisabled={frozen || saveMut.isPending || blockedId(chosenPreset.id)}
              consentOn={consentOn}
            />
          ) : (
            <p className="text-[12px] text-muted m-0">
              {chosenPreset.installed
                ? i18nT('pages.developer.featurePreviewsTab.decisions_provider_ready')
                : i18nT('pages.developer.featurePreviewsTab.decisions_provider_download_note', {
                    size: gigabytes(chosenPreset.download_bytes),
                  })}
            </p>
          )}
          {/* What running it costs the rest of the machine, before and while it runs. */}
          <p className="text-[12px] text-text m-0">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_memory_cost', {
              name: chosenPreset.name,
              peak: fmtUnit(chosenPreset.peak_ram_gb, 'gigabyte', { maximumFractionDigits: 0 }),
            })}
          </p>
        </div>
      )}
      <p className="text-[11px] text-muted m-0">{i18nT('pages.developer.featurePreviewsTab.decisions_provider_measured')}</p>
      {needsSave && (
        <div className="flex flex-col gap-1">
          {/* The picked option is not live yet; say what keeps answering meanwhile. */}
          <p className="text-[12px] text-muted m-0">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_pending')}
          </p>
          <div>
            <Btn disabled={!canSave} onClick={save}>
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_use')}
            </Btn>
          </div>
        </div>
      )}
      {/* No hand-off: a failed save leaves the reader's unsaved pick on the card, and
          asking the agent unmounts it. A refused SageMaker field says which one. */}
      {saveMut.isError && (
        <ErrorNotice
          variant="inline"
          message={
            sageMakerRefusal(saveMut.error) ?? i18nT('pages.developer.featurePreviewsTab.decisions_provider_save_failed')
          }
        />
      )}
      {removable.map(p => (
        <div key={p.id}>
          <Btn disabled={frozen || removeMut.isPending} onClick={() => removeMut.mutate(p.id)}>
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_remove', {
              name: p.name,
              size: gigabytes(p.download_bytes),
            })}
          </Btn>
        </div>
      ))}
      {removeMut.isError && (
        <ErrorNotice
          variant="inline"
          askAgent
          message={i18nT('pages.developer.featurePreviewsTab.decisions_provider_remove_failed')}
        />
      )}
    </div>
  )
}
