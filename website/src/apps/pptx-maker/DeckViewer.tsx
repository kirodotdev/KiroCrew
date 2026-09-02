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
import { ExternalLink, FolderOpen, Presentation } from 'lucide-react'
import { EmptyState } from '../../components/ui'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import SegmentedControl from '../../components/SegmentedControl'
import { revealOrOpen } from '../../components/FilePathMenu'
import { useBranding } from '../../hooks/useBranding'
import { i18nT } from '../../i18n/t'
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
function DocumentTab({ path }: { path: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['pptx-maker', 'doc', path],
    queryFn: () => fetchArtifactText(path),
    refetchInterval: POLL_DOC_MS,
  })
  if (isLoading) return <div className="text-sm text-muted">{i18nT('apps.pptxMaker.deckViewer.loading')}</div>
  if (isError || data === undefined) {
    return <div className="text-sm text-muted">{i18nT('apps.pptxMaker.deckViewer.unavailable')}</div>
  }
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
    <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(320px,1fr))]">
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

export default function DeckViewer({ deckId }: { deckId: string }) {
  const [tab, setTab] = useState<DeckTab>('slides')
  const seenRef = useRef<Record<string, number> | null>(null)
  // Reveal shells out on the gateway host, so it is only useful when the browser
  // is on that same machine. On a remote session the backend degrades reveal to a
  // clipboard copy (and this button already swallowed every failure silently), so
  // hide it there to match every other gated file-location surface.
  const isLocal = useBranding().directLocal

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
        {isLocal && detail.dirPath && (
          <button
            type="button"
            onClick={() => { void revealOrOpen(detail.dirPath, 'reveal') }}
            className="inline-flex items-center gap-1 text-[12px] text-muted px-2 py-1 rounded hover:bg-bg-elevated hover:text-text transition-colors bg-transparent border-none cursor-pointer"
          >
            <FolderOpen className="lucide-inline" />
            {i18nT('apps.pptxMaker.deckViewer.reveal_folder')}
          </button>
        )}
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
      </div>
      <div className="flex-1 min-w-0 overflow-y-auto p-5">
        {activeTab === 'slides' && (
          <SlidesTab detail={detail} defs={defsQuery.data ?? null} />
        )}
        {activeTab === 'artDirection' && detail.specs.artDirection && (
          <BoardTab path={detail.specs.artDirection} />
        )}
        {(activeTab === 'brief' || activeTab === 'outline') && detail.specs[activeTab] && (
          <DocumentTab path={detail.specs[activeTab] as string} />
        )}
      </div>
    </div>
  )
}
