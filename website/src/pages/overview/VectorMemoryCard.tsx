import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { Brain, Hourglass, CheckCircle, XCircle, RefreshCw, Search, AlertTriangle, Check, X } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, SendBtn, Input, Badge } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { esc } from '../../api/helpers'

import { i18nT } from '../../i18n/t'
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

export default function VectorMemoryCard({ onActiveChange, onMigratedChange }: { onActiveChange?: (active: boolean) => void; onMigratedChange?: (migrated: boolean) => void }) {
  const [stats, setStats] = useState<VectorStats | null>(null)
  const [embStatus, setEmbStatus] = useState<EmbeddingStatus | null>(null)
  const [semantic, setSemantic] = useState<SemanticEntry[]>([])
  const [episodic, setEpisodic] = useState<EpisodicEntry[]>([])
  const [epHasMore, setEpHasMore] = useState(false)
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [epQuery, setEpQuery] = useState('')
  const [epTagFilter, setEpTagFilter] = useState<string|null>(null)
  const [newKey, setNewKey] = useState(''); const [newVal, setNewVal] = useState('')
  const [enabling, setEnabling] = useState(false)
  const [view, setView] = useState<'semantic'|'episodic'|'audit'|'inspector'>('semantic')
  const [editKey, setEditKey] = useState<string|null>(null); const [editVal, setEditVal] = useState('')
  const [eventFilter, setEventFilter] = useState<string>('all')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [evHasMore, setEvHasMore] = useState(false)
  const [inspectorQuery, setInspectorQuery] = useState(''); const [preview, setPreview] = useState<ContextPreview | null>(null)
  const [writeError, setWriteError] = useState('')
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

  const load = useCallback(async () => {
    const [st, emb, sem] = await Promise.all([
      api.vectorStats().catch(() => null),
      api.vectorEmbeddingStatus().catch(() => null),
      api.vectorSemantic().catch(() => ({ entries: [] })),
    ])
    setStats(st); setEmbStatus(emb); setSemantic(sem?.entries || [])
    if (st?.migrated != null) onMigratedChange?.(st.migrated)
    // onMigratedChange is the parent's stable useState setter (setMigrated), so
    // including it can't cause a refetch loop; it just satisfies exhaustive-deps.
  }, [onMigratedChange])

  useEffect(() => { load() }, [load])

  const pollEmbeddingStatus = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      const s = await api.vectorEmbeddingStatus().catch(() => null)
      if (!s) return
      setEmbStatus(s)
      if (s.setup_step === 'done' || s.setup_step === 'error' || (s.setup_step === 'idle' && s.setup_error)) {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
        // setup_error surfaces directly from embStatus in the error state.
        load().then(() => setEnabling(false))
      }
    }, 2000)
  }, [load])

  useEffect(() => {
    const step = embStatus?.setup_step
    if (step && step !== 'idle' && step !== 'done' && step !== 'error' && !enabling) {
      setEnabling(true)
      pollEmbeddingStatus()
      return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null } }
    }
  }, [embStatus?.setup_step]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const loadEpisodic = useCallback(async (q?: string, append = false, tag?: string | null) => {
    const query = q ?? epQuery
    const activeTag = tag !== undefined ? tag : epTagFilter
    const offset = append ? episodic.length : 0
    const d = query
      ? await api.vectorEpisodicSearch(query, activeTag || undefined).catch(() => ({ results: [] }))
      : await api.vectorEpisodic(50, offset, activeTag || undefined).catch(() => ({ entries: [] }))
    const items = d?.results || d?.entries || []
    setEpisodic(prev => append ? [...prev, ...items] : items)
    setEpHasMore(!query && items.length >= 50)
  }, [epQuery, epTagFilter, episodic.length])
  const loadEvents = async (append = false) => {
    const offset = append ? events.length : 0
    const d = await api.vectorEvents(50, offset).catch(() => ({ events: [] }))
    const items = d?.events || []
    setEvents(prev => append ? [...prev, ...items] : items)
    setEvHasMore(items.length >= 50)
  }
  const loadPreview = async (q?: string) => {
    const d = await api.vectorContextPreview(q).catch(() => null)
    setPreview(d)
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

  if (stats === null) return <Card><CardTitle><Brain className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.vector_memory')}</CardTitle><p className="text-muted text-sm">{i18nT('pages.overview.vectorMemoryCard.loading')}</p></Card>

  const startEmbeddings = async () => {
    setEnabling(true)
    api.vectorEnableEmbeddings().catch(() => {})
    pollEmbeddingStatus()
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
      {!active && !enabling && (
        <div className="flex flex-col gap-3 items-start">
          {embStatus?.setup_error
            ? (
              <div className="flex items-center gap-2">
                <p className="text-sm text-danger"><XCircle className="lucide-inline" /> {embStatus.setup_error}</p>
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
                  {step === 'error' && <><XCircle className="lucide-inline" /> {embStatus?.setup_error || i18nT('pages.overview.vectorMemoryCard.setup_failed')}</>}
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
              { label: 'Semantic', value: stats?.semantic_active ?? 0 },
              { label: 'Episodic', value: stats?.episodic_active ?? 0 },
              { label: 'Embedded', value: stats?.embedded_count ?? stats?.faiss_index_size ?? 0 },
            ].map(s => (
              <div key={s.label} className="stat-accent relative overflow-hidden bg-bg-elevated rounded-md px-3 py-2 border border-border">
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
            {(['semantic','episodic','audit','inspector'] as const).map(v => (
              <button key={v} onClick={() => { setView(v); if (v === 'episodic') loadEpisodic(); if (v === 'audit') loadEvents(); if (v === 'inspector') loadPreview() }}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[13px] font-medium cursor-pointer border-none transition-colors ${view === v ? 'bg-bg-hover text-accent' : 'bg-transparent text-muted hover:text-text'}`}>{
                  v === 'inspector' ? <><Search className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.inspector')}</> : v[0].toUpperCase() + v.slice(1)
                }</button>
            ))}
            </div>
          </div>
        </div>
      )}
    </Card>

    {active && view === 'semantic' && (
      <Card>
        <CardTitle>{i18nT('pages.overview.vectorMemoryCard.semantic_memory')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.structured_key_value_facts_about_you_confidence')} /></CardTitle>
        <div className="flex gap-2 items-center mb-3 relative">
          <div className="relative" style={{ flex: 1 }}>
            <Input placeholder={i18nT('pages.overview.vectorMemoryCard.key_e_g_pref_backend_framework')} value={newKey} onChange={e => { setNewKey(e.target.value); setWriteError('') }}
              list="key-suggestions" className="w-full" />
            <datalist id="key-suggestions">{filteredKeys.map(k => <option key={k} value={k}>{k}</option>)}</datalist>
          </div>
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.value')} style={{ flex: 2 }} value={newVal} onChange={e => { setNewVal(e.target.value); setWriteError('') }}
            onKeyDown={async e => { if (e.key === 'Enter' && newKey && newVal) { try { await api.vectorSemanticWrite(newKey, newVal); setNewKey(''); setNewVal(''); setWriteError(''); load() } catch (err: unknown) { setWriteError(extractError(err)) } } }} />
          <SendBtn onClick={async () => { if (!newKey || !newVal) return; try { await api.vectorSemanticWrite(newKey, newVal); setNewKey(''); setNewVal(''); setWriteError(''); load() } catch (e: unknown) { setWriteError(extractError(e)) } }}>{i18nT('pages.overview.vectorMemoryCard.set')}</SendBtn>
        </div>
        {writeError && <p className="text-danger text-[13px] mb-2"><AlertTriangle className="lucide-inline" /> {writeError}</p>}
        <div className="flex gap-2 items-center mb-3">
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.filter_by_key_or_value')} style={{ flex: 1 }} value={semFilter} onChange={e => { setSemFilter(e.target.value); setEditKey(null) }} />
          {semFilter && <Btn onClick={() => { setSemFilter(''); setEditKey(null) }}>{i18nT('pages.overview.vectorMemoryCard.clear')}</Btn>}
        </div>
        <div className="max-h-[500px] overflow-y-auto">
        <table className="w-full border-collapse table-striped"><thead><tr>
          {['Key','Value','Confidence','Source',''].map(h => <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium sticky top-0 bg-card z-10">{h}</th>)}
        </tr></thead><tbody>
          {filteredSemantic.length === 0 ? <tr><td colSpan={5} className="text-muted italic px-2.5 py-3.5 text-sm">{semFilter ? i18nT('pages.overview.vectorMemoryCard.no_matching_entries') : i18nT('pages.overview.vectorMemoryCard.no_semantic_entries')}</td></tr> : visibleSemantic.map(e => {
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
                        onKeyDown={async ev => { if (ev.key === 'Enter') { try { await api.vectorSemanticWrite(e.key, editVal); setEditKey(null); setWriteError(''); load() } catch (err: unknown) { setWriteError(extractError(err)) } } if (ev.key === 'Escape') setEditKey(null) }}
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
        <div className="flex gap-2 items-center mb-3">
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.search_episodic_memories')} style={{ flex: 1 }} value={epQuery} onChange={e => setEpQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') loadEpisodic() }} />
          <SendBtn onClick={() => loadEpisodic()}>{i18nT('pages.overview.vectorMemoryCard.search')}</SendBtn>
          {epQuery && <Btn onClick={() => { setEpQuery(''); setEpTagFilter(null); loadEpisodic('', false, null) }}>{i18nT('pages.overview.vectorMemoryCard.clear')}</Btn>}
          {!epQuery && epTagFilter && <Btn onClick={() => { setEpTagFilter(null); loadEpisodic('', false, null) }}>{i18nT('pages.overview.vectorMemoryCard.clear')}</Btn>}
        </div>
        {episodic.length > 0 && (() => {
          const allTags = [...new Set(episodic.flatMap(e => parseTags(e.tags)))]
          return allTags.length > 0 ? (
            <div className="flex gap-1.5 flex-wrap mb-3">
              <span className="text-muted text-[12px] self-center mr-1">{i18nT('pages.overview.vectorMemoryCard.filter_by_tag')}</span>
              {allTags.map((tag: string) => (
                <button key={tag} onClick={() => { const t = epTagFilter === tag ? null : tag; setEpTagFilter(t); setEpQuery(''); loadEpisodic('', false, t) }}
                  className={`px-2 py-0.5 rounded-full text-[12px] border transition-colors cursor-pointer ${epTagFilter === tag ? 'bg-warn/30 text-warn border-warn/40' : 'bg-ok-subtle text-ok border-ok/20 hover:bg-ok/20'}`}>{tag}</button>
              ))}
            </div>
          ) : null
        })()}
        <div className="max-h-[500px] overflow-y-auto">
        <table className="w-full border-collapse table-striped"><thead><tr>
          {['Text','Tags','Imp.',...(epQuery ? ['Score'] : []),'When',''].map(h => <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium sticky top-0 bg-card z-10">{h}</th>)}
        </tr></thead><tbody>
          {episodic.length === 0 ? <tr><td colSpan={epQuery ? 6 : 5} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.overview.vectorMemoryCard.no_episodic_entries')}</td></tr> : episodic.map(e => {
            const tags = parseTags(e.tags);
            return (
              <tr key={e.id} className="hover:bg-bg-hover transition-colors">
                <td className="px-2.5 py-2 border-b border-border text-sm max-w-[450px]"><span className="break-words whitespace-pre-wrap">{esc(e.text)}</span></td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><div className="flex gap-1 flex-wrap">{tags.map((t: string) => <Badge key={t} variant="ok">{t}</Badge>)}</div></td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{confidenceBadge(e.importance)}</td>
                {epQuery && <td className="px-2.5 py-2 border-b border-border text-sm font-mono text-[12px]">{e.score != null ? e.score.toFixed(3) : '—'}</td>}
                <td className="px-2.5 py-2 border-b border-border text-sm text-muted whitespace-nowrap">{(() => { const m = e.text?.match(/^\[(\d{4}-\d{2}-\d{2})/); if (m) return m[1]; const raw = e.created_at || e.ts || ''; const d = new Date(raw.replace(' ', 'T') + (raw.includes('+') || raw.includes('Z') ? '' : 'Z')); return isNaN(d.getTime()) ? '—' : fmtDateNumeric(d) })()}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><Btn danger onClick={async () => { await api.vectorEpisodicDelete(e.id); setEpisodic(prev => prev.filter(x => x.id !== e.id)) }}>{i18nT('pages.overview.vectorMemoryCard.delete')}</Btn></td>
              </tr>
            )
          })}
        </tbody></table>
        </div>
        {epHasMore && <div className="flex justify-center mt-3"><Btn onClick={() => loadEpisodic(undefined, true)}>{i18nT('pages.overview.vectorMemoryCard.load_more')}</Btn></div>}
        {episodic.length > 0 && <p className="text-muted text-[12px] mt-2">{i18nT('pages.overview.vectorMemoryCard.showing')} {episodic.length} {i18nT('pages.overview.vectorMemoryCard.entries')}</p>}
      </Card>
    )}

    {active && view === 'audit' && (
      <Card>
        <CardTitle>{i18nT('pages.overview.vectorMemoryCard.audit_trail')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.every_memory_create_update_delete_conflict_and_i')} /></CardTitle>
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
          {filteredEvents.length === 0 ? <tr><td colSpan={4} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.overview.vectorMemoryCard.no_events')}</td></tr> : filteredEvents.map((e, i: number) => (
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
        {evHasMore && <div className="flex justify-center mt-3"><Btn onClick={() => loadEvents(true)}>{i18nT('pages.overview.vectorMemoryCard.load_more')}</Btn></div>}
        {events.length > 0 && <p className="text-muted text-[12px] mt-2">{i18nT('pages.overview.vectorMemoryCard.showing')} {filteredEvents.length} {i18nT('pages.overview.vectorMemoryCard.events')}{eventFilter !== 'all' ? ` (${events.length} total)` : ''}</p>}
      </Card>
    )}

    {active && view === 'inspector' && (
      <Card>
        <CardTitle><Search className="lucide-inline" /> {i18nT('pages.overview.vectorMemoryCard.memory_inspector')} <InfoTip text={i18nT('pages.overview.vectorMemoryCard.preview_what_gets_injected_into_prompts_enter_a')} /></CardTitle>
        <div className="flex gap-2 items-center mb-3">
          <Input placeholder={i18nT('pages.overview.vectorMemoryCard.test_query_e_g_what_database_should_i_use')} style={{ flex: 1 }} value={inspectorQuery} onChange={e => setInspectorQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') loadPreview(inspectorQuery) }} />
          <SendBtn onClick={() => loadPreview(inspectorQuery)}>{i18nT('pages.overview.vectorMemoryCard.preview')}</SendBtn>
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
        {!preview && <p className="text-muted text-sm italic">{i18nT('pages.overview.vectorMemoryCard.click_preview_to_see_what_gets_injected_into_pro')}</p>}
      </Card>
    )}
  </>)
}
