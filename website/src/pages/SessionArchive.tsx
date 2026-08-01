import { useState, useEffect, useCallback, useRef } from 'react'

import { i18nT } from '../i18n/t'
interface ArchiveEntry {
  name: string
  key: string
  stamp: string
  size: number
  mtime: number
}

export default function SessionArchive() {
  const [filterKey, setFilterKey] = useState('')
  const [archives, setArchives] = useState<ArchiveEntry[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [contentError, setContentError] = useState('')
  const [contentLoading, setContentLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const abortRef = useRef<AbortController | null>(null)

  const loadList = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const r = await fetch(`/api/session/archive`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const d = await r.json()
      setArchives(d.archives || [])
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadList() }, [loadList])
  useEffect(() => { return () => { abortRef.current?.abort() } }, [])

  const q = filterKey.trim().toLowerCase()
  const visible = q ? archives.filter(a => a.key.toLowerCase().includes(q) || a.name.toLowerCase().includes(q)) : archives

  const openArchive = async (name: string) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setSelected(name)
    setContent('')
    setContentError('')
    setContentLoading(true)
    try {
      const r = await fetch(`/api/session/archive/${encodeURIComponent(name)}`, { signal: controller.signal })
      if (!r.ok) throw new Error(await r.text())
      const text = await r.text()
      setContent(text.length > 200_000 ? text.slice(0, 200_000) + '\n... truncated (showing first 200KB) ...' : text)
    } catch (e) {
      if (!controller.signal.aborted) setContentError(String(e))
    } finally {
      if (abortRef.current === controller) setContentLoading(false)
    }
  }

  const fmtStamp = (s: string) =>
    s.length === 15 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)} ${s.slice(9, 11)}:${s.slice(11, 13)}:${s.slice(13, 15)}` : s

  const fmtSize = (n: number) => n < 1024 ? `${n}B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)}KB` : `${(n / 1024 / 1024).toFixed(2)}MB`

  return (
    <div className="flex gap-4 h-full text-sm">
      <div className="w-1/3 flex flex-col border border-border rounded p-2 overflow-hidden">
        <div className="flex gap-2 mb-2">
          <input aria-label={i18nT('pages.sessionArchive.fuzzy_filter_substring_match')} className="flex-1 bg-bg-2 border border-border rounded px-2 py-1 text-[13px]" placeholder={i18nT('pages.sessionArchive.fuzzy_filter_substring_match')} value={filterKey} onChange={e => setFilterKey(e.target.value)} />
          <button className="px-2 py-1 bg-accent text-accent-fg rounded text-[13px]" onClick={loadList}>{i18nT('pages.sessionArchive.reload')}</button>
        </div>
        {loading && <div className="text-muted text-[13px]">{i18nT('pages.sessionArchive.loading')}</div>}
        {error && <div className="text-red-500 text-[13px]">{error}</div>}
        <div className="overflow-auto flex-1">
          {archives.length === 0 && !loading && !error && <div className="text-muted text-[13px] p-2">{i18nT('pages.sessionArchive.no_archives_archives_are_created_when_session_fi')}</div>}
          {archives.length > 0 && visible.length === 0 && !error && <div className="text-muted text-[13px] p-2">{i18nT('pages.sessionArchive.no_matches_for_query', { query: filterKey })}</div>}
          {visible.map(a => (
            <div
              key={a.name}
              role="button"
              tabIndex={0}
              onClick={() => openArchive(a.name)}
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openArchive(a.name) } }}
              className={`cursor-pointer px-2 py-1 rounded hover:bg-bg-2 ${selected === a.name ? 'bg-bg-2' : ''}`}
            >
              <div className="text-[13px] font-mono truncate" title={a.key}>{a.key}</div>
              <div className="text-[13px] text-muted flex justify-between"><span>{fmtStamp(a.stamp)}</span><span>{fmtSize(a.size)}</span></div>
            </div>
          ))}
        </div>
      </div>
      <div className="flex-1 flex flex-col border border-border rounded p-2 overflow-hidden">
        {!selected && <div className="text-muted text-[13px] p-2">{i18nT('pages.sessionArchive.select_an_archive_to_view_its_contents')}</div>}
        {selected && (
          <>
            <div className="text-[13px] text-muted mb-2 font-mono truncate">{selected}</div>
            {contentLoading && <div className="text-muted text-[13px] p-2">{i18nT('pages.sessionArchive.loading')}</div>}
            {!contentLoading && contentError && <div className="text-red-500 text-[13px] p-2">{contentError}</div>}
            {!contentLoading && !contentError && <pre className="flex-1 overflow-auto text-[13px] bg-bg-2 p-2 rounded whitespace-pre-wrap">{content}</pre>}
          </>
        )}
      </div>
    </div>
  )
}
