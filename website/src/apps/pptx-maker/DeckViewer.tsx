/**
 * DeckViewer — the tabbed deliverable viewer for one deck.
 *
 * Four tabs in the order the agent produces them: Brief, Outline, Art direction,
 * Slides. The tab follows whichever deliverable was written most recently (see
 * `tabToFollow`), which is what makes the panel narrate a deck being built rather
 * than sitting on whatever the user last clicked.
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, Copy, ExternalLink, FolderOpen, MoreHorizontal, Presentation } from 'lucide-react'
import { Btn, EmptyState } from '../../components/ui'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../../components/ui/dropdown-menu'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import OutlineView from './OutlineView'
import SegmentedControl from '../../components/SegmentedControl'
import { revealOrOpen, useRevealFailure } from '../../components/FilePathMenu'
import ErrorNotice from '../../components/ErrorNotice'
import { useBranding } from '../../hooks/useBranding'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'
import {
  fetchArtifactJson,
  fetchArtifactText,
  pptxMakerApi,
  type ComposeDefs,
  type DeckDetail,
} from './api'
import BoardFrame from './BoardFrame'
import SlidePreview from './SlidePreview'
import {
  DECK_TABS,
  POLL_DECK_MS,
  POLL_DOC_MS,
  SLIDE_ASPECT,
  tabAvailable,
  tabToFollow,
  type DeckTab,
} from './lib'

function tabLabel(tab: DeckTab): string {
  switch (tab) {
    case 'brief':
      return i18nT('apps.pptxMaker.deckViewer.tab_brief')
    case 'outline':
      return i18nT('apps.pptxMaker.deckViewer.tab_outline')
    case 'artDirection':
      return i18nT('apps.pptxMaker.deckViewer.tab_art_direction')
    default:
      return i18nT('apps.pptxMaker.deckViewer.tab_slides')
  }
}

/** A markdown deliverable, re-read on a slow poll so edits appear live. */
function DocumentTab({ path, outline = false }: { path: string; outline?: boolean }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['pptx-maker', 'doc', path],
    queryFn: () => fetchArtifactText(path),
    refetchInterval: POLL_DOC_MS,
  })
  if (isLoading) return <div className="text-sm text-muted">{i18nT('apps.pptxMaker.deckViewer.loading')}</div>
  if (isError || data === undefined) {
    return <div className="text-sm text-muted">{i18nT('apps.pptxMaker.deckViewer.unavailable')}</div>
  }
  if (outline) return <OutlineView markdown={data} />
  return (
    <div className="max-w-3xl">
      <MarkdownRenderer content={data} />
    </div>
  )
}

/** The art-direction board, re-read on the same slow poll. */
function BoardTab({ path }: { path: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['pptx-maker', 'board', path],
    queryFn: () => fetchArtifactText(path),
    refetchInterval: POLL_DOC_MS,
  })
  if (isLoading) return <div className="text-sm text-muted">{i18nT('apps.pptxMaker.deckViewer.loading')}</div>
  if (isError || data === undefined) {
    return <div className="text-sm text-muted">{i18nT('apps.pptxMaker.deckViewer.unavailable')}</div>
  }
  return <BoardFrame html={data} title={i18nT('apps.pptxMaker.deckViewer.tab_art_direction')} />
}

function SlidesTab({ detail, defs }: { detail: DeckDetail; defs: ComposeDefs | null }) {
  if (detail.slides.length === 0) {
    return (
      <EmptyState
        icon={<Presentation className="lucide-inline" />}
        title={i18nT('apps.pptxMaker.deckViewer.no_slides_yet')}
        subtitle={i18nT('apps.pptxMaker.deckViewer.slides_appear_as_the_agent_composes_them')}
      />
    )
  }
  return (
    <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(min(100%,320px),1fr))]">
      {detail.slides.map((slide, index) => (
        <div key={slide.slug}>
          {slide.composeUrl ? (
            <SlidePreview
              composeUrl={slide.composeUrl}
              defs={defs}
              label={`${index + 1}. ${slide.slug}`}
            />
          ) : (
            <div
              className="relative w-full rounded-lg border border-border bg-bg-elevated"
              style={{ paddingBottom: SLIDE_ASPECT }}
            />
          )}
          <div className="text-[12px] text-muted mt-1.5 px-0.5 truncate">
            {index + 1}. {slide.slug}
          </div>
        </div>
      ))}
    </div>
  )
}

