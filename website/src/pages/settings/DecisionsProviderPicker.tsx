import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import type {
  DecisionsLocalModel,
  DecisionsLocalRuntimeData,
  DecisionsProviderData,
  DecisionsRuntimeStatus,
} from '../../api/client/decisions'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { fmtPercent, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { DECISIONS_PROVIDER_QUERY_KEY, PRESET_NONE } from './decisionsProviderQuery'

/** Preset id for hosted Jev; the gateway's `local_models.PRESET_JEV`. */
export const PRESET_JEV = 'jev'

// The gateway reports memory in GiB after firmware and kernel reserve, so a 24 GB
// machine reads about 23.5; the nominal threshold is met within this slack.
const RAM_REPORT_SLACK_GB = 1.5
/** How often the card re-reads the runtime while a model is being prepared. */
const RUNTIME_POLL_MS = 2000
/** States in which the runtime is still working toward `running`. */
const RUNTIME_BUSY = new Set(['downloading', 'installing', 'starting'])

export { DECISIONS_PROVIDER_QUERY_KEY, PRESET_NONE }

const gigabytes = (bytes: number) => fmtUnit(bytes / 1e9, 'gigabyte', { maximumFractionDigits: 1 })

/**
 * The preset recommended for a machine with `totalGb` of memory: the first local
 * preset, in the gateway's order, whose threshold the machine meets, else hosted
 * Jev. An unknown size recommends Jev, because suggesting a model the machine
 * cannot hold is worse than suggesting nothing local.
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
      // Decisions switch is off, and the line must not read as if it were in use.
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
 * Which System One server answers the seam: hosted Jev, or a model on this machine.
 *
 * Each local preset states, in the reader's terms, how close it comes to Jev, what
 * memory it needs and how slow it is, and the card marks the one this machine's
 * memory suits. Choosing one writes a preset id -- never an address or a port --
 * through the owner-only provider route, which builds the loopback URL itself and
 * then downloads, installs and runs the model; the card follows that progress.
 */
export function DecisionsProviderPicker({
  frozen,
  cardReadFailed = false,
  consentOn = true,
}: {
  frozen: boolean
  /** Whether the Decisions switch is on: a model only answers while it is. */
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
  const saveMut = useMutation({
    mutationFn: (preset: string) => api.saveDecisionsProvider(preset),
    onSuccess: () => setSelected(null),
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

  // Policy decides which side may be chosen; an older gateway reports neither, which
  // is the permissive answer it always had.
  const hostedBlocked = data.hosted_permitted === false
  const localBlocked = data.local_permitted === false
  // "No model" sends and runs nothing, so no policy withholds it.
  const blockedId = (id: string) => (id === PRESET_NONE ? false : id === PRESET_JEV ? hostedBlocked : localBlocked)
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
  const recommended = recommendedPreset(localBlocked ? [] : presets, totalGb)
  const chosen = selected ?? (data.active === 'custom' ? '' : data.active)
  const chosenPreset = presets.find(p => p.id === chosen)
  const runtime = live?.runtime ?? data.runtime
  // The runtime's report applies only to the preset it is running.
  const chosenRuntime =
    runtime && chosenPreset && runtime.preset === chosenPreset.id && runtime.state !== 'idle' ? runtime : undefined
  const needsSave = chosen !== '' && chosen !== data.active
  const canSave = needsSave && !frozen && !saveMut.isPending && !blockedId(chosen)
  const removable = presets.filter(p => p.installed && p.id !== data.active && p.id !== runtime?.preset)

  // A preset that just failed to start here is not one to recommend for this machine.
  const failedHere = (id: string) => runtime?.preset === id && runtime.state === 'error'
  // Memory decides only between local models; hosted Jev is the fallback, not a fit.
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
    (consentOn && (id === PRESET_JEV || (runtime?.preset === id && runtime.state === 'running')))
  const activeBadge = (id: string) =>
    id === data.active && answering(id) ? (
      <span className="rounded bg-bg px-1.5 text-[11px] text-muted border border-border">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_active')}
      </span>
    ) : null

  const option = (id: string, name: string, details: ReactNode) => (
    <label
      key={id}
      className={`flex items-start gap-2 rounded-md border px-2.5 py-1.5 cursor-pointer ${
        chosen === id ? 'border-accent bg-bg' : 'border-border bg-bg'
      }`}
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
        <span className="flex flex-wrap items-center gap-1.5 text-[12px] font-medium text-text">
          {name}
          {activeBadge(id)}
          {recommendedBadge(id)}
        </span>
        {details}
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
      {option(
        PRESET_JEV,
        i18nT('pages.developer.featurePreviewsTab.decisions_provider_jev'),
        <span className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_jev_detail')}
        </span>,
      )}
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
              onRetry={() => saveMut.mutate(chosenPreset.id)}
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
            <Btn disabled={!canSave} onClick={() => saveMut.mutate(chosen)}>
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_use')}
            </Btn>
          </div>
        </div>
      )}
      {/* No hand-off: a failed save leaves the reader's unsaved pick on the card, and
          asking the agent unmounts it. */}
      {saveMut.isError && (
        <ErrorNotice variant="inline" message={i18nT('pages.developer.featurePreviewsTab.decisions_provider_save_failed')} />
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
