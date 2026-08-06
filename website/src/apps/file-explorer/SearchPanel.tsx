import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, X, FileText } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { EmptyState } from '../../components/ui'
import { fileExplorerApi } from './api'
import { basename } from './utils'
import { useDebouncedValue } from './hooks'
import type { SearchResult } from './types'

import { i18nT } from '../../i18n/t'
interface SearchPanelProps {
  rootPath: string
  onClose: () => void
  onJump: (r: SearchResult) => void
}

function shortenPath(p: string, root: string) {
  if (root && p.startsWith(root)) return p.slice(root.length).replace(/^\//, '')
  return p
}

export default function SearchPanel({ rootPath, onClose, onJump }: SearchPanelProps) {
  const [q, setQ] = useState('')
  const [include, setInclude] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  useEffect(() => { inputRef.current?.focus() }, [])

  const debouncedQ = useDebouncedValue(q, 250)
  const debouncedInclude = useDebouncedValue(include, 250)

  const { data, isLoading: searching, error } = useQuery({
    queryKey: ['file-explorer', 'search', rootPath, debouncedQ, debouncedInclude],
    queryFn: () => fileExplorerApi.search(rootPath, debouncedQ, debouncedInclude),
    enabled: !!debouncedQ.trim(),
  })

  const results = data?.results || []
  const engine = data?.engine || ''
  const truncated = !!data?.truncated

  return (
    <div className="mc-fe-search">
      <div className="mc-fe-search-bar">
        <Search size={14} style={{ color: 'var(--muted)' }} />
        <input
          ref={inputRef}
          aria-label={i18nT('apps.fileExplorer.searchPanel.search_in', { name: basename(rootPath) || rootPath })}
          className="mc-fe-search-input"
          type="text"
          placeholder={i18nT('apps.fileExplorer.searchPanel.search_in_2', { name: basename(rootPath) || rootPath })}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape') onClose() }}
        />
        <input
          aria-label={i18nT('apps.fileExplorer.searchPanel.glob_filter_comma_separated_patterns')}
          className="mc-fe-search-glob"
          type="text"
          placeholder={i18nT('apps.fileExplorer.searchPanel.glob_e_g_md')}
          value={include}
          onChange={(e) => setInclude(e.target.value)}
          title={i18nT('apps.fileExplorer.searchPanel.comma_separated_glob_patterns')}
        />
        <button className="mc-fe-iconbtn" onClick={onClose} title={i18nT('apps.fileExplorer.searchPanel.close_esc')} aria-label={i18nT('apps.fileExplorer.searchPanel.close_search')}><X size={14} /></button>
      </div>
      <div className="mc-fe-search-status">
        {searching ? i18nT('apps.fileExplorer.searchPanel.searching')
          : error ? <span style={{ color: 'var(--danger)' }}>{(error as Error).message || i18nT('apps.fileExplorer.searchPanel.search_failed')}</span>
          : `${results.length} result${results.length !== 1 ? 's' : ''}${truncated ? ' (capped)' : ''}${engine ? ` · ${engine}` : ''}`}
      </div>
      <div className="mc-fe-search-results">
        {results.length === 0 && !searching && q && !error
          ? <EmptyState icon={<Search size={22} />} title={i18nT('apps.fileExplorer.searchPanel.no_matches')} />
          : results.map((r, i) => (
              <Clickable
                key={`${r.file}:${r.line}:${i}`}
                className="mc-fe-search-row"
                onClick={() => onJump(r)}
              >
                <div className="mc-fe-search-file">
                  <FileText size={11} style={{ marginRight: 4, opacity: 0.6 }} />
                  {shortenPath(r.file, rootPath)}
                  <span style={{ color: 'var(--muted)', marginLeft: 6 }}>:{r.line}</span>
                </div>
                <div className="mc-fe-search-preview">{r.preview}</div>
              </Clickable>
            ))}
      </div>
    </div>
  )
}
