/** The two lists the Artifacts page shows beside its saved library: the
 *  unsaved documents from the user's chats (`SessionDocsGallery`) and a
 *  publish provider's remote artifacts (`RemoteBrowseSection`). */
import { safeSetItem } from '../utils/safeStorage'
import { useState, useMemo, useEffect, useRef } from 'react'
import { useInfiniteQuery, keepPreviousData } from '@tanstack/react-query'
import { Loader2, ChevronRight, ChevronDown, ChevronUp, FileText } from 'lucide-react'
import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import { Card, CardTitle, Btn, SearchInput } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import Clickable from '../components/Clickable'
import RemoteArtifactCard from '../components/RemoteArtifactCard'
import { timeAgo as _timeAgo } from '../utils/timeAgo'
import type { PublishProviderDescriptor, RemoteArtifact, SessionDoc } from '../types'
import { isoToTs, SessionDocStar, SessionDocPath } from '../components/library/LibraryTable'
import { i18nT } from '../i18n/t'

/** Unsaved session documents in the GALLERY view. The table and tree views
 * fold these into their rows (SessionDocRow) — but the gallery is the DEFAULT
 * view, so without this section a document badged "Artifact" in the chat
 * transcript is invisible on this page until the user discovers the table
 * toggle. Same affordance as SessionDocRow: the leading star materializes the
 * document into a real, starred artifact. */
/* Cap the docs section so an active user's cross-session firehose cannot push
 * the saved library — the page's primary content — below the fold (the same
 * burial this section exists to cure, inverted). Same disclosure pattern as
 * FileChangeChips' COLLAPSED_COUNT. */
const SESSION_DOCS_COLLAPSED = 5

const SESSION_DOCS_COLLAPSE_KEY = 'mc-artifacts-session-docs-collapsed'

export function SessionDocsGallery({ docs, pending, onMaterialize, materializingPath, onPreview }: {
  docs: SessionDoc[]
  /** True while the session-docs query is in flight — renders a fixed-height
   *  skeleton so the section does not pop in and shift the gallery under the
   *  user's cursor once the query resolves. */
  pending: boolean
  onMaterialize: (path: string, sessionKey?: string) => void
  materializingPath: string | null
  /** Row click opens the read-only preview (the star stays the save gesture). */
  onPreview: (d: SessionDoc) => void
}) {
  const [expanded, setExpanded] = useState(false)
  // Persisted: a user who never intends to save these docs can put the section
  // away for good; the header stays as a one-click way back.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SESSION_DOCS_COLLAPSE_KEY) === '1',
  )
  const listRef = useRef<HTMLDivElement>(null)
  const toggleCollapsed = () => {
    setCollapsed((v) => {
      safeSetItem(SESSION_DOCS_COLLAPSE_KEY, v ? '' : '1')
      return !v
    })
  }
  // A successful materialize unmounts its row; without this, focus falls to
  // <body> and keyboard users lose their place. Re-anchor on the list.
  const handleMaterialize = (path: string, sessionKey?: string) => {
    onMaterialize(path, sessionKey)
    listRef.current?.focus()
  }
  if (pending && !docs.length) {
    // Fixed-height placeholder (~header + one row) reserving the slot.
    return (
      <Card className="mt-0 p-3" aria-busy="true">
        <div className="h-[24px] w-40 rounded bg-bg-hover animate-pulse mb-2" />
        <div className="h-[32px] rounded-lg bg-bg-hover animate-pulse" />
      </Card>
    )
  }
  if (!docs.length) return null
  const overflow = docs.length > SESSION_DOCS_COLLAPSED
  const visible = overflow && !expanded ? docs.slice(0, SESSION_DOCS_COLLAPSED) : docs
  return (
    <Card className="mt-0 p-3">
      <CardTitle className={collapsed ? 'mb-0 px-1' : 'mb-2 px-1'}>
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-expanded={!collapsed}
          className="flex items-center gap-2 bg-transparent border-none p-0 cursor-pointer text-inherit"
        >
          {collapsed ? <ChevronRight size={14} className="shrink-0 text-muted" /> : <ChevronDown size={14} className="shrink-0 text-muted" />}
          {i18nT('pages.artifactsPage.from_your_chats')}
          {collapsed && <span className="text-muted font-normal">({docs.length})</span>}
        </button>
      </CardTitle>
      {!collapsed && (
      <>
      {/* Expanded, this list is the full session-doc set (hundreds of rows). It
        * lives inside the page column, which hands its scroll axis to the
        * gallery once that virtualizes — so an unbounded list here is CLIPPED
        * with no way to reach the rest of it. Cap it and let it scroll itself.
        * Collapsed to five rows it needs no cap, so the common case still has a
        * single scroller on the page. */}
      <div
        ref={listRef}
        tabIndex={-1}
        className={`flex flex-col gap-0.5 outline-hidden ${expanded ? 'max-h-[40vh] overflow-y-auto' : ''}`}
      >
        {visible.map((d) => (
          // Click opens a read-only preview — a READ, no registration, no side
          // effects. Promotion into the library stays on the explicit star
          // (which stops propagation so starring never also opens the preview).
          <Clickable
            key={d.path}
            onClick={() => onPreview(d)}
            className="flex items-center gap-2.5 px-2 py-1.5 rounded-lg cursor-pointer hover:bg-bg-hover transition-colors"
          >
            <SessionDocStar d={d} busy={materializingPath === d.path} onMaterialize={handleMaterialize} />
            <FileText size={13} className="text-ok shrink-0" />
            <span className="text-sm text-text-strong font-medium truncate min-w-0 max-w-[280px]">{d.name}</span>
            {/* The path control is as wide as its text, so the free space after
              * it keeps the row's preview click. Wrapping puts a launch
              * refusal on its own line under the path. */}
            <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 min-w-0 flex-1">
              <SessionDocPath path={d.path} className="text-[11px] text-muted truncate min-w-0" />
            </span>
            <span className="text-[12px] text-muted truncate min-w-0 max-w-[180px]" title={d.session_title}>{d.session_title}</span>
            <span className="text-[12px] text-muted whitespace-nowrap shrink-0">{_timeAgo(isoToTs(d.updated_at))}</span>
          </Clickable>
        ))}
      </div>
      {/* OUTSIDE the scrollable list on purpose: inside it, "Show less" sat
        * after the last of hundreds of rows, so the control that undoes the
        * expansion was itself only reachable by scrolling past everything the
        * expansion added. */}
      {overflow && (
        <Btn
          onClick={() => setExpanded((v) => !v)}
          className="justify-center w-full px-2 py-1.5 mt-1 rounded-lg text-[11.5px] font-medium border-none"
          aria-expanded={expanded}
        >
          {expanded
            ? <><ChevronUp size={13} className="shrink-0" /> {i18nT('pages.artifactsPage.show_less')}</>
            : <><ChevronDown size={13} className="shrink-0" /> {i18nT('pages.artifactsPage.show_all_count', { count: docs.length })}</>}
        </Btn>
      )}
      </>
      )}
    </Card>
  )
}

