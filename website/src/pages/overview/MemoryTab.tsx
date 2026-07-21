import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from 'react'
import { XCircle, AlertTriangle, CheckCircle, ClipboardList, RefreshCw, Hourglass, Check, Network } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, SendBtn, Input, Badge } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { esc } from '../../api/helpers'
import VectorMemoryCard from './VectorMemoryCard'
import MemoryGraphTab from './MemoryGraphTab'
import type { Lesson, SessionInfo } from '../../types'
import { useSortableTable } from '../../hooks/useSortableTable'
import SortableHeader from '../../components/SortableHeader'

export default function MemoryTab({ refreshTrigger }: { refreshTrigger: number }) {
  const [view, setView] = useState<'table' | 'graph'>('table')
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
    if (keys.length === 0) { setConsolidateMsg(<><XCircle className="lucide-inline" /> No sessions to consolidate — start a chat first</>); setConsolidating(false); return }
    const results = await Promise.allSettled(keys.map((k: string) => api.consolidateMemory(k, true)))
    const succeeded = results.filter(r => r.status === 'fulfilled').length
    const failed = results.filter(r => r.status === 'rejected').length
    if (failed > 0) setConsolidateMsg(<><AlertTriangle className="lucide-inline" /> Consolidated {succeeded}/{keys.length} sessions ({failed} failed)</>)
    else { setConsolidateMsg(<><CheckCircle className="lucide-inline" /> Consolidated {succeeded} session{succeeded === 1 ? '' : 's'}</>); setConsolidateOk(true) }
    setConsolidating(false)
    scheduleClear(() => setConsolidateMsg(''), 4000)
  }
  return (<>
    <div className="inline-flex items-center gap-1 p-1 rounded-md bg-bg-elevated mb-4 w-fit">
      <button onClick={() => setView('table')} className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[13px] font-medium cursor-pointer border-none transition-colors ${view === 'table' ? 'bg-bg-hover text-accent' : 'bg-transparent text-muted hover:text-text'}`}><ClipboardList className="lucide-inline" /> Table</button>
      <button onClick={() => setView('graph')} className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[13px] font-medium cursor-pointer border-none transition-colors ${view === 'graph' ? 'bg-bg-hover text-accent' : 'bg-transparent text-muted hover:text-text'}`}><Network className="lucide-inline" /> Graph</button>
    </div>
    {view === 'graph' ? <MemoryGraphTab /> : <>
    <Card><CardTitle>Memory Settings <InfoTip text="Controls how conversation history is consolidated into memory." /></CardTitle>
      <div className="flex gap-3 items-end flex-wrap">
        <label htmlFor="memory-idle-hours" className="flex flex-col gap-1 text-[13px] text-muted">
          <span>Consolidation idle (hours)</span>
          <input id="memory-idle-hours" aria-label="Consolidation idle (hours)" type="number" min={0.5} max={24} step={0.5} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none transition-colors focus-ring" value={idleHours} onChange={e => setIdleHours(Number(e.target.value))} />
        </label>
        {!migrated && (
          <label htmlFor="memory-max-days" className="flex flex-col gap-1 text-[13px] text-muted">
            <span>History retention (days)</span>
            <input id="memory-max-days" aria-label="History retention (days)" type="number" min={7} max={365} step={1} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none transition-colors focus-ring" value={maxDays} onChange={e => setMaxDays(Number(e.target.value))} />
          </label>
        )}
        <Btn onClick={async () => { await api.saveMemorySettings({ history_idle_hours: idleHours, history_max_days: maxDays }); setSettingsSaved(true); scheduleClear(() => setSettingsSaved(false), 2000) }}>{settingsSaved ? <><Check className="lucide-inline" /> Saved</> : 'Save'}</Btn>
        <Btn onClick={consolidate} disabled={consolidating}>{consolidating ? <><Hourglass className="lucide-inline" /> Running…</> : <><RefreshCw className="lucide-inline" /> Test Consolidation</>}</Btn>
        {consolidateMsg && <span className={`text-[13px] ${consolidateOk ? 'text-ok' : 'text-danger'}`}>{consolidateMsg}</span>}

        {migrated && <span className="text-[12px] text-muted ml-2">Vector-only mode — markdown writes disabled</span>}
      </div>
    </Card>
    <VectorMemoryCard onActiveChange={setVectorActive} onMigratedChange={setMigrated} />
    {!vectorActive && (<>
      <Card><CardTitle>Preferences <InfoTip text="Learned user preferences (coding style, tools, workflows). Auto-updated by memory consolidation." /> <Btn onClick={async () => { await api.saveMemoryPreferences(pref); setPrefSaved(true); scheduleClear(() => setPrefSaved(false), 2000) }}>{prefSaved ? <><Check className="lucide-inline" /> Saved</> : 'Save'}</Btn></CardTitle>
        <textarea aria-label="Preferences" className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text text-sm font-body outline-none resize-y leading-relaxed transition-colors focus-ring" rows={8} value={pref} onChange={e => setPref(e.target.value)} placeholder="Loading…" /></Card>
      <Card><CardTitle>Projects <Btn onClick={async () => { await api.saveMemoryProjects(proj); setProjSaved(true); scheduleClear(() => setProjSaved(false), 2000) }}>{projSaved ? <><Check className="lucide-inline" /> Saved</> : 'Save'}</Btn></CardTitle>
        <textarea aria-label="Projects" className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text text-sm font-body outline-none resize-y leading-relaxed transition-colors focus-ring" rows={8} value={proj} onChange={e => setProj(e.target.value)} placeholder="Loading…" /></Card>
      <Card><CardTitle>Daily History <Btn onClick={async () => { await api.saveMemoryHistory(hist); setHistSaved(true); scheduleClear(() => setHistSaved(false), 2000) }}>{histSaved ? <><Check className="lucide-inline" /> Saved</> : 'Save'}</Btn></CardTitle>
        <textarea aria-label="Daily History" className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text text-sm font-mono outline-none resize-y leading-relaxed transition-colors focus-ring" rows={10} value={hist} onChange={e => setHist(e.target.value)} placeholder="No history yet" /></Card>
    </>)}
    {!vectorActive && (
      <Card><CardTitle>Lessons <InfoTip text="Persistent lessons injected into every session. Auto-extracted from task runner failures. Add manually via 'kirocrew learn add'. When vector memory is active, lessons are managed in the Semantic tab as lesson.* entries." /></CardTitle>
      <div className="flex gap-2 items-center flex-wrap mb-3">
        <Input placeholder="Rule (e.g. always use conduit for ADA)" style={{ flex: 2 }} value={rule} onChange={e => setRule(e.target.value)} />
        <select className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer appearance-none transition-colors focus-ring" style={{ flex: '0 0 140px' }} value={cat} onChange={e => setCat(e.target.value)}>
          <option value="knowledge">knowledge</option><option value="tool">tool</option><option value="preference">preference</option>
        </select>
        <SendBtn onClick={async () => { if (!rule) return; await api.createLesson(rule, cat); setRule(''); loadLessons() }}>Add</SendBtn>
      </div>
      <table className="w-full border-collapse table-striped"><thead><tr><SortableHeader label="Rule" sortKey="rule" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label="Category" sortKey="category" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label="When" sortKey="ts" sort={lessonSort} onToggle={toggleLessonSort} /><th aria-label="Actions" className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium"></th></tr></thead>
        <tbody>{lessons.length === 0 ? <tr><td colSpan={4} className="text-muted italic px-2.5 py-3.5 text-sm">No lessons</td></tr> : sortedLessons.map((l) => (
          <tr key={`${l.rule}-${l.ts}`} className="hover:bg-bg-hover transition-colors"><td className="px-2.5 py-2 border-b border-border text-sm">{esc(l.rule)}</td><td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="ok">{l.category}</Badge></td><td className="px-2.5 py-2 border-b border-border text-sm">{new Date(l.ts).toLocaleString()}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm"><Btn danger onClick={async () => { await api.deleteLesson(l.rule); loadLessons() }}>Delete</Btn></td></tr>
        ))}</tbody></table></Card>
    )}
  </>}</>)
}
