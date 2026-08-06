import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from 'react'
import { XCircle, AlertTriangle, CheckCircle, RefreshCw, Hourglass, Check, BookOpen } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, SendBtn, Input, Badge, EmptyState } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import SimpleSelect from '../../components/SimpleSelect'
import { esc } from '../../api/helpers'
import VectorMemoryCard from './VectorMemoryCard'
import EmbeddingModelCard from './EmbeddingModelCard'
import type { Lesson, SessionInfo } from '../../types'
import { useSortableTable } from '../../hooks/useSortableTable'
import SortableHeader from '../../components/SortableHeader'

import { i18nT } from '../../i18n/t'
import { fmtDateTimeNumeric } from '../../i18n/format'
export default function MemoryTab({ refreshTrigger }: { refreshTrigger: number }) {
  const [pref, setPref] = useState(''); const [proj, setProj] = useState(''); const [hist, setHist] = useState('')
  const [prefSaved, setPrefSaved] = useState(false); const [projSaved, setProjSaved] = useState(false); const [histSaved, setHistSaved] = useState(false)
  const [lessons, setLessons] = useState<Lesson[]>([]); const [rule, setRule] = useState(''); const [cat, setCat] = useState('knowledge')
  const [idleHours, setIdleHours] = useState(3); const [maxDays, setMaxDays] = useState(90); const [settingsSaved, setSettingsSaved] = useState(false)
  const [migrated, setMigrated] = useState(false)
  const [vectorActive, setVectorActive] = useState(false)
  const [consolidating, setConsolidating] = useState(false)
  const [consolidateMsg, setConsolidateMsg] = useState<ReactNode>('')
  const [consolidateOk, setConsolidateOk] = useState(false)
  // Track all "Saved" / "consolidate-msg-clear" timeout ids so they can be
  // cleared on unmount — otherwise a pending setTimeout fires after the
  // component is gone and (in vitest) shows up as an unhandled error from
  // "tasks running past test environment teardown".
  const timeoutsRef = useRef<ReturnType<typeof setTimeout>[]>([])
  useEffect(() => () => {
    timeoutsRef.current.forEach(clearTimeout)
    timeoutsRef.current = []
  }, [])
  const scheduleClear = useCallback((fn: () => void, ms: number) => {
    const id = setTimeout(() => {
      timeoutsRef.current = timeoutsRef.current.filter(t => t !== id)
      fn()
    }, ms)
    timeoutsRef.current.push(id)
  }, [])
  const loadLessons = useCallback(async () => { const d = await api.lessons(); setLessons(d.lessons || []) }, [])
  const loadMemory = useCallback(() => {
    api.memoryPreferences().then(d => setPref(d.content || ''))
    api.memoryProjects().then(d => setProj(d.content || ''))
    api.memoryHistory().then(d => setHist(d.content || ''))
  }, [])
  const lessonComparators = useMemo(() => ({
    rule: (a: Lesson, b: Lesson) => a.rule.localeCompare(b.rule),
    category: (a: Lesson, b: Lesson) => a.category.localeCompare(b.category),
    ts: (a: Lesson, b: Lesson) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
  }), [])
  const recentLessons = useMemo(() => lessons.slice(-20), [lessons])
  const { sorted: sortedLessons, sort: lessonSort, toggle: toggleLessonSort } = useSortableTable(recentLessons, 'memory-lessons', lessonComparators, { key: 'ts', dir: 'desc' })
  useEffect(() => {
    loadMemory()
    api.memorySettings().then(d => { setIdleHours(d.history_idle_hours ?? 3); setMaxDays(d.history_max_days ?? 90); setMigrated(d.migrated ?? false) })
    loadLessons()
  }, [loadLessons, loadMemory])
  useEffect(() => { loadLessons(); loadMemory() }, [refreshTrigger, loadLessons, loadMemory])
  const consolidate = async () => {
    setConsolidating(true); setConsolidateMsg(''); setConsolidateOk(false)
    const sessions = await api.sessions(200).catch(() => ({ sessions: [] }))
    const keys = sessions?.sessions?.map((s: SessionInfo) => s.key).filter(Boolean) || []
    if (keys.length === 0) { setConsolidateMsg(<><XCircle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.no_sessions_to_consolidate_start_a_chat_first')}</>); setConsolidating(false); return }
    const results = await Promise.allSettled(keys.map((k: string) => api.consolidateMemory(k, true)))
    const succeeded = results.filter(r => r.status === 'fulfilled').length
    const failed = results.filter(r => r.status === 'rejected').length
    if (failed > 0) setConsolidateMsg(<><AlertTriangle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.consolidated_sessions_failed', { succeeded, total: keys.length, failed })}</>)
    else { setConsolidateMsg(<><CheckCircle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.consolidated')} {i18nT('pages.overview.memoryTab.session', { count: succeeded })}</>); setConsolidateOk(true) }
    setConsolidating(false)
    scheduleClear(() => setConsolidateMsg(''), 4000)
  }
  return (<>
    {/* Graph/vector internals live on the Developer page (Memory tab); this
        surface is the user-facing browser: settings, preferences, projects,
        daily history, and lessons. */}
    <Card><CardTitle>{i18nT('pages.overview.memoryTab.memory_settings')} <InfoTip text={i18nT('pages.overview.memoryTab.controls_how_conversation_history_is_consolidate')} /></CardTitle>
      <div className="flex gap-3 items-end flex-wrap">
        <label htmlFor="memory-idle-hours" className="flex flex-col gap-1 text-[13px] text-muted">
          <span>{i18nT('pages.overview.memoryTab.consolidation_idle_hours')}</span>
          <input id="memory-idle-hours" aria-label={i18nT('pages.overview.memoryTab.consolidation_idle_hours')} type="number" min={0.5} max={24} step={0.5} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none transition-colors focus-ring" value={idleHours} onChange={e => setIdleHours(Number(e.target.value))} />
        </label>
        {!migrated && (
          <label htmlFor="memory-max-days" className="flex flex-col gap-1 text-[13px] text-muted">
            <span>{i18nT('pages.overview.memoryTab.history_retention_days')}</span>
            <input id="memory-max-days" aria-label={i18nT('pages.overview.memoryTab.history_retention_days')} type="number" min={7} max={365} step={1} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none transition-colors focus-ring" value={maxDays} onChange={e => setMaxDays(Number(e.target.value))} />
          </label>
        )}
        <Btn onClick={async () => { await api.saveMemorySettings({ history_idle_hours: idleHours, history_max_days: maxDays }); setSettingsSaved(true); scheduleClear(() => setSettingsSaved(false), 2000) }}>{settingsSaved ? <><Check className="lucide-inline" /> {i18nT('pages.overview.memoryTab.saved')}</> : i18nT('pages.overview.memoryTab.save')}</Btn>
        <Btn onClick={consolidate} disabled={consolidating}>{consolidating ? <><Hourglass className="lucide-inline" /> {i18nT('pages.overview.memoryTab.running')}</> : <><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.memoryTab.summarize_now')}</>}</Btn>
        {consolidateMsg && <span className={`text-[13px] ${consolidateOk ? 'text-ok' : 'text-danger'}`}>{consolidateMsg}</span>}

        {migrated && <span className="text-[12px] text-muted ml-2">{i18nT('pages.overview.memoryTab.semantic_memory_active_text_files_are_read_only')}</span>}
      </div>
    </Card>
    <VectorMemoryCard onActiveChange={setVectorActive} onMigratedChange={setMigrated} />
    <EmbeddingModelCard />
    {!vectorActive && (<>
      <Card><CardTitle>{i18nT('pages.overview.memoryTab.preferences')} <InfoTip text={i18nT('pages.overview.memoryTab.learned_user_preferences_coding_style_tools_work')} /> <Btn onClick={async () => { await api.saveMemoryPreferences(pref); setPrefSaved(true); scheduleClear(() => setPrefSaved(false), 2000) }}>{prefSaved ? <><Check className="lucide-inline" /> {i18nT('pages.overview.memoryTab.saved')}</> : i18nT('pages.overview.memoryTab.save')}</Btn></CardTitle>
        <textarea aria-label={i18nT('pages.overview.memoryTab.preferences')} className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text text-sm font-body outline-none resize-y leading-relaxed transition-colors focus-ring" rows={8} value={pref} onChange={e => setPref(e.target.value)} placeholder={i18nT('pages.overview.memoryTab.loading')} /></Card>
      <Card><CardTitle>{i18nT('pages.overview.memoryTab.projects')} <Btn onClick={async () => { await api.saveMemoryProjects(proj); setProjSaved(true); scheduleClear(() => setProjSaved(false), 2000) }}>{projSaved ? <><Check className="lucide-inline" /> {i18nT('pages.overview.memoryTab.saved')}</> : i18nT('pages.overview.memoryTab.save')}</Btn></CardTitle>
        <textarea aria-label={i18nT('pages.overview.memoryTab.projects')} className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text text-sm font-body outline-none resize-y leading-relaxed transition-colors focus-ring" rows={8} value={proj} onChange={e => setProj(e.target.value)} placeholder={i18nT('pages.overview.memoryTab.loading')} /></Card>
      <Card><CardTitle>{i18nT('pages.overview.memoryTab.daily_history')} <Btn onClick={async () => { await api.saveMemoryHistory(hist); setHistSaved(true); scheduleClear(() => setHistSaved(false), 2000) }}>{histSaved ? <><Check className="lucide-inline" /> {i18nT('pages.overview.memoryTab.saved')}</> : i18nT('pages.overview.memoryTab.save')}</Btn></CardTitle>
        <textarea aria-label={i18nT('pages.overview.memoryTab.daily_history')} className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text text-sm font-mono outline-none resize-y leading-relaxed transition-colors focus-ring" rows={10} value={hist} onChange={e => setHist(e.target.value)} placeholder={i18nT('pages.overview.memoryTab.no_history_yet')} /></Card>
    </>)}
    {!vectorActive && (
      <Card><CardTitle>{i18nT('pages.overview.memoryTab.lessons')} <InfoTip text={i18nT('pages.overview.memoryTab.persistent_lessons_injected_into_every_session_a')} /></CardTitle>
      <div className="flex gap-2 items-center flex-wrap mb-3">
        <Input placeholder={i18nT('pages.overview.memoryTab.rule_e_g_always_use_tabs_not_spaces')} style={{ flex: 2 }} value={rule} onChange={e => setRule(e.target.value)} />
        <SimpleSelect
          aria-label={i18nT('pages.overview.memoryTab.category')}
          style={{ flex: '0 0 140px' }}
          options={['knowledge', 'tool', 'preference']}
          optionLabels={[i18nT('pages.overview.memoryTab.knowledge'), i18nT('pages.overview.memoryTab.tool'), i18nT('pages.overview.memoryTab.preference')]}
          value={cat}
          onChange={setCat}
        />
        <SendBtn onClick={async () => { if (!rule) return; await api.createLesson(rule, cat); setRule(''); loadLessons() }}>{i18nT('pages.overview.memoryTab.add')}</SendBtn>
      </div>
      <table className="w-full border-collapse table-striped"><thead><tr><SortableHeader label={i18nT('pages.overview.memoryTab.rule')} sortKey="rule" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label={i18nT('pages.overview.memoryTab.category')} sortKey="category" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label={i18nT('pages.overview.memoryTab.when')} sortKey="ts" sort={lessonSort} onToggle={toggleLessonSort} /><th aria-label={i18nT('pages.overview.memoryTab.actions')} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium"></th></tr></thead>
        <tbody>{lessons.length === 0 ? <tr><td colSpan={4}><EmptyState icon={<BookOpen className="lucide-inline" />} title={i18nT('pages.overview.memoryTab.no_lessons_yet')} subtitle={i18nT('pages.overview.memoryTab.lessons_empty_subtitle')} /></td></tr> : sortedLessons.map((l) => (
          <tr key={`${l.rule}-${l.ts}`} className="hover:bg-bg-hover transition-colors"><td className="px-2.5 py-2 border-b border-border text-sm">{esc(l.rule)}</td><td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="ok">{l.category}</Badge></td><td className="px-2.5 py-2 border-b border-border text-sm">{fmtDateTimeNumeric(l.ts)}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm"><Btn danger onClick={async () => { await api.deleteLesson(l.rule); loadLessons() }}>{i18nT('pages.overview.memoryTab.delete')}</Btn></td></tr>
        ))}</tbody></table></Card>
    )}
  </>)
}