/** Browse one publish provider's remote artifacts (provider-routed; vendor
 * copy comes from the provider's own display_name). Loading stays quiet so a
 * remote cannot block the library; failures use a bounded inline notice. */
export function RemoteBrowseSection({ provider, onForked, onCloned }: {
  provider: PublishProviderDescriptor
  onForked: (slug: string) => void
  onCloned: (slug: string) => void
}) {
  const [search, setSearch] = useState('')
  // Every listing scope the provider advertises, tried in this order. A scope
  // the provider answers `scope_unsupported` for is skipped rather than shown
  // as a failure; once every scope is skipped the section renders nothing.
  const { list_mine, list_shared_with_me, list_public } = provider.discovery_model
  const scopes = useMemo(
    () => [list_mine && 'mine', list_shared_with_me && 'shared', list_public && 'public']
      .filter((s): s is string => !!s),
    [list_mine, list_shared_with_me, list_public],
  )
  const [skippedScopes, setSkippedScopes] = useState(0)
  const scope: string | undefined = scopes[skippedScopes]
  const useSearch = !!search && provider.discovery_model.full_text_search
  const {
    data, isLoading, error, refetch, fetchNextPage, hasNextPage, isFetchingNextPage, isPlaceholderData,
  } = useInfiniteQuery<
    { artifacts: RemoteArtifact[]; next_page_token?: string | null; scopeUnsupported?: boolean }
  >({
    // Tag the key with the mode ('q' vs 'scope') so a full-text query that
    // happens to equal the scope word (e.g. typing "mine") can't collide with
    // the scope listing's cache entry.
    queryKey: ['remote-artifacts', provider.name, useSearch ? ['q', search] : ['scope', scope]],
    queryFn: async ({ pageParam }) => {
      try {
        return await api.browseRemoteArtifacts(provider.name, {
          ...(useSearch ? { q: search } : { scope }),
          ...(pageParam ? { pageToken: pageParam as string } : {}),
        })
      } catch (err) {
        // A scope the provider does not list is its settled answer, not a
        // failure: it resolves as an empty page the section skips, so no retry
        // asks again and no error card renders. Only a listing is skipped; a
        // search the provider cannot run stays an error, because the user typed it.
        if (!useSearch && apiErrorCode(err) === 'scope_unsupported') {
          return { artifacts: [], scopeUnsupported: true }
        }
        throw err
      }
    },
    initialPageParam: '',
    // The provider paginates via next_page_token; stop when it stops handing
    // one out (null/empty ⇒ last page). Without this, remote artifacts beyond
    // the provider's first page would be unreachable.
    getNextPageParam: (last) => last.next_page_token || undefined,
    staleTime: 60_000,
    // Nothing left to list once every advertised scope is skipped.
    enabled: useSearch || scope !== undefined,
    // Keep the prior page's rows while a new full-text query fetches. Without
    // this, every keystroke changes the key → data=undefined → isLoading=true →
    // the section (and the focused SearchInput inside it) unmounts, dropping
    // keyboard focus mid-word.
    placeholderData: keepPreviousData,
  })
  // Read only from the current key's own answer: the placeholder kept while the
  // next scope loads is the skipped scope's page, which would skip again.
  const scopeUnsupported = !isPlaceholderData && !!data?.pages[0]?.scopeUnsupported
  useEffect(() => {
    if (scopeUnsupported) setSkippedScopes((n) => n + 1)
  }, [scopeUnsupported])
  const items: RemoteArtifact[] = (data?.pages || []).flatMap((p) => p.artifacts || [])
  // Drop artifacts already on this device (cloned or forked) — they live in
  // Your Artifacts above, so listing them here too would be a duplicate.
  const notLocal = items.filter(a => !a.local_slug)
  if (isLoading && !notLocal.length) return null
  // Remote discovery is optional to the local library, but its failure must
  // remain actionable without recreating the full-width provider card.
  if (error) {
    const errorMessage = error instanceof Error ? error.message : String(error)
    return (
      <div className="mt-3 flex max-w-lg items-start gap-2">
        <ErrorNotice
          variant="inline"
          title={provider.display_name}
          message={errorMessage}
          askAgent
          className="min-w-0 flex-1"
          messageClassName="line-clamp-2"
          messageTooltip={errorMessage}
          testId={`remote-browse-error-${provider.name}`}
        />
        <Btn onClick={() => refetch()} className="shrink-0">
          {i18nT('pages.artifactsPage.retry')}
        </Btn>
      </div>
    )
  }
  if (!notLocal.length && !search) return null
  const filtered = search && !useSearch
    ? notLocal.filter(a => a.title.toLowerCase().includes(search.toLowerCase()) || a.tags?.some(t => t.toLowerCase().includes(search.toLowerCase())))
    : notLocal
  // In full-text mode the shown rows can be the PREVIOUS query's results
  // (keepPreviousData) while a new query fetches, and they are NOT locally
  // re-filtered — so clone/fork would act on a stale artifact. Disable those
  // actions until the current query resolves. (Scope/list mode isn't affected:
  // its rows are locally filtered and the id-match stays valid.)
  const actionsStale = useSearch && isPlaceholderData
  return (
    <Card className="mt-4">
      <CardTitle>{i18nT('pages.artifactsPage.on')} {provider.display_name}</CardTitle>
      <div className="mb-2">
        <SearchInput placeholder={i18nT('pages.artifactsPage.filter_artifacts', { provider: provider.display_name })} value={search} onChange={e => setSearch((e.target as HTMLInputElement).value)} />
      </div>
      <div className="divide-y divide-border">
        {filtered.map((a) => (
          <RemoteArtifactCard
            key={a.external_id}
            artifact={a}
            provider={provider.name}
            providerLabel={provider.display_name}
            onForked={onForked}
            onCloned={onCloned}
            actionsDisabled={actionsStale}
          />
        ))}
      </div>
      {hasNextPage && (
        <div className="mt-2 flex justify-center">
          {/* Stable aria-label: while loading the button shows only a spinner
              icon, so it needs an accessible name in both states. */}
          <Btn
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
            aria-label={i18nT('pages.artifactsPage.load_more_artifacts', { provider: provider.display_name })}
          >
            {isFetchingNextPage
              ? <Loader2 className="lucide-inline w-3.5 h-3.5 animate-spin" />
              : i18nT('pages.artifactsPage.load_more')}
          </Btn>
        </div>
      )}
    </Card>
  )
}
