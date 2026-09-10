import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query'
import { Brain, Hourglass, CheckCircle, RefreshCw, Search, AlertTriangle, Check, X } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, SendBtn, Input, Badge } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import ErrorNotice from '../../components/ErrorNotice'
import { memoryQueryRetry } from './MemoryStoreCard'
import { esc } from '../../api/helpers'

import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
import { fmtDateNumeric, fmtDateTimeNumeric } from '../../i18n/format'
const extractError = (err: unknown): string => {
  if (err != null && typeof err === 'object' && !(err instanceof Error)) {
    const obj = err as Record<string, unknown>;
    if (obj.error != null || obj.detail != null) return String(obj.error ?? obj.detail) || i18nT('pages.overview.vectorMemoryCard.unknown_error');
    if (obj.message) return String(obj.message);
    try { return JSON.stringify(obj) } catch { return i18nT('pages.overview.vectorMemoryCard.unknown_error') }
  }
  const msg = err instanceof Error ? err.message : String(err ?? i18nT('pages.overview.vectorMemoryCard.unknown_error'));
  try { const p = JSON.parse(msg); return String(p?.error ?? p?.detail ?? p?.message ?? msg) || i18nT('pages.overview.vectorMemoryCard.unknown_error') }
  catch { return msg || i18nT('pages.overview.vectorMemoryCard.unknown_error') }
}

interface VectorStats {
  migrated?: boolean
  semantic_active?: number
  episodic_active?: number
  embedded_count?: number
  faiss_index_size?: number
  has_legacy_memory?: boolean
}

interface EmbeddingStatus {
  setup_step?: string
  setup_error?: string
  provider?: string
  model_available?: boolean
  model_id?: string
  model_dim?: number
  // 'custom' means a user-supplied GGUF (memory.embed_model_path) is in use and
  // the bundled model is never downloaded; model_path is that file.
  model_source?: string
  model_path?: string
  server_healthy?: boolean
  download_step?: string
  download_attempt?: number
  bytes_downloaded?: number
  bytes_total?: number
}

interface SemanticEntry {
  key: string
  value_json?: unknown
  confidence: number
  source?: string
}

interface EpisodicEntry {
  id: string
  text?: string
  tags?: unknown
  importance: number
  score?: number
  created_at?: string
  ts?: string
}

interface EpisodicPage {
  entries: EpisodicEntry[]
  hasMore: boolean
}

interface AuditEvent {
  event_type: string
  memory_type?: string
  memory_key?: string
  new_value?: string
  old_value?: string
  created_at?: string
}

interface ContextPreview {
  semantic_context?: string
  episodic_context?: string
}

export function parseTags(raw: unknown): string[] {
  let t: unknown = raw || [];
  if (typeof t === 'string') {
    try {
      const parsed = JSON.parse(t);
      t = Array.isArray(parsed) ? parsed : typeof parsed === 'string' ? [parsed] : [t];
    } catch { t = [t]; }
  }
  return Array.isArray(t) ? t : [];
}

// Cap how many semantic rows we render at once. The store can hold thousands of
// entries (vector-only mode); rendering them all synchronously — each row does a
// JSON.parse + JSON.stringify(…, null, 2) + esc() — froze the Settings page for
// 10-20s on open. The full set stays in memory for key-suggestion
// dedup; only the rendered window is bounded. Filter to reach entries past the cap.
export const SEMANTIC_RENDER_CAP = 100

// Render a semantic value exactly as the table cell shows it: parse a JSON
// string, then pretty-print objects (plain String() for scalars). Shared by
// the row renderer and the filter so filtering on visible value text matches
// what's on screen — and object values match by content, not "[object Object]".
export function semanticValueText(e: { value_json?: unknown }): string {
  let val: unknown = e?.value_json
  if (typeof val === 'string') { try { val = JSON.parse(val) } catch { /* raw string, keep as-is */ } }
  return typeof val === 'object' && val !== null ? JSON.stringify(val, null, 2) : String(val ?? '')
}

// Turn a raw embedding model id (e.g. "qwen3-embedding:0.6b") into a friendly
// display name (e.g. "Qwen3-Embedding-0.6B") for disclosure in the UI. Falls
// back to the raw id for any shape it doesn't recognise, so a future model
// swap still discloses *something* rather than silently blanking.
export function formatEmbedModel(modelId?: string): string {
  const raw = (modelId ?? '').trim()
  if (!raw) return ''
  const [base, tag] = raw.split(':')
  const pretty = base.split('-').map(p => p ? p[0].toUpperCase() + p.slice(1) : p).join('-')
  return tag ? `${pretty}-${tag.toUpperCase()}` : pretty
}

// Build the embedding-model disclosure shown under the EMBEDDINGS badge:
// a short label (friendly model name + vector dimension) plus a fuller tooltip.
// The model name is a technical identifier; the surrounding copy is localized.
// Returns null when no model id is known, so the disclosure line is omitted.
export function embedModelDisclosure(status?: EmbeddingStatus | null): { label: string; title: string } | null {
  // A custom model's id is either operator-chosen or derived as
  // 'custom:<file>:<size>'; neither reads well through formatEmbedModel, so
  // label it by filename and put the full path in the tooltip.
  const isCustom = status?.model_source === 'custom'
  const customFile = (status?.model_path ?? '').split(/[\\/]/).pop() ?? ''
  const name = isCustom && customFile ? customFile : formatEmbedModel(status?.model_id)
  if (!name) return null
  const dim = status?.model_dim
  const label = dim ? i18nT('pages.overview.vectorMemoryCard.embed_model_label', { model: name, dim }) : name
  const baseTitle = dim
    ? i18nT('pages.overview.vectorMemoryCard.embed_model_runs_locally', { model: status?.model_id, dim })
    : String(status?.model_id ?? '')
  // Technical identifiers only — no new localized copy needed.
  const title = isCustom && status?.model_path ? `${baseTitle} — ${status.model_path}` : baseTitle
  return { label, title }
}

