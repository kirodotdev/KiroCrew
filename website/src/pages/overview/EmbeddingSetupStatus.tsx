/**
 * The embedding setup's own state, inside the Embedding Model card: the model
 * download with its progress bar, "initializing" and "engine starting" while it
 * comes up, a setup error with Retry and its collapsed diagnostic, the
 * degraded-setup warning, and which model makes the vectors. It polls the status every 2 s while setup runs.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Brain, CheckCircle, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { queryReadFailed } from '../../utils/queryReadFailed'
import { embeddingSetupStepLabel, embeddingSetupWarning, embeddingSetupError, embeddingSetupErrorPointer, embeddingSetupDiagnostic, embeddingRepairMessage, enableEmbeddingsErrorMessage, embedModelDisclosure, EMBED_MODEL_PATH_ID, type EmbeddingSetupFields } from './embeddingStatusText'
import { setupRunning, useEmbeddingStatus, useVectorStats } from './useVectorMemoryState'

interface EmbeddingStatus extends EmbeddingSetupFields {
  setup_step?: string
  setup_error?: string
  setup_warning?: string
  provider?: string
  model_available?: boolean
  model_id?: string
  model_dim?: number
  model_source?: string
  model_path?: string
  download_step?: string
  download_attempt?: number
  bytes_downloaded?: number
  bytes_total?: number
}

const extractError = (err: unknown): string => {
  if (err != null && typeof err === 'object' && !(err instanceof Error)) {
    const obj = err as Record<string, unknown>
    if (obj.error != null || obj.detail != null) return String(obj.error ?? obj.detail) || i18nT('pages.overview.vectorMemoryCard.unknown_error')
    if (obj.message) return String(obj.message)
    try { return JSON.stringify(obj) } catch { return i18nT('pages.overview.vectorMemoryCard.unknown_error') }
  }
  const msg = err instanceof Error ? err.message : String(err ?? i18nT('pages.overview.vectorMemoryCard.unknown_error'))
  try { const p = JSON.parse(msg); return String(p?.error ?? p?.detail ?? p?.message ?? msg) || i18nT('pages.overview.vectorMemoryCard.unknown_error') }
  catch { return msg || i18nT('pages.overview.vectorMemoryCard.unknown_error') }
}

// Collapsed raw backend exception behind a localized known-code notice;
// `translate="no"` because it is machine output, not copy.
function SetupDiagnostic({ text }: { text: string }) {
  return (
    <details className="text-[11px] text-muted" data-testid="embedding-setup-diagnostic">
      <summary className="cursor-pointer">{i18nT('memoryV2.view_details')}</summary>
      <pre translate="no" className="mt-1 whitespace-pre-wrap break-words font-mono">{text}</pre>
    </details>
  )
}

/** The download step's words, with megabytes and percent while bytes are known. */
function downloadStepLabel(step: string, status: EmbeddingStatus | null): string {
  const rawStep = status?.download_step
  if (rawStep === 'verifying') return i18nT('pages.overview.vectorMemoryCard.verifying_model_integrity')
  if (rawStep === 'waiting_retry') return i18nT('pages.overview.vectorMemoryCard.retrying_download', { attempt: status?.download_attempt ?? 0 })
  if (step === 'downloading') {
    const dl = status?.bytes_downloaded ?? 0
    const total = status?.bytes_total ?? 0
    if (total > 0) return i18nT('pages.overview.vectorMemoryCard.downloading_embedding_model', { done: (dl / 1e6).toFixed(0), total: (total / 1e6).toFixed(0), pct: Math.round((dl / total) * 100) })
    return i18nT('pages.overview.vectorMemoryCard.downloading_embedding_model_610mb')
  }
  return embeddingSetupStepLabel(step, status?.download_attempt)
}

function downloadPct(status: EmbeddingStatus | null): number | null {
  const dl = status?.bytes_downloaded ?? 0
  const total = status?.bytes_total ?? 0
  return total > 0 && dl > 0 ? Math.min(95, Math.round((dl / total) * 100)) : null
}

