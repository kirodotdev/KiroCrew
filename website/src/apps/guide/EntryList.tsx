// The left column: search box, filter chips, and the ranked entry list.
import { useTranslation } from 'react-i18next'
import { LifeBuoy, Search, ChevronRight, AlertCircle } from 'lucide-react'
import FilterChips from './FilterChips'
import { type EntrySummary } from './api'

const TRUST_DOT: Record<string, string> = {
  confirmed: 'var(--ok)',
  'known-bug': 'var(--warn)',
  unverified: 'var(--muted)',
}

export default function EntryList({
  query,
  onQuery,
  platforms,
  topics,
  platform,
  topic,
  onPlatform,
  onTopic,
  onClear,
  entries,
  loading,
  error,
  selectedId,
  onSelect,
}: {
  query: string
  onQuery: (q: string) => void
  platforms: string[]
  topics: string[]
  platform: string
  topic: string
  onPlatform: (p: string) => void
  onTopic: (t: string) => void
  onClear: () => void
  entries: EntrySummary[]
  loading: boolean
  error: boolean
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col min-h-0 w-80 shrink-0" style={{ borderRight: '1px solid var(--border)' }}>
      <div className="p-4" style={{ borderBottom: '1px solid var(--border)' }}>
        <div className="flex items-center gap-2 mb-3">
          <LifeBuoy size={18} style={{ color: 'var(--accent)' }} />
          <span className="font-semibold text-sm">{t('apps.guide.title')}</span>
        </div>
        <div
          className="flex items-center gap-2 px-3 py-2 rounded-lg focus-within:ring-1 focus-within:ring-[var(--accent)]"
          style={{ border: '1px solid var(--border)', background: 'var(--card)' }}
        >
          <Search size={15} style={{ color: 'var(--muted)' }} />
          <input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder={t('apps.guide.searchPlaceholder')}
            aria-label={t('apps.guide.searchPlaceholder')}
            className="flex-1 bg-transparent outline-none text-sm"
            style={{ color: 'var(--text)' }}
          />
        </div>
      </div>
      <FilterChips
        platforms={platforms}
        topics={topics}
        platform={platform}
        topic={topic}
        onPlatform={onPlatform}
        onTopic={onTopic}
        onClear={onClear}
      />
      <div className="flex-1 min-h-0 overflow-y-auto">
        {error && (
          <div className="flex items-center gap-2 p-4 text-sm" style={{ color: 'var(--warn)' }}>
            <AlertCircle size={15} />
            {t('apps.guide.errorLoading')}
          </div>
        )}
        {!error && loading && entries.length === 0 && (
          <div className="p-4 text-sm" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.loading')}
          </div>
        )}
        {!error && !loading && entries.length === 0 && (
          <div className="p-4 text-sm" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.noResults')}
          </div>
        )}
        {entries.map((e) => (
          <button
            key={e.id}
            onClick={() => onSelect(e.id)}
            className="w-full text-left px-4 py-3 flex items-start gap-2 focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
            style={{
              borderBottom: '1px solid var(--border)',
              background: e.id === selectedId ? 'var(--card)' : 'transparent',
            }}
          >
            <span
              className="mt-1.5 shrink-0 rounded-full"
              style={{ width: 7, height: 7, background: TRUST_DOT[e.trust || ''] || 'var(--muted)' }}
              aria-hidden="true"
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{e.title}</span>
              {e.symptom && (
                <span className="block text-xs mt-0.5" style={{ color: 'var(--muted)' }}>
                  {e.symptom}
                </span>
              )}
            </span>
            <ChevronRight size={14} className="mt-0.5 shrink-0 ml-auto" style={{ color: 'var(--muted)' }} />
          </button>
        ))}
      </div>
    </div>
  )
}
