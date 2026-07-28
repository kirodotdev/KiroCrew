import { useState, useEffect, useRef, useCallback, useContext, useMemo, type ReactNode } from 'react'
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso'
import { api } from '../api/client'
import { WsContext } from '../App'
import { PageHeader } from '../components/ui'
import { LAYOUT } from '../components/layout'

import { i18nT } from '../i18n/t'
const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR'] as const
const levelColor = (lvl: string) => lvl === 'ERROR' ? 'text-danger' : lvl === 'WARNING' ? 'text-warn' : lvl === 'DEBUG' ? 'text-muted' : 'text-text'
const levelBg = (lvl: string, active: boolean) => {
  if (!active) return 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'
  if (lvl === 'DEBUG') return 'bg-muted text-muted-fg border-muted'
  if (lvl === 'INFO') return 'bg-info text-info-fg border-info'
  if (lvl === 'WARNING') return 'bg-warn text-warn-fg border-warn'
  return 'bg-danger text-danger-fg border-danger'
}

/** Reusable log viewer — used in LogsPage and ActivityViewer */
export function LogViewer({ compact }: { compact?: boolean }) {
  const [lines, setLines] = useState<{ level: string; msg: string }[]>([])
  const [currentLevel, setCurrentLevel] = useState('INFO')
  const [search, setSearch] = useState('')
  const [matchesOnly, setMatchesOnly] = useState(false)
  const [autoFollow, setAutoFollow] = useState(true)
  const [wrapLines, setWrapLines] = useState(true)
  const [newestFirst, setNewestFirst] = useState(false)
  const [atTop, setAtTop] = useState(true)
  const virtuosoRef = useRef<VirtuosoHandle>(null)
  const { subscribeLogs } = useContext(WsContext)

  useEffect(() => { api.logLevel().then(d => setCurrentLevel(d.level)) }, [])

  const onLog = useCallback((data: { level: string; msg: string }) => {
    setLines(prev => { const next = [...prev, data]; return next.length > LAYOUT.LOG_LINE_CAP ? next.slice(-LAYOUT.LOG_LINE_CAP) : next })
  }, [])

  useEffect(() => {
    subscribeLogs(onLog)
    return () => subscribeLogs(null)
  }, [subscribeLogs, onLog])

  const changeLevel = async (level: string) => { const r = await api.setLogLevel(level); if (r.ok) setCurrentLevel(level) }

  const { filtered, matchCount } = useMemo(() => {
    const levelIdx = LEVELS.indexOf(currentLevel as typeof LEVELS[number])
    const levelFiltered = lines.filter(l => LEVELS.indexOf(l.level as typeof LEVELS[number]) >= levelIdx)
    const q = search.toLowerCase()
    // newestFirst reverses the display (latest at top); default is latest-last (tail).
    if (!q) {
      const base = levelFiltered.map(l => ({ ...l, match: false }))
      return { filtered: newestFirst ? [...base].reverse() : base, matchCount: 0 }
    }
    const result = levelFiltered.map(l => ({ ...l, match: l.msg.toLowerCase().includes(q) }))
    const matched = result.filter(l => l.match)
    const display = matchesOnly ? matched : result
    return { filtered: newestFirst ? [...display].reverse() : display, matchCount: matched.length }
  }, [lines, search, matchesOnly, currentLevel, newestFirst])

  // Re-align to the latest end when filters / tail / direction change.
  useEffect(() => {
    if (autoFollow && filtered.length > 0) {
      virtuosoRef.current?.scrollToIndex({ index: newestFirst ? 0 : filtered.length - 1, behavior: 'smooth' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, matchesOnly, autoFollow, newestFirst])

  // Newest-first live-follow: followOutput only tracks the bottom, so pin to the
  // top (index 0) as new lines prepend. Gated on atTop so streaming logs don't
  // yank the user back up while they're scrolled down reading older entries —
  // mirrors followOutput's auto-pause in latest-last mode.
  useEffect(() => {
    if (autoFollow && newestFirst && atTop && filtered.length > 0) {
      virtuosoRef.current?.scrollToIndex({ index: 0 })
    }
  }, [filtered.length, autoFollow, newestFirst, atTop])

  const toggleTail = useCallback(() => {
    setAutoFollow(p => !p)
    if (!autoFollow && filtered.length > 0) {
      virtuosoRef.current?.scrollToIndex({ index: newestFirst ? 0 : filtered.length - 1, behavior: 'smooth' })
    }
  }, [autoFollow, filtered.length, newestFirst])

  const highlight = (msg: string) => {
    const q = search.toLowerCase()
    if (!q) return <>{msg}</>
    const parts: (string | ReactNode)[] = []
    let remaining = msg, lower = remaining.toLowerCase(), idx = lower.indexOf(q), key = 0
    while (idx !== -1) {
      parts.push(remaining.slice(0, idx))
      parts.push(<mark key={key++} className="bg-transparent text-accent font-bold">{remaining.slice(idx, idx + q.length)}</mark>)
      remaining = remaining.slice(idx + q.length)
      lower = remaining.toLowerCase()
      idx = lower.indexOf(q)
    }
    parts.push(remaining)
    return <>{parts}</>
  }

  const sz = compact ? { btn: 'px-2 py-0.5 text-[11px]', input: 'px-2 py-0.5 text-[11px]', row: 'text-[12px]', gap: 'gap-1 mb-2', label: 'text-[11px]' }
    : { btn: 'px-3.5 py-[5px] text-[13px]', input: 'px-3 py-1.5 text-[13px]', row: 'text-[13px]', gap: 'gap-1.5 mb-3', label: 'text-[13px]' }

  return (
    <div className={`flex-1 flex flex-col min-h-0 ${compact ? '' : 'px-6 pb-8'}`}>
      <div className={`flex ${sz.gap} flex-wrap items-center`}>
        <span className={`${sz.label} text-muted mr-1`}>{i18nT('pages.logsPage.log_level')}</span>
        {LEVELS.map(l => (
          <button key={l} className={`${sz.btn} rounded-full font-medium font-body cursor-pointer border transition-all ${levelBg(l, currentLevel === l)}`} onClick={() => changeLevel(l)}>{l.charAt(0) + l.slice(1).toLowerCase()}</button>
        ))}
      </div>
      <div className={`flex gap-2 ${compact ? 'mb-2' : 'mb-3'} items-center`}>
        <input type="text" aria-label={i18nT('pages.logsPage.filter_logs')} placeholder={i18nT('pages.logsPage.filter_logs_2')} value={search}
          onChange={e => { const v = e.target.value; setSearch(v); if (!v) setMatchesOnly(false) }}
          className={`flex-1 ${sz.input} rounded-lg border border-border bg-surface text-text font-mono placeholder:text-muted focus:outline-none focus:border-accent`}
        />
        {search && (
          <>
            <button className={`${sz.btn} rounded cursor-pointer border transition-all whitespace-nowrap ${matchesOnly ? 'bg-surface border-border-strong text-text' : 'bg-transparent border-border text-muted'}`}
              onClick={() => setMatchesOnly(p => !p)}>{i18nT('pages.logsPage.matches_only')}</button>
            <span className={`${sz.label} text-muted whitespace-nowrap`}>{matchCount} {i18nT('pages.logsPage.matches')}</span>
          </>
        )}
        <button className={`${sz.btn} rounded cursor-pointer border transition-all whitespace-nowrap ml-auto ${newestFirst ? 'bg-surface border-border-strong text-text' : 'bg-transparent border-border text-muted'}`}
          onClick={() => setNewestFirst(p => !p)}>{newestFirst ? 'Latest: first' : 'Latest: last'}</button>
        <button className={`${sz.btn} rounded cursor-pointer border transition-all whitespace-nowrap ${wrapLines ? 'bg-surface border-border-strong text-text' : 'bg-transparent border-border text-muted'}`}
          onClick={() => setWrapLines(p => !p)}>{wrapLines ? 'Wrap: on' : 'Wrap: off'}</button>
        <button className={`${sz.btn} rounded cursor-pointer border transition-all whitespace-nowrap ${autoFollow ? 'bg-surface border-border-strong text-text' : 'bg-transparent border-border text-muted'}`}
          onClick={toggleTail}>{autoFollow ? 'Tail: on' : 'Tail: off'}</button>
      </div>
      <div className={`flex-1 flex flex-col min-h-0 ${compact ? 'border border-border rounded-lg overflow-hidden mb-2' : 'card-glow border border-border bg-card rounded-lg p-5 animate-rise shadow-sm transition-all'}`}>
        <Virtuoso ref={virtuosoRef} data={filtered} followOutput={autoFollow && !newestFirst ? 'smooth' : false}
          atTopStateChange={setAtTop}
          style={{ flex: 1, minHeight: 0 }}
          itemContent={(_i, l) => (
            <div className={`font-mono ${sz.row} ${wrapLines ? 'whitespace-pre-wrap break-all' : 'whitespace-pre'} px-2.5 py-0.5 leading-[1.7] ${l.match ? 'border-l-2 border-accent bg-accent/10' : ''} ${levelColor(l.level)}`}>
              {l.match ? highlight(l.msg) : l.msg}
            </div>
          )}
        />
      </div>
    </div>
  )
}

export default function LogsPage() {
  return (
    <>
      <PageHeader title={i18nT('pages.logsPage.live_logs')} subtitle={i18nT('pages.logsPage.real_time_application_output')} />
      <LogViewer />
    </>
  )
}