export default function EmbeddingSetupStatus() {
  const statsRead = useVectorStats()
  const embeddingRead = useEmbeddingStatus()
  const status = (embeddingRead.data ?? null) as EmbeddingStatus | null
  // The standing-rebuild summary only drives a slow refetch here; the card renders it.
  const { refetch: refetchEmbedding } = embeddingRead
  const { refetch: refetchStats } = statsRead
  const repairing = !!embeddingRepairMessage(status)
  useEffect(() => {
    if (!repairing) return
    const timer = setInterval(() => { void refetchEmbedding() }, 30000)
    return () => clearInterval(timer)
  }, [repairing, refetchEmbedding])
  const [enabling, setEnabling] = useState(false)
  const [startError, setStartError] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const load = useCallback(() => Promise.all([refetchStats(), refetchEmbedding()]), [refetchStats, refetchEmbedding])
  const poll = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      const result = await refetchEmbedding()
      if (result.error || !result.data) return
      const s = result.data as EmbeddingStatus
      if (s.setup_step === 'done' || s.setup_step === 'error' || (s.setup_step === 'idle' && s.setup_error)) {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
        void load().then(() => setEnabling(false))
      }
    }, 2000)
  }, [load, refetchEmbedding])
  const inProgress = setupRunning(status?.setup_step)
  useEffect(() => {
    if (inProgress && !pollRef.current) {
      setEnabling(true)
      poll()
      return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null } }
    }
  }, [inProgress, poll])
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const start = async () => {
    setStartError('')
    setEnabling(true)
    try { await api.vectorEnableEmbeddings(); poll() } catch (error: unknown) { setStartError(enableEmbeddingsErrorMessage(error) || extractError(error)); setEnabling(false) }
  }

  const stats = statsRead.data
  const active = !enabling && ((stats != null && ((stats.semantic_active ?? 0) > 0 || (stats.episodic_active ?? 0) > 0)) || (!!status?.provider && status.provider !== 'none'))
  const warning = embeddingSetupWarning(status)
  const error = embeddingSetupError(status)
  const diagnostic = embeddingSetupDiagnostic(status)
  // The path field sits in this same card, so a path error points at it instead of repeating it.
  const pointer = embeddingSetupErrorPointer(status)
  const failed = queryReadFailed(embeddingRead)
  const disclosure = embedModelDisclosure(status)

  return <div className="mb-3 flex flex-col gap-2 empty:hidden" data-testid="embedding-setup-status">
    {/* The store's counts only tell an idle store from a running one; their read failing hides nothing here. */}
    <ErrorNotice message={embeddingRead.error ? extractError(embeddingRead.error) : undefined} askAgent={false} />
    {failed && <Btn className="w-fit aria-disabled:cursor-default aria-disabled:opacity-30" aria-disabled={embeddingRead.isFetching || undefined} onClick={() => { if (!embeddingRead.isFetching) void load() }}>{i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>}
    {warning && <p role="status" data-testid="embedding-setup-warning" className="flex items-start gap-1.5 text-[12px] text-warn">
      <AlertTriangle className="lucide-inline mt-0.5 shrink-0" />
      <span>{warning}{' '}<a href={`#${EMBED_MODEL_PATH_ID}`} className="whitespace-nowrap underline transition-colors hover:text-accent">{i18nT('pages.overview.vectorMemoryCard.open_embedding_model_settings')}</a></span>
    </p>}
    {/* With inherited vectors the warning above already points at the path field. */}
    {active && error && !(pointer && status?.setup_warning_code === 'legacy_embedding_vectors') && (pointer
      ? <div className="flex flex-wrap items-center gap-1.5 text-[12px]" data-testid="embedding-setup-error-pointer">
        <ErrorNotice message={pointer} variant="inline" askAgent={false} />
        {/* Focuses the path field below. */}
        <a href={`#${EMBED_MODEL_PATH_ID}`} className="whitespace-nowrap text-danger underline transition-colors hover:text-accent">{i18nT('pages.overview.vectorMemoryCard.open_embedding_model_settings')}</a>
      </div>
      : <ErrorNotice message={error} variant="inline" askAgent={false} />)}
    {active && error && diagnostic && <SetupDiagnostic text={diagnostic} />}
    {/* Which model makes the vectors: the file name for a custom one, its path in the tooltip. */}
    {active && disclosure && <p className="truncate text-[11px] text-muted" title={disclosure.title}>{disclosure.label}</p>}
    {!active && !enabling && !failed && !statsRead.isPending && embeddingRead.data && (startError || error
      ? <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <ErrorNotice message={startError || error} variant="inline" askAgent={false} />
          <Btn onClick={start}><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>
        </div>
        {!startError && diagnostic && <SetupDiagnostic text={diagnostic} />}
      </div>
      : <p className="text-sm text-muted">{i18nT(status?.model_available ? 'pages.overview.vectorMemoryCard.model_loaded_embedding_engine_is_starting_up' : 'pages.overview.vectorMemoryCard.vector_memory_is_initializing_the_embedding_mode')}</p>)}
    {enabling && <SetupProgress status={status} error={error} diagnostic={diagnostic} />}
  </div>
}

function SetupProgress({ status, error, diagnostic }: { status: EmbeddingStatus | null; error: string; diagnostic: string }) {
  const step = status?.setup_step || 'checking'
  const steps = ['checking', 'downloading', 'done']
  const bytePct = downloadPct(status)
  const known = step === 'downloading' && bytePct != null
  const pct = step === 'error' ? 0 : known ? bytePct! : Math.max(5, Math.min(95, ((Math.max(0, steps.indexOf(step)) + 1) / steps.length) * 100))
  return <div className="flex items-center gap-3" data-testid="embedding-setup-progress">
    <div className="text-2xl motion-safe:animate-pulse"><Brain className="lucide-inline" /></div>
    <div className="flex-1">
      <div className="mb-1 text-sm font-medium text-text-strong">
        {step !== 'done' && step !== 'error' && downloadStepLabel(step, status)}
        {step === 'done' && <><CheckCircle className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.ready')}</>}
        {step === 'error' && <ErrorNotice message={error || i18nT('pages.overview.vectorMemoryCard.setup_failed')} variant="inline" askAgent={false} />}
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full border border-border bg-bg-elevated">
        <div className={`h-full rounded-full ${known ? 'transition-all duration-1000 ease-out' : step === 'downloading' ? 'animate-[grow_300s_ease-out_forwards]' : 'transition-all duration-700 ease-out'}`}
          style={{ width: known ? `${pct}%` : step === 'downloading' ? undefined : `${pct}%`, background: step === 'error' ? 'var(--danger)' : 'var(--accent)' }} />
      </div>
      <div className="mt-1 text-[12px] text-muted">
        {step === 'downloading' && i18nT('pages.overview.vectorMemoryCard.downloading_from_cdn')}
        {step === 'error' && i18nT('pages.overview.vectorMemoryCard.download_failed_check_network_connectivity_and_t')}
      </div>
      {step === 'error' && diagnostic && <SetupDiagnostic text={diagnostic} />}
    </div>
  </div>
}
