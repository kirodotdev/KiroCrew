import { useState, useEffect, useMemo } from 'react'
import { Trash2, RefreshCw, Download, AlertTriangle } from 'lucide-react'

interface StorageEntry {
  key: string
  bytes: number
  value: string
}

interface StorageGroup {
  prefix: string
  count: number
  bytes: number
  entries: StorageEntry[]
}

const PREFIX_PATTERNS: [RegExp, string][] = [
  [/^vc_heights_/, 'vc_heights_*'],
  [/^kirocrew:touched-files:/, 'kirocrew:touched-files:*'],
  [/^mc-cmt-read:/, 'mc-cmt-read:*'],
  [/^mimir-tasks:/, 'mimir-tasks:*'],
  [/^sort:/, 'sort:*'],
  [/^mc-chat-/, 'mc-chat-*'],
  [/^mc-paste-/, 'mc-paste-*'],
]

function getPrefix(key: string): string {
  for (const [re, label] of PREFIX_PATTERNS) {
    if (re.test(key)) return label
  }
  return '(static keys)'
}

function scanStorage(): StorageEntry[] {
  const entries: StorageEntry[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (!key) continue
    const value = localStorage.getItem(key) || ''
    entries.push({ key, bytes: new Blob([key + value]).size, value })
  }
  return entries.sort((a, b) => b.bytes - a.bytes)
}

function groupEntries(entries: StorageEntry[]): StorageGroup[] {
  const map = new Map<string, StorageGroup>()
  for (const e of entries) {
    const prefix = getPrefix(e.key)
    let g = map.get(prefix)
    if (!g) { g = { prefix, count: 0, bytes: 0, entries: [] }; map.set(prefix, g) }
    g.count++
    g.bytes += e.bytes
    g.entries.push(e)
  }
  return [...map.values()].sort((a, b) => b.bytes - a.bytes)
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(2) + ' MB'
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return bytes + ' B'
}

/** Approximate quota (browsers vary; 5 MB is the common floor) */
const ESTIMATED_QUOTA = 5 * 1024 * 1024

