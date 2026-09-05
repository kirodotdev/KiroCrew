// Kiro Crew Guide — a searchable, richly-rendered troubleshooting knowledge base.
//
// Left: search + platform/topic filter chips + ranked list. Right: the selected
// entry rendered with the full schema (rich markdown, per-step commands,
// collapsible expectations, "if stuck", crew prompt with Copy / Send to chat).
// The selected entry lives in the ?entry= query param, so in-text entry links
// and a page refresh both restore it. Prose is language-aware (prefers `_zh`
// fields when the dashboard language is Chinese).
import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { AlertCircle } from 'lucide-react'
import EntryList from './EntryList'
import EntryDetail from './EntryDetail'
import { fetchEntries, fetchEntry, fetchIndex, type EntrySummary, type EntryDetail as Entry } from './api'

export default function GuidePage() {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('entry')

  const [query, setQuery] = useState('')
  const [platform, setPlatform] = useState('')
  const [topic, setTopic] = useState('')
  const [entries, setEntries] = useState<EntrySummary[]>([])
  const [listLoading, setListLoading] = useState(false)
  const [listError, setListError] = useState(false)

  const [ids, setIds] = useState<Set<string>>(new Set())
  const [platforms, setPlatforms] = useState<string[]>([])
  const [topics, setTopics] = useState<string[]>([])

  const [detail, setDetail] = useState<Entry | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(false)

  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null)

  // One-time index: id set (for in-text autolinking) + filter facet values.
  useEffect(() => {
    fetchIndex()
      .then((idx) => {
        setIds(new Set(idx.ids))
        setPlatforms(idx.platforms)
        setTopics(idx.topics)
      })
      .catch(() => {})
  }, [])

  const runSearch = useCallback(async (q: string, p: string, tp: string) => {
    setListLoading(true)
    setListError(false)
    try {
      const data = await fetchEntries(q, { platform: p || undefined, topic: tp || undefined, limit: 25 })
      setEntries(data.entries || [])
    } catch {
      setListError(true)
      setEntries([])
    } finally {
      setListLoading(false)
    }
  }, [])

  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current)
    debounce.current = setTimeout(() => void runSearch(query, platform, topic), 200)
    return () => {
      if (debounce.current) clearTimeout(debounce.current)
    }
  }, [query, platform, topic, runSearch])

  // Load the detail whenever the selected id changes (deep-link + refresh safe).
  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      setDetailError(false)
      return
    }
    let cancelled = false
    setDetail(null)
    setDetailLoading(true)
    setDetailError(false)
    fetchEntry(selectedId)
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch(() => {
        if (!cancelled) setDetailError(true)
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [selectedId])

  const select = useCallback(
    (id: string) => {
      const next = new URLSearchParams(params)
      next.set('entry', id)
      setParams(next, { replace: false })
    },
    [params, setParams],
  )

  const retryDetail = useCallback(() => {
    if (selectedId) {
      // Re-trigger the effect by clearing then re-setting is unnecessary; just refetch.
      setDetail(null)
      setDetailLoading(true)
      setDetailError(false)
      fetchEntry(selectedId)
        .then(setDetail)
        .catch(() => setDetailError(true))
        .finally(() => setDetailLoading(false))
    }
  }, [selectedId])

  return (
    <div className="flex h-full min-h-0" style={{ background: 'var(--bg)', color: 'var(--text)' }}>
      <EntryList
        query={query}
        onQuery={setQuery}
        platforms={platforms}
        topics={topics}
        platform={platform}
        topic={topic}
        onPlatform={(p) => setPlatform((cur) => (cur === p ? '' : p))}
        onTopic={(tp) => setTopic((cur) => (cur === tp ? '' : tp))}
        onClear={() => {
          setPlatform('')
          setTopic('')
        }}
        entries={entries}
        loading={listLoading}
        error={listError}
        selectedId={selectedId}
        onSelect={select}
      />

      <div className="flex-1 min-h-0 overflow-y-auto p-6">
        {!selectedId && (
          <div className="h-full flex items-center justify-center text-sm" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.selectPrompt')}
          </div>
        )}
        {selectedId && detailLoading && (
          <div className="h-full flex items-center justify-center text-sm" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.loading')}
          </div>
        )}
        {selectedId && detailError && (
          <div className="h-full flex flex-col items-center justify-center gap-3 text-sm" style={{ color: 'var(--warn)' }}>
            <span className="flex items-center gap-2">
              <AlertCircle size={15} />
              {t('apps.guide.detailError')}
            </span>
            <button
              type="button"
              onClick={retryDetail}
              className="text-xs rounded px-3 py-1 focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
              style={{ color: 'var(--text)', border: '1px solid var(--border)', background: 'var(--card)' }}
            >
              {t('apps.guide.retry')}
            </button>
          </div>
        )}
        {selectedId && detail && !detailLoading && !detailError && (
          <EntryDetail entry={detail} ids={ids} onSelect={select} lang={lang} />
        )}
      </div>
    </div>
  )
}