/**
 * `handOff` is the page's per-placement hand-off decision for this viewer's
 * notices: beside the studio ChatPane it is off, because asking an agent
 * navigates away and discards the unsent message draft in that composer.
 */
/** `/Users/me/…/20261002-2140-deck` — keeps the root and the deck folder name. */
export function middleTruncate(path: string, max = 44): string {
  if (path.length <= max) return path
  const tail = Math.ceil(max * 0.6)
  return `${path.slice(0, max - tail - 1)}…${path.slice(-tail)}`
}

export default function DeckViewer({ deckId, handOff = true }: { deckId: string; handOff?: boolean }) {
  const [tab, setTab] = useState<DeckTab>('slides')
  const seenRef = useRef<Record<string, number> | null>(null)
  // Reveal shells out on the gateway host, so it is only useful when the browser
  // is on that same machine. On a remote session the backend degrades reveal to a
  // clipboard copy (and this button already swallowed every failure silently), so
  // hide it there to match every other gated file-location surface.
  const isLocal = useBranding().directLocal
  // A failed reveal renders under the header row; askAgent on — the viewer
  // holds no draft.
  const reveal = useRevealFailure(deckId)
  // 'copied' briefly confirms the copy; 'failed' explains a refused clipboard.
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  useEffect(() => {
    if (copyState !== 'copied') return
    const timer = setTimeout(() => setCopyState('idle'), 1500)
    return () => clearTimeout(timer)
  }, [copyState])

  const detailQuery = useQuery({
    queryKey: ['pptx-maker', 'deck', deckId],
    queryFn: () => pptxMakerApi.deck(deckId),
    refetchInterval: POLL_DECK_MS,
  })
  const detail = detailQuery.data

  // The deck's shared SVG defs, fetched once per defs epoch. Keyed on the URL so
  // a recompose that emits new defs refetches, and an unchanged deck does not.
  const defsQuery = useQuery({
    queryKey: ['pptx-maker', 'defs', detail?.defsUrl ?? ''],
    queryFn: () => fetchArtifactJson<ComposeDefs>(detail?.defsUrl as string),
    enabled: Boolean(detail?.defsUrl),
  })

  // Follow the deliverable that just changed. Comparing successive polls (rather
  // than reacting to the newest timestamp outright) is what stops an already-built
  // deck from yanking the user to whatever was last touched.
  useEffect(() => {
    if (!detail) return
    const follow = tabToFollow(seenRef.current, detail.updatedAt)
    seenRef.current = detail.updatedAt
    if (follow) setTab(follow)
  }, [detail])

  // Reset the follow baseline when the user switches decks, or the first poll of
  // the new deck would be diffed against the previous one's timestamps.
  useEffect(() => {
    seenRef.current = null
    setTab('slides')
    setCopyState('idle')
  }, [deckId])

  if (detailQuery.isLoading) {
    return <div className="text-sm text-muted p-5">{i18nT('apps.pptxMaker.deckViewer.loading')}</div>
  }
  if (!detail) {
    return (
      <div className="p-5">
        <EmptyState
          icon={<Presentation className="lucide-inline" />}
          title={i18nT('apps.pptxMaker.deckViewer.deck_not_found')}
        />
      </div>
    )
  }

  const segments = DECK_TABS.filter((candidate) => tabAvailable(detail, candidate)).map(
    (candidate) => ({ key: candidate, label: tabLabel(candidate) }),
  )
  const activeTab = tabAvailable(detail, tab) ? tab : 'slides'

  return (
    <div className="flex flex-col min-h-0 flex-1">
      <div className="flex items-center gap-2 flex-wrap px-3 py-2 border-b border-border shrink-0">
        <SegmentedControl
          segments={segments}
          value={activeTab}
          onChange={(next) => setTab(next as DeckTab)}
          layoutId="pptx-deck-tab"
          collapse={false}
        />
        <div className="flex-1" />
        {detail.pptxUrl && (
          <a
            href={`/api/apps/pptx-maker/${detail.pptxUrl}`}
            download={`${detail.name}.pptx`}
            className="inline-flex items-center gap-1 text-[12px] text-accent px-2 py-1 rounded hover:bg-bg-elevated transition-colors"
          >
            <ExternalLink className="lucide-inline" />
            {i18nT('apps.pptxMaker.deckViewer.download_pptx')}
          </a>
        )}
        {copyState === 'copied' && (
          <span role="status" className="inline-flex items-center gap-1 text-[12px] text-muted">
            <Check className="lucide-inline" />
            {i18nT('apps.pptxMaker.deckViewer.copied_deck_path')}
          </span>
        )}
        {/* One overflow trigger keeps the row at two controls (Download + this). */}
        {detail.dirPath && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Btn
                className="!px-1.5"
                aria-label={i18nT('apps.pptxMaker.deckViewer.more_deck_actions')}
                title={i18nT('apps.pptxMaker.deckViewer.more_deck_actions')}
              >
                <MoreHorizontal size={14} />
              </Btn>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="max-w-[22rem]">
              {/* The deck's directory is the `deck_id` every SDPM tool takes, so
                  pasting it into a new chat points the agent at this exact deck
                  (names can repeat; the path cannot). The item says so. */}
              <DropdownMenuItem
                onSelect={() => {
                  void copyToClipboard(detail.dirPath).then((ok) => setCopyState(ok ? 'copied' : 'failed'))
                }}
                className="items-start"
              >
                <Copy size={13} className="shrink-0 mt-0.5" />
                <span className="flex flex-col min-w-0">
                  <span>{i18nT('apps.pptxMaker.deckViewer.copy_deck_path')}</span>
                  {/* Middle-truncated so the item stays one short hint; the
                      full path is in the tooltip and in what gets copied. */}
                  <span className="text-[12px] text-muted" title={detail.dirPath}>
                    {i18nT('apps.pptxMaker.deckViewer.copy_deck_path_hint', { path: middleTruncate(detail.dirPath) })}
                  </span>
                </span>
              </DropdownMenuItem>
              {isLocal && (
                <DropdownMenuItem onSelect={() => { void revealOrOpen(detail.dirPath, 'reveal', reveal) }}>
                  <FolderOpen size={13} className="shrink-0" />
                  <span>{i18nT('apps.pptxMaker.deckViewer.reveal_folder')}</span>
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {copyState === 'failed' && (
        <div className="px-3 py-2 border-b border-border shrink-0">
          {/* No hand-off when docked beside the studio chat (handOff=false): it
              would navigate away and discard that ChatPane's unsent draft. */}
          <ErrorNotice
            variant="inline"
            className="whitespace-normal"
            title={i18nT('apps.pptxMaker.deckViewer.copy_deck_path_failed')}
            message={detail.dirPath}
            askAgent={handOff}
            onDismiss={() => setCopyState('idle')}
            testId="deck-viewer-copy-error"
          />
        </div>
      )}
      {reveal.error && (
        <div className="px-3 py-2 border-b border-border shrink-0">
          {/* No hand-off when docked beside the studio chat: same draft as above. */}
          <ErrorNotice variant="inline" className="whitespace-normal" message={reveal.error} askAgent={handOff} onDismiss={reveal.clear} testId="deck-viewer-reveal-error" />
        </div>
      )}
      <div className="flex-1 min-w-0 overflow-y-auto p-5">
        {activeTab === 'slides' && (
          <SlidesTab detail={detail} defs={defsQuery.data ?? null} />
        )}
        {activeTab === 'artDirection' && detail.specs.artDirection && (
          <BoardTab path={detail.specs.artDirection} />
        )}
        {activeTab === 'brief' && detail.specs.brief && (
          <DocumentTab path={detail.specs.brief} />
        )}
        {activeTab === 'outline' && detail.specs.outline && (
          <DocumentTab path={detail.specs.outline} outline />
        )}
      </div>
    </div>
  )
}