export default function LocalStorageDebug() {
  const [entries, setEntries] = useState<StorageEntry[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  const refresh = () => setEntries(scanStorage())
  useEffect(refresh, [])

  const groups = useMemo(() => groupEntries(entries), [entries])
  const totalBytes = useMemo(() => entries.reduce((s, e) => s + e.bytes, 0), [entries])
  const usagePercent = Math.min(100, (totalBytes / ESTIMATED_QUOTA) * 100)

  const filtered = useMemo(() => {
    if (!filter) return entries
    const q = filter.toLowerCase()
    return entries.filter(e => e.key.toLowerCase().includes(q))
  }, [entries, filter])

  const deleteKey = (key: string) => {
    localStorage.removeItem(key)
    refresh()
  }

  const deleteGroup = (prefix: string) => {
    const g = groups.find(x => x.prefix === prefix)
    if (!g) return
    for (const e of g.entries) localStorage.removeItem(e.key)
    refresh()
  }

  const exportAll = () => {
    const data: Record<string, string> = {}
    for (const e of entries) data[e.key] = e.value
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `kirocrew-localstorage-${Date.now()}.json`
    a.click(); URL.revokeObjectURL(url)
  }

  const clearOrphans = () => {
    let removed = 0
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i)
      if (k && k.startsWith('vc_heights_')) { localStorage.removeItem(k); removed++ }
    }
    refresh()
    return removed
  }

  return (
    <div className="space-y-4">
      {/* ── Usage Overview ── */}
      <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2">Usage</h4>
      <div className="card-glow border border-border bg-card rounded-lg p-5 mb-4 shadow-sm">
        <div className="flex items-center justify-between mb-3">
          <div>
            <span className="text-sm font-medium text-text">{entries.length} keys</span>
            <span className="text-muted text-sm ml-2">{formatBytes(totalBytes)} / ~5 MB</span>
          </div>
          <div className="flex gap-2">
            <button onClick={refresh} className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs border border-border bg-card text-text hover:bg-bg-hover transition-all" title="Refresh"><RefreshCw size={13} /> Refresh</button>
            <button onClick={exportAll} className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs border border-border bg-card text-text hover:bg-bg-hover transition-all" title="Export JSON"><Download size={13} /> Export</button>
            <button onClick={clearOrphans} className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs border border-danger/50 bg-card text-danger hover:bg-danger-subtle transition-all" title="Delete cached scroll positions from old sessions (safe — rebuilds automatically)">
              <Trash2 size={13} /> Clear Old Caches
            </button>
          </div>
        </div>

        <div className="w-full h-2 rounded-full bg-muted/30 overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${usagePercent > 80 ? 'bg-danger' : usagePercent > 50 ? 'bg-warn' : 'bg-accent'}`}
            style={{ width: `${usagePercent}%` }}
          />
        </div>
        {usagePercent > 80 && (
          <div className="flex items-center gap-1.5 text-xs text-danger mt-2">
            <AlertTriangle size={12} /> Storage is {usagePercent.toFixed(0)}% full — app may crash on next write
          </div>
        )}
      </div>

      {/* ── Categories ── */}
      <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2">By Category</h4>
      <div className="card-glow border border-border bg-card rounded-lg p-5 mb-4 shadow-sm">
        <div className="flex items-center justify-between pb-2 mb-1 border-b border-border text-[11px] text-muted">
          <span className="flex-1">Prefix</span>
          <span className="w-14 text-right">Keys</span>
          <span className="w-20 text-right">Size</span>
          <span className="w-16"></span>
        </div>
        <div className="flex flex-col">
          {groups.map(g => (
            <div key={g.prefix} className="flex items-center justify-between py-2 border-b border-border/50 last:border-0">
              <span className="font-mono text-xs text-text flex-1">{g.prefix}</span>
              <span className="w-14 text-right text-xs text-muted">{g.count}</span>
              <span className="w-20 text-right text-xs text-muted">{formatBytes(g.bytes)}</span>
              <span className="w-16 flex justify-end">
                {g.prefix !== '(static keys)' && (
                  <button
                    onClick={() => deleteGroup(g.prefix)}
                    className="flex items-center gap-1 px-2 py-0.5 rounded text-[11px] border border-danger/30 text-danger hover:bg-danger-subtle transition-all"
                    title={`Delete all ${g.count} keys`}
                  >
                    <Trash2 size={10} /> Clear
                  </button>
                )}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* ── Key Inspector ── */}
      <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2">All Keys</h4>
      <div className="card-glow border border-border bg-card rounded-lg p-5 mb-4 shadow-sm">
        <input
          type="text"
          placeholder="Filter keys..."
          aria-label="Filter keys"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          className="w-full px-3 py-2 text-xs rounded-md border border-border bg-bg text-text placeholder:text-muted mb-3 focus:border-accent focus:outline-none"
        />

        <div className="space-y-px">
          {filtered.slice(0, 200).map(e => (
            <div key={e.key} className="group">
              <div
                className="flex items-center gap-2 py-1.5 px-2 rounded hover:bg-bg-hover cursor-pointer text-xs"
                role="button"
                tabIndex={0}
                aria-expanded={expanded === e.key}
                onClick={() => setExpanded(expanded === e.key ? null : e.key)}
                onKeyDown={ev => {
                  if (ev.target === ev.currentTarget && (ev.key === 'Enter' || ev.key === ' ')) {
                    ev.preventDefault()
                    setExpanded(expanded === e.key ? null : e.key)
                  }
                }}
              >
                <span className="font-mono text-text truncate flex-1">{e.key}</span>
                <span className="text-muted flex-shrink-0">{formatBytes(e.bytes)}</span>
                <button
                  onClick={ev => { ev.stopPropagation(); deleteKey(e.key) }}
                  className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-danger-subtle text-danger transition-all"
                  title="Delete key"
                  aria-label="Delete key"
                >
                  <Trash2 size={12} />
                </button>
              </div>
              {expanded === e.key && (
                <pre className="text-[10px] text-muted bg-bg rounded-md px-3 py-2 max-h-32 overflow-auto whitespace-pre-wrap break-all mb-1 border border-border/50">
                  {e.value.length > 2000 ? e.value.slice(0, 2000) + '\n\n… (truncated)' : e.value}
                </pre>
              )}
            </div>
          ))}
          {filtered.length > 200 && (
            <div className="text-xs text-muted py-3 text-center">
              Showing 200 of {filtered.length} — use filter to narrow
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