export default function VectorMemoryCard({ onActiveChange, onMigratedChange, diagnosticsOnly = false }: { onActiveChange?: (active: boolean) => void; onMigratedChange?: (migrated: boolean) => void; diagnosticsOnly?: boolean }) {
  // One instance covers every input in this card; the binding's focus/blur reset makes sharing safe.
  const ime = useImeGuard()
  const queryClient = useQueryClient()
  const statsRead = useQuery({ queryKey: ['member-memory', 'default', 'vector-stats'], queryFn: () => api.vectorStats(), staleTime: 0, retry: false })
  const embeddingRead = useQuery({ queryKey: ['member-memory', 'default', 'embedding-status'], queryFn: async () => {
    const status = await api.vectorEmbeddingStatus()
    if (!status) throw new Error(i18nT('pages.overview.vectorMemoryCard.unknown_error'))
    return status
  }, staleTime: 0, retry: false })
  const semanticRead = useQuery({ queryKey: ['member-memory', 'default', 'semantic-browser'], queryFn: () => api.vectorSemantic(), enabled: !diagnosticsOnly, staleTime: 0, retry: false })
  const stats = statsRead.data as VectorStats | undefined
  const embStatus = (embeddingRead.data ?? null) as EmbeddingStatus | null
  const semantic = useMemo(() => (semanticRead.data?.entries ?? []) as SemanticEntry[], [semanticRead.data])
  const [episodic, setEpisodic] = useState<EpisodicEntry[]>([])
  const [epQuery, setEpQuery] = useState('')
  const [epTagFilter, setEpTagFilter] = useState<string|null>(null)
  const [epRequest, setEpRequest] = useState({ query: '', tag: null as string | null })
  const [newKey, setNewKey] = useState(''); const [newVal, setNewVal] = useState('')
  const [enabling, setEnabling] = useState(false)
  const [view, setView] = useState<'semantic'|'episodic'|'audit'|'inspector'>(diagnosticsOnly ? 'audit' : 'semantic')
  const [editKey, setEditKey] = useState<string|null>(null); const [editVal, setEditVal] = useState('')
  const [eventFilter, setEventFilter] = useState<string>('all')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const audit = useInfiniteQuery({
    queryKey: ['member-memory', 'default', 'audit-events'],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.vectorEvents(50, pageParam) as Promise<{ events: AuditEvent[] }>,
    getNextPageParam: (last, pages) => last.events.length === 50 ? pages.length * 50 : undefined,
    enabled: view === 'audit',
    staleTime: 0,
    retry: memoryQueryRetry,
  })
  const events = useMemo(() => audit.data?.pages.flatMap(page => page.events) ?? [], [audit.data])
  const [inspectorQuery, setInspectorQuery] = useState(''); const [preview, setPreview] = useState<ContextPreview | null>(null)
  const [writeError, setWriteError] = useState('')
  const [embeddingStartError, setEmbeddingStartError] = useState('')
  const [semFilter, setSemFilter] = useState('')

  const ALLOWLIST_PREFIXES = useMemo(() => [
    'pref.frontend.', 'pref.backend.', 'pref.streaming.', 'pref.editor.', 'pref.os', 'pref.shell',
    'pref.style.', 'pref.communication.', 'pref.testing.', 'pref.deployment.',
    'project.name', 'project.repo', 'project.stack', 'project.storage', 'project.description',
    'user.name', 'user.timezone', 'user.team', 'user.role',
  ], [])

  const keySuggestions = useMemo(() => {
    const existing = new Set(semantic.map(e => e.key))
    return ALLOWLIST_PREFIXES.filter(p => !existing.has(p.replace(/\.$/, '')))
  }, [semantic, ALLOWLIST_PREFIXES])

  const filteredKeys = useMemo(() => {
    if (!newKey) return keySuggestions.slice(0, 8)
    return keySuggestions.filter(k => k.startsWith(newKey)).slice(0, 8)
  }, [newKey, keySuggestions])

  const filteredSemantic = useMemo(() => {
    const q = semFilter.trim().toLowerCase()
    if (!q) return semantic
    return semantic.filter((e) =>
      String(e.key ?? '').toLowerCase().includes(q) || semanticValueText(e).toLowerCase().includes(q))
  }, [semantic, semFilter])
  const visibleSemantic = useMemo(() => filteredSemantic.slice(0, SEMANTIC_RENDER_CAP), [filteredSemantic])

  const { refetch: refetchStats } = statsRead
  const { refetch: refetchEmbedding } = embeddingRead
  const { refetch: refetchSemantic } = semanticRead
  const load = useCallback(() => Promise.all([
    refetchStats(), refetchEmbedding(), ...(diagnosticsOnly ? [] : [refetchSemantic()]),
  ]), [refetchStats, refetchEmbedding, refetchSemantic, diagnosticsOnly])

  useEffect(() => { if (stats?.migrated != null) onMigratedChange?.(stats.migrated) }, [stats?.migrated, onMigratedChange])

  const pollEmbeddingStatus = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      const result = await refetchEmbedding()
      if (result.error || !result.data) return
      const s = result.data
      if (s.setup_step === 'done' || s.setup_step === 'error' || (s.setup_step === 'idle' && s.setup_error)) {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
        // setup_error surfaces directly from embStatus in the error state.
        load().then(() => setEnabling(false))
      }
    }, 2000)
  }, [load, refetchEmbedding])

  const setupInProgress = !!embStatus?.setup_step
    && embStatus.setup_step !== 'idle'
    && embStatus.setup_step !== 'done'
    && embStatus.setup_step !== 'error'

  useEffect(() => {
    if (setupInProgress && !pollRef.current) {
      setEnabling(true)
      pollEmbeddingStatus()
      return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null } }
    }
  }, [setupInProgress, pollEmbeddingStatus])

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  // Query keys capture the submitted filters, separate from the editable input.
  // A rejected read leaves the previous rows and any unsaved drafts in place.
  const episodicRead = useInfiniteQuery({
    queryKey: ['member-memory', 'default', 'episodic-browser', epRequest.query, epRequest.tag],
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      const data = epRequest.query
        ? await api.vectorEpisodicSearch(epRequest.query, epRequest.tag || undefined)
        : await api.vectorEpisodic(50, pageParam, epRequest.tag || undefined)
      const entries = (data?.results || data?.entries || []) as EpisodicEntry[]
      return { entries, hasMore: !epRequest.query && entries.length === 50 }
    },
    getNextPageParam: (last, pages) => last.hasMore ? pages.reduce((offset, page) => offset + page.entries.length, 0) : undefined,
    enabled: view === 'episodic',
    staleTime: 0,
    retry: false,
  })
  useEffect(() => { if (episodicRead.data) setEpisodic(episodicRead.data.pages.flatMap(page => page.entries)) }, [episodicRead.data])
  const deleteEpisodic = useMutation({ mutationFn: async (id: string) => {
    await api.vectorEpisodicDelete(id)
    await queryClient.cancelQueries({ queryKey: ['member-memory', 'default', 'episodic-browser'] })
    queryClient.setQueriesData<InfiniteData<EpisodicPage, number>>(
      { queryKey: ['member-memory', 'default', 'episodic-browser'] },
      previous => previous ? {
        ...previous,
        pages: previous.pages.map(page => ({ ...page, entries: page.entries.filter(entry => entry.id !== id) })),
      } : previous,
    )
    setEpisodic(previous => previous.filter(entry => entry.id !== id))
  } })
  const loadEpisodic = (q?: string, append = false, tag?: string | null) => {
    if (episodicRead.isFetching) return
    if (append) { void episodicRead.fetchNextPage(); return }
    const query = q ?? epQuery
    const activeTag = tag !== undefined ? tag : epTagFilter
    if (query === epRequest.query && activeTag === epRequest.tag) void episodicRead.refetch()
    else setEpRequest({ query, tag: activeTag })
  }
  const [previewQuery, setPreviewQuery] = useState<string | undefined>()
  const previewRead = useQuery({
    queryKey: ['member-memory', 'default', 'context-preview', previewQuery ?? ''],
    queryFn: () => api.vectorContextPreview(previewQuery),
    enabled: view === 'inspector',
    staleTime: 0,
    retry: false,
  })
  useEffect(() => { if (previewRead.data !== undefined) setPreview(previewRead.data) }, [previewRead.data])
  const loadPreview = (q?: string) => {
    if (previewRead.isFetching) return
    if ((q ?? '') === (previewQuery ?? '')) void previewRead.refetch()
    else setPreviewQuery(q)
  }

  const confidenceBadge = (c: number) => {
    const v = typeof c === 'number' ? c.toFixed(2) : c
    if (c >= 0.95) return <Badge variant="ok">● {v}</Badge>
    if (c >= 0.8) return <Badge variant="warn">● {v}</Badge>
    return <Badge variant="err">● {v}</Badge>
  }

  const active = !enabling && ((stats != null && ((stats.semantic_active ?? 0) > 0 || (stats.episodic_active ?? 0) > 0)) || (embStatus?.provider && embStatus.provider !== 'none'))
  const filteredEvents = eventFilter === 'all' ? events : events.filter(e => e.event_type === eventFilter)
  const eventTypes = useMemo(() => [...new Set(events.map(e => e.event_type))], [events])

  useEffect(() => { onActiveChange?.(!!active) }, [active, onActiveChange])

  const summaryError = statsRead.error || embeddingRead.error
  if (statsRead.isPending && !summaryError && !semanticRead.isError && !audit.isError) return <Card><CardTitle><Brain className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.vector_memory')}</CardTitle><p role="status" className="text-muted text-sm">{i18nT('pages.overview.vectorMemoryCard.loading')}</p></Card>

  const startEmbeddings = async () => {
    setEmbeddingStartError('')
    setEnabling(true)
    try {
      await api.vectorEnableEmbeddings()
      pollEmbeddingStatus()
    } catch (error: unknown) {
      setEmbeddingStartError(extractError(error))
      setEnabling(false)
    }
  }

  // Derive the download step label from the raw status
  const downloadStepLabel = (step: string, status: EmbeddingStatus | null): string => {
    const rawStep = status?.download_step
    if (rawStep === 'verifying') return i18nT('pages.overview.vectorMemoryCard.verifying_model_integrity')
    if (rawStep === 'waiting_retry') {
      const attempt = status?.download_attempt ?? 0
      return i18nT('pages.overview.vectorMemoryCard.retrying_download', { attempt })
    }
    if (step === 'downloading') {
      const dl = status?.bytes_downloaded ?? 0
      const total = status?.bytes_total ?? 0
      if (total > 0) {
        const pctDone = Math.round((dl / total) * 100)
        const dlMB = (dl / 1e6).toFixed(0)
        const totalMB = (total / 1e6).toFixed(0)
        return i18nT('pages.overview.vectorMemoryCard.downloading_embedding_model', { done: dlMB, total: totalMB, pct: pctDone })
      }
      return i18nT('pages.overview.vectorMemoryCard.downloading_embedding_model_610mb')
    }
    return step
  }

  // Compute determinate progress percentage from byte counts when available
  const downloadPct = (status: EmbeddingStatus | null): number | null => {
    const dl = status?.bytes_downloaded ?? 0
    const total = status?.bytes_total ?? 0
    if (total > 0 && dl > 0) return Math.min(95, Math.round((dl / total) * 100))
    return null
  }

  return (<>
    <Card>
      <CardTitle><Brain className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.vector_memory')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.structured_semantic_key_value_episodic_conversat')} /></CardTitle>
      {/* No hand-off: this card and its parent retain unsaved memory values and editing drafts. */}
      <ErrorNotice message={statsRead.error ? extractError(statsRead.error) : undefined} />
      {/* No hand-off: a status retry must not discard the same unsaved memory drafts. */}
      <ErrorNotice message={embeddingRead.error ? extractError(embeddingRead.error) : undefined} />
      {summaryError && <Btn disabled={statsRead.isFetching || embeddingRead.isFetching} onClick={() => void load()}>{i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>}
      {!active && !enabling && !summaryError && (
        <div className="flex flex-col gap-3 items-start">
          {embeddingStartError || embStatus?.setup_error
            ? (
              <div className="flex items-center gap-2">
                {/* No hand-off: retry here preserves all unsaved memory drafts. */}
                <ErrorNotice message={embeddingStartError || embStatus?.setup_error} variant="inline" />
                <Btn onClick={startEmbeddings}><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>
              </div>
            )
            : embStatus?.model_available
              ? <p className="text-sm text-muted">{i18nT('pages.overview.vectorMemoryCard.model_loaded_embedding_engine_is_starting_up')}</p>
              : <p className="text-sm text-muted">{i18nT('pages.overview.vectorMemoryCard.vector_memory_is_initializing_the_embedding_mode')}</p>
          }
        </div>
      )}
      {enabling && (() => {
        const step = embStatus?.setup_step || 'checking'
        const steps = ['checking', 'downloading', 'done']
        const idx = steps.indexOf(step)
        const bytePct = downloadPct(embStatus)
        const pct = step === 'error' ? 0
          : step === 'downloading' && bytePct != null ? bytePct
          : Math.max(5, Math.min(95, ((Math.max(0, idx) + 1) / steps.length) * 100))
        const hasDeterminatePct = step === 'downloading' && bytePct != null
        return (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-3">
              <div className="text-2xl animate-pulse"><Brain className="lucide-inline" /></div>
              <div className="flex-1">
                <div className="text-sm font-medium text-text-strong mb-1">
                  {step === 'checking' && i18nT('pages.overview.vectorMemoryCard.checking_system_status')}
                  {step === 'downloading' && downloadStepLabel(step, embStatus)}
                  {step === 'done' && <><CheckCircle className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.ready')}</>}
                  {/* No hand-off: the card may still hold unsaved memory drafts. */}
                  {step === 'error' && <ErrorNotice message={embStatus?.setup_error || i18nT('pages.overview.vectorMemoryCard.setup_failed')} variant="inline" />}
                </div>
                <div className="w-full bg-bg-elevated rounded-full h-2 border border-border overflow-hidden">
                  <div className={`h-full rounded-full ${hasDeterminatePct ? 'transition-all duration-1000 ease-out' : step === 'downloading' ? 'animate-[grow_300s_ease-out_forwards]' : 'transition-all duration-700 ease-out'}`}
                    style={{ width: hasDeterminatePct ? `${pct}%` : step === 'downloading' ? undefined : `${pct}%`, background: step === 'error' ? 'var(--danger)' : 'var(--accent)' }} />
                </div>
                <div className="text-[12px] text-muted mt-1">
                  {step === 'downloading' && i18nT('pages.overview.vectorMemoryCard.downloading_from_cdn')}
                  {step === 'error' && i18nT('pages.overview.vectorMemoryCard.download_failed_check_network_connectivity_and_t')}
                </div>
              </div>
            </div>
          </div>
        )
      })()}
      {active && (
        <div className="flex flex-col gap-3">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              { key: 'semantic', label: i18nT('pages.overview.vectorMemoryCard.semantic'), value: stats ? stats.semantic_active ?? 0 : '…' },
              { key: 'episodic', label: i18nT('pages.overview.vectorMemoryCard.episodic'), value: stats ? stats.episodic_active ?? 0 : '…' },
              { key: 'embedded', label: i18nT('pages.overview.vectorMemoryCard.embedded'), value: stats ? stats.embedded_count ?? stats.faiss_index_size ?? 0 : '…' },
            ].map(s => (
              <div key={s.key} className="stat-accent relative overflow-hidden bg-bg-elevated rounded-md px-3 py-2 border border-border">
                <div className="text-muted text-[11px] uppercase tracking-wider">{s.label}</div>
                <div className="text-lg font-bold text-text-strong">{s.value}</div>
              </div>
            ))}
            <div className="stat-accent relative overflow-hidden bg-bg-elevated rounded-md px-3 py-2 border border-border">
              <div className="text-muted text-[11px] uppercase tracking-wider">{i18nT('pages.overview.vectorMemoryCard.embeddings')}</div>
              <div className="text-lg font-bold">
                {embStatus?.setup_step && embStatus.setup_step !== 'idle' && embStatus.setup_step !== 'done'
                  ? <Badge variant="warn"><Hourglass className="lucide-inline" /> {embStatus.setup_step}</Badge>
                  : (() => {
                      const modelOk = embStatus?.model_available ?? embStatus?.server_healthy;
                      if (!modelOk) return <Badge variant="warn"><AlertTriangle className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.model_loading')}</Badge>;
                      return <Badge variant="ok"><Check className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.active')}</Badge>;
                    })()
                }
              </div>
              {embedModelDisclosure(embStatus) && (
                <div className="text-muted text-[11px] mt-1 font-normal truncate" title={embedModelDisclosure(embStatus)!.title}>
                  {embedModelDisclosure(embStatus)!.label}
                </div>
              )}
            </div>
          </div>
          <div className="flex gap-2 flex-wrap items-center">
            <div className="inline-flex items-center gap-1 p-1 rounded-md bg-bg-elevated w-fit">
            {(['semantic','episodic','audit','inspector'] as const).filter(v => !diagnosticsOnly || v === 'audit' || v === 'inspector').map(v => (
              <button key={v} onClick={() => {
                setView(v)
                if (v === 'episodic') {
                  if (view === v) loadEpisodic()
                  else setEpRequest({ query: epQuery, tag: epTagFilter })
                }
                if (v === 'audit' && view === v) void audit.refetch()
                if (v === 'inspector' && view === v) loadPreview(previewQuery)
              }}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[13px] font-medium cursor-pointer border-none transition-colors ${view === v ? 'bg-bg-hover text-accent' : 'bg-transparent text-muted hover:text-text'}`}>{
                  v === 'inspector' ? <><Search className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.inspector')}</> : v[0].toUpperCase() + v.slice(1)
                }</button>
            ))}
            </div>
          </div>
        </div>
      )}
    </Card>

    {(active || semanticRead.isError) && view === 'semantic' && !diagnosticsOnly && (
      <Card>
        <CardTitle>{i18nT('pages.overview.vectorMemoryCard.semantic_memory')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.structured_key_value_facts_about_you_confidence')} /></CardTitle>
        {/* No hand-off: retain new and inline memory drafts while retrying the read. */}
        <ErrorNotice message={semanticRead.error ? extractError(semanticRead.error) : undefined} />
        {semanticRead.isError && <Btn disabled={semanticRead.isFetching} onClick={() => void refetchSemantic()}>{i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>}
        {semanticRead.isFetching && <p role="status" className="text-muted text-sm">{i18nT('pages.overview.vectorMemoryCard.loading')}</p>}
        {/* Two inputs and a button. `flex-wrap` so the button drops to its own
            line at a narrow width instead of the three of them competing: with
            no wrap the inputs cannot shrink past their intrinsic minimum, so the
            row overflows and the button leaves the viewport. */}
        <div className="flex gap-2 items-center flex-wrap mb-3 relative">
          <div className="relative" style={{ flex: 1 }}>
            <Input placeholder={i18nT('pages.overview.vectorMemoryCard.key_e_g_pref_backend_framework')} value={newKey} onChange={e => { setNewKey(e.target.value); setWriteError('') }}
              list="key-suggestions" className="w-full" />
            <datalist id="key-suggestions">{filteredKeys.map(k => <option key={k} value={k}>{k}</option>)}</datalist>
          </div>
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.value')} style={{ flex: 2 }} value={newVal} onChange={e => { setNewVal(e.target.value); setWriteError('') }}
            {...ime.bindEnter({ onEnter: async () => { if (!newKey || !newVal) return; try { await api.vectorSemanticWrite(newKey, newVal); setNewKey(''); setNewVal(''); setWriteError(''); load() } catch (err: unknown) { setWriteError(extractError(err)) } } })} />
          <SendBtn onClick={async () => { if (!newKey || !newVal) return; try { await api.vectorSemanticWrite(newKey, newVal); setNewKey(''); setNewVal(''); setWriteError(''); load() } catch (e: unknown) { setWriteError(extractError(e)) } }}>{i18nT('pages.overview.vectorMemoryCard.set')}</SendBtn>
        </div>
        {/* No hand-off: preserve the rejected value and inline edit for retry. */}
        <ErrorNotice message={writeError} className="mb-2" />
        <div className="flex gap-2 items-center flex-wrap mb-3">
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.filter_by_key_or_value')} style={{ flex: 1 }} value={semFilter} onChange={e => { setSemFilter(e.target.value); setEditKey(null) }} />
          {semFilter && <Btn onClick={() => { setSemFilter(''); setEditKey(null) }}>{i18nT('pages.overview.vectorMemoryCard.clear')}</Btn>}
        </div>
        <div className="max-h-[500px] overflow-y-auto">
        <table className="w-full border-collapse table-striped"><thead><tr>
          {['Key','Value','Confidence','Source',''].map(h => <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium sticky top-0 bg-card z-10">{h}</th>)}
        </tr></thead><tbody>
          {filteredSemantic.length === 0 && semanticRead.isSuccess && !semanticRead.isFetching ? <tr><td colSpan={5} className="text-muted italic px-2.5 py-3.5 text-sm">{semFilter ? i18nT('pages.overview.vectorMemoryCard.no_matching_entries') : i18nT('pages.overview.vectorMemoryCard.no_semantic_entries')}</td></tr> : visibleSemantic.map(e => {
            const valStr = semanticValueText(e)
            const isEditing = editKey === e.key
            return (
              <tr key={e.key} className="hover:bg-bg-hover transition-colors group">
                <td className="px-2.5 py-2 border-b border-border text-sm font-mono text-accent/80">{esc(e.key)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm cursor-pointer max-w-[400px]" onClick={() => { if (!isEditing) { setEditKey(e.key); setEditVal(valStr) } }}>
                  {isEditing ? (
                    // Presentational wrapper: stops the parent cell's edit-trigger
                    // click from firing while interacting with the edit controls.
                    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
                    <div className="flex gap-1 items-center" onClick={ev => ev.stopPropagation()}>
                      <Input value={editVal} onChange={ev => setEditVal(ev.target.value)} className="!py-1 !px-2 !text-sm"
                        {...ime.bindEnter({ onEnter: async () => { try { await api.vectorSemanticWrite(e.key, editVal); setEditKey(null); setWriteError(''); load() } catch (err: unknown) { setWriteError(extractError(err)) } }, onEscape: () => setEditKey(null) })}
                        autoFocus />
                      <Btn onClick={async () => { try { await api.vectorSemanticWrite(e.key, editVal); setEditKey(null); setWriteError(''); load() } catch (err: unknown) { setWriteError(extractError(err)) } }}><Check className="lucide-inline" /></Btn>
                      <Btn onClick={() => setEditKey(null)}><X className="lucide-inline" /></Btn>
                    </div>
                  ) : (
                    <span className="break-words whitespace-pre-wrap group-hover:underline group-hover:decoration-dotted group-hover:underline-offset-2">{esc(valStr)}</span>
                  )}
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{confidenceBadge(e.confidence)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant={e.source === 'user_explicit' ? 'aim' : 'ok'}>{e.source}</Badge></td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><Btn danger onClick={async () => { try { await api.vectorSemanticDelete(e.key); setWriteError(''); load() } catch (err: unknown) { setWriteError(extractError(err)) } }}>{i18nT('pages.overview.vectorMemoryCard.delete')}</Btn></td>
              </tr>
            )
          })}
        </tbody></table>
        </div>
        {filteredSemantic.length > 0 && (
          <p className="text-muted text-[12px] mt-2">
            {i18nT('pages.overview.vectorMemoryCard.showing')} {visibleSemantic.length} {i18nT('pages.overview.vectorMemoryCard.of')} {filteredSemantic.length}{filteredSemantic.length !== semantic.length ? ` (filtered from ${semantic.length})` : ''}{filteredSemantic.length > visibleSemantic.length ? <> {i18nT('pages.overview.vectorMemoryCard.refine_your_filter_to_narrow_further')}</> : ''}
          </p>
        )}
      </Card>
    )}

    {active && view === 'episodic' && (
      <Card>
        <CardTitle>{i18nT('pages.overview.vectorMemoryCard.episodic_memory')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.conversation_fragments_with_vector_search_import')} /></CardTitle>
        {/* No hand-off: retain the query, loaded rows and other memory drafts. */}
        <ErrorNotice message={episodicRead.error ? extractError(episodicRead.error) : undefined} />
        {episodicRead.isError && <Btn disabled={episodicRead.isFetching} onClick={() => void (episodicRead.isFetchNextPageError ? episodicRead.fetchNextPage() : episodicRead.refetch())}>{i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>}
        {episodicRead.isFetching && <p role="status" className="text-muted text-sm">{i18nT('pages.overview.vectorMemoryCard.loading')}</p>}
        <div className="flex gap-2 items-center flex-wrap mb-3">
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.search_episodic_memories')} style={{ flex: 1 }} value={epQuery} onChange={e => setEpQuery(e.target.value)}
            {...ime.bindEnter({ onEnter: () => loadEpisodic() })} />
          <SendBtn disabled={episodicRead.isFetching} onClick={() => loadEpisodic()}>{i18nT('pages.overview.vectorMemoryCard.search')}</SendBtn>
          {epQuery && <Btn disabled={episodicRead.isFetching} onClick={() => { setEpQuery(''); setEpTagFilter(null); loadEpisodic('', false, null) }}>{i18nT('pages.overview.vectorMemoryCard.clear')}</Btn>}
          {!epQuery && epTagFilter && <Btn disabled={episodicRead.isFetching} onClick={() => { setEpTagFilter(null); loadEpisodic('', false, null) }}>{i18nT('pages.overview.vectorMemoryCard.clear')}</Btn>}
        </div>
        {episodic.length > 0 && (() => {
          const allTags = [...new Set(episodic.flatMap(e => parseTags(e.tags)))]
          return allTags.length > 0 ? (
            <div className="flex gap-1.5 flex-wrap mb-3">
              <span className="text-muted text-[12px] self-center mr-1">{i18nT('pages.overview.vectorMemoryCard.filter_by_tag')}</span>
              {allTags.map((tag: string) => (
                <button key={tag} disabled={episodicRead.isFetching} onClick={() => { const t = epTagFilter === tag ? null : tag; setEpTagFilter(t); setEpQuery(''); loadEpisodic('', false, t) }}
                  className={`px-2 py-0.5 rounded-full text-[12px] border transition-colors cursor-pointer ${epTagFilter === tag ? 'bg-warn/30 text-warn border-warn/40' : 'bg-ok-subtle text-ok border-ok/20 hover:bg-ok/20'}`}>{tag}</button>
              ))}
            </div>
          ) : null
        })()}
        <div className="max-h-[500px] overflow-y-auto">
        <table className="w-full border-collapse table-striped"><thead><tr>
          {[
            i18nT('pages.overview.vectorMemoryCard.column_text'),
            i18nT('components.slotTagPopover.tags'),
            i18nT('pages.overview.vectorMemoryCard.column_importance'),
            ...(epRequest.query ? [i18nT('pages.overview.vectorMemoryCard.column_score')] : []),
            i18nT('pages.overview.memoryTab.when'),
            '',
          ].map(h => <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium sticky top-0 bg-card z-10">{h}</th>)}
        </tr></thead><tbody>
          {episodic.length === 0 && episodicRead.isSuccess && !episodicRead.isFetching ? <tr><td colSpan={epRequest.query ? 6 : 5} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.overview.vectorMemoryCard.no_episodic_entries')}</td></tr> : episodic.map(e => {
            const tags = parseTags(e.tags);
            return (
              <tr key={e.id} className="hover:bg-bg-hover transition-colors">
                <td className="px-2.5 py-2 border-b border-border text-sm max-w-[450px]"><span className="break-words whitespace-pre-wrap">{esc(e.text)}</span></td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><div className="flex gap-1 flex-wrap">{tags.map((t: string) => <Badge key={t} variant="ok">{t}</Badge>)}</div></td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{confidenceBadge(e.importance)}</td>
                {epRequest.query && <td className="px-2.5 py-2 border-b border-border text-sm font-mono text-[12px]">{e.score != null ? e.score.toFixed(3) : '—'}</td>}
                <td className="px-2.5 py-2 border-b border-border text-sm text-muted whitespace-nowrap">{(() => { const m = e.text?.match(/^\[(\d{4}-\d{2}-\d{2})/); if (m) return m[1]; const raw = e.created_at || e.ts || ''; const d = new Date(raw.replace(' ', 'T') + (raw.includes('+') || raw.includes('Z') ? '' : 'Z')); return isNaN(d.getTime()) ? '—' : fmtDateNumeric(d) })()}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  <Btn danger disabled={deleteEpisodic.isPending} onClick={() => deleteEpisodic.mutate(e.id)}>{i18nT(deleteEpisodic.isError && deleteEpisodic.variables === e.id ? 'pages.overview.vectorMemoryCard.retry' : 'pages.overview.vectorMemoryCard.delete')}</Btn>
                  {/* No hand-off: keep this row, its search and all unsaved memory drafts. */}
                  <ErrorNotice message={deleteEpisodic.isError && deleteEpisodic.variables === e.id ? extractError(deleteEpisodic.error) : undefined} />
                </td>
              </tr>
            )
          })}
        </tbody></table>
        </div>
        {episodicRead.hasNextPage && <div className="flex justify-center mt-3"><Btn disabled={episodicRead.isFetching} onClick={() => loadEpisodic(undefined, true)}>{i18nT('pages.overview.vectorMemoryCard.load_more')}</Btn></div>}
        {episodic.length > 0 && <p className="text-muted text-[12px] mt-2">{i18nT('pages.overview.vectorMemoryCard.showing')} {episodic.length} {i18nT('pages.overview.vectorMemoryCard.entries')}</p>}
      </Card>
    )}

    {(active || audit.isError) && view === 'audit' && (
      <Card>
        <CardTitle>{i18nT('pages.overview.vectorMemoryCard.audit_trail')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.every_memory_create_update_delete_conflict_and_i')} /></CardTitle>
        {/* No hand-off: this card and its parent retain unsaved memory values and editing drafts. */}
        <ErrorNotice message={audit.error ? extractError(audit.error) : undefined} />
        {audit.isError && <Btn disabled={audit.isFetching} onClick={() => void (audit.isFetchNextPageError ? audit.fetchNextPage() : audit.refetch())}>{i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>}
        <div className="flex gap-1.5 flex-wrap mb-3">
          <Btn onClick={() => setEventFilter('all')} className={eventFilter === 'all' ? '!border-accent !text-accent' : ''}>{i18nT('pages.overview.vectorMemoryCard.all')}</Btn>
          {eventTypes.map((t: string) => (
            <Btn key={t} onClick={() => setEventFilter(t)} className={eventFilter === t ? '!border-accent !text-accent' : ''}>{t}</Btn>
          ))}
        </div>
        <div className="max-h-[500px] overflow-y-auto">
        <table className="w-full border-collapse table-striped"><thead><tr>
          {['Event','Key/Type','Details','When'].map(h => <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium sticky top-0 bg-card z-10">{h}</th>)}
        </tr></thead><tbody>
          {filteredEvents.length === 0 && audit.isSuccess ? <tr><td colSpan={4} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.overview.vectorMemoryCard.no_events')}</td></tr> : filteredEvents.map((e, i: number) => (
            <tr key={i} className="hover:bg-bg-hover transition-colors">
              <td className="px-2.5 py-2 border-b border-border text-sm">
                <Badge variant={e.event_type.includes('block') || e.event_type.includes('reject') ? 'err' : e.event_type.includes('skip') ? 'warn' : 'ok'}>{e.event_type}</Badge>
              </td>
              <td className="px-2.5 py-2 border-b border-border text-sm font-mono text-[12px]">{esc(e.memory_type === 'episodic' ? `episodic` : (e.memory_key || e.memory_type || ''))}</td>
              <td className="px-2.5 py-2 border-b border-border text-sm max-w-[350px]"><span className="break-words whitespace-pre-wrap">{esc(e.new_value || e.old_value || '')}</span></td>
              <td className="px-2.5 py-2 border-b border-border text-sm text-muted whitespace-nowrap">{e.created_at ? fmtDateTimeNumeric(e.created_at.replace(' ', 'T') + (e.created_at.includes('+') || e.created_at.includes('Z') ? '' : 'Z')) : '—'}</td>
            </tr>
          ))}
        </tbody></table>
        </div>
        {audit.hasNextPage && <div className="flex justify-center mt-3"><Btn disabled={audit.isFetching} onClick={() => void audit.fetchNextPage()}>{i18nT('pages.overview.vectorMemoryCard.load_more')}</Btn></div>}
        {events.length > 0 && <p className="text-muted text-[12px] mt-2">{i18nT('pages.overview.vectorMemoryCard.showing')} {filteredEvents.length} {i18nT('pages.overview.vectorMemoryCard.events')}{eventFilter !== 'all' ? ` (${events.length} total)` : ''}</p>}
      </Card>
    )}

    {active && view === 'inspector' && (
      <Card>
        <CardTitle><Search className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.memory_inspector')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.preview_what_gets_injected_into_prompts_enter_a')} /></CardTitle>
        {/* No hand-off: retain the inspector query, prior context and memory drafts. */}
        <ErrorNotice message={previewRead.error ? extractError(previewRead.error) : undefined} />
        {previewRead.isError && <Btn disabled={previewRead.isFetching} onClick={() => void previewRead.refetch()}>{i18nT('pages.overview.vectorMemoryCard.retry')}</Btn>}
        {previewRead.isFetching && <p role="status" className="text-muted text-sm">{i18nT('pages.overview.vectorMemoryCard.loading')}</p>}
        <div className="flex gap-2 items-center flex-wrap mb-3">
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.test_query_e_g_what_database_should_i_use')} style={{ flex: 1 }} value={inspectorQuery} onChange={e => setInspectorQuery(e.target.value)}
            {...ime.bindEnter({ onEnter: () => loadPreview(inspectorQuery) })} />
          <SendBtn disabled={previewRead.isFetching} onClick={() => loadPreview(inspectorQuery)}>{i18nT('pages.overview.vectorMemoryCard.preview')}</SendBtn>
        </div>
        {preview && (
          <div className="flex flex-col gap-3">
            {preview.semantic_context && (
              <div>
                <div className="text-muted text-[12px] uppercase tracking-wider mb-1.5">{i18nT('pages.overview.vectorMemoryCard.semantic_context_injected_at_session_start')}</div>
                <pre className="bg-bg-elevated border border-border rounded-md p-3 text-sm font-mono text-text overflow-x-auto whitespace-pre-wrap max-h-[200px] overflow-y-auto">{preview.semantic_context || '(empty)'}</pre>
              </div>
            )}
            {preview.episodic_context && (
              <div>
                <div className="text-muted text-[12px] uppercase tracking-wider mb-1.5">{i18nT('pages.overview.vectorMemoryCard.episodic_context_injected_per_message')}</div>
                <pre className="bg-bg-elevated border border-border rounded-md p-3 text-sm font-mono text-text overflow-x-auto whitespace-pre-wrap max-h-[300px] overflow-y-auto">{preview.episodic_context || '(no matches)'}</pre>
              </div>
            )}
            {!preview.semantic_context && !preview.episodic_context && (
              <p className="text-muted text-sm italic">{i18nT('pages.overview.vectorMemoryCard.no_context_to_inject_add_some_memories_first')}</p>
            )}
          </div>
        )}
        {!preview && !previewRead.isFetching && !previewRead.isError && <p className="text-muted text-sm italic">{i18nT('pages.overview.vectorMemoryCard.click_preview_to_see_what_gets_injected_into_pro')}</p>}
      </Card>
    )}
  </>)
}
