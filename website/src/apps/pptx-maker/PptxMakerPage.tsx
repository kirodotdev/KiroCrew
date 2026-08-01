/**
 * PptxMakerPage — the PPTX Maker studio.
 *
 * A BUILTIN dashboard page (rendered by BuiltinAppRoute inside the main React
 * tree), so it talks to its in-gateway routes with same-origin fetch and the
 * dashboard's session cookie — not the app-sdk hooks, which need
 * `<AppApiProvider>` and only wrap standalone installed apps via AppHost.
 *
 * Three views behind one segmented control:
 * - **Decks** — the deck list beside the tabbed deliverable viewer. This is where
 *   a deck being built is watched: the viewer follows whichever deliverable the
 *   agent just wrote.
 * - **Library** — styles and .pptx templates (import, rename, pin, delete).
 * - **Settings** — the deck output directory.
 *
 * Deck GENERATION happens in chat, not here: the app ships four agents
 * (`pptx-maker-spec`, `pptx-maker-vibe`, `pptx-maker-composer`, `pptx-maker-style`) and the page links
 * into a chat session on one of them. Keeping generation in the real chat surface
 * means the user gets the full native chat (follow-up chips, question cards, tool
 * groups, steer-send) rather than a reduced embed.
 */

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  Download,
  Layers,
  Loader2,
  MessageSquarePlus,
  Presentation,
  Sparkles,
} from 'lucide-react'
import {
  Btn,
  Card,
  CardTitle,
  EmptyState,
  Input,
  PageHeader,
  SendBtn,
  StatCard,
} from '../../components/ui'
import Clickable from '../../components/Clickable'
import InfoTip from '../../components/InfoTip'
import SegmentedControl from '../../components/SegmentedControl'
import { SearchInput } from '../../components/ui'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { pptxMakerApi } from './api'
import DeckViewer from './DeckViewer'
import LibraryPanel from './LibraryPanel'
import { POLL_DECKS_MS, POLL_IDLE_MS, POLL_PROVISION_MS, filterDecks } from './lib'

type MainView = 'decks' | 'library' | 'settings'
type LibraryKind = 'styles' | 'templates'

/**
 * The agents this app ships, in the order they are offered.
 *
 * The catalog keys are FULL literals rather than a suffix interpolated at the call
 * site: a key assembled from parts exists nowhere in the source, so the extractor
 * and the unused-key tooling cannot see it and it renders as the raw dotted string
 * if it ever goes missing (`dynamicKeys.test.ts`).
 */
const CHAT_AGENT_LABEL_KEY = {
  'pptx-maker--pptx-maker-spec': 'apps.pptxMaker.pptxMakerPage.mode_spec',
  'pptx-maker--pptx-maker-vibe': 'apps.pptxMaker.pptxMakerPage.mode_vibe',
  'pptx-maker--pptx-maker-style': 'apps.pptxMaker.pptxMakerPage.mode_style',
} as const

const CHAT_AGENT_HINT_KEY = {
  'pptx-maker--pptx-maker-spec': 'apps.pptxMaker.pptxMakerPage.mode_spec_hint',
  'pptx-maker--pptx-maker-vibe': 'apps.pptxMaker.pptxMakerPage.mode_vibe_hint',
  'pptx-maker--pptx-maker-style': 'apps.pptxMaker.pptxMakerPage.mode_style_hint',
} as const

/**
 * Display order. Keyed MAPS above rather than a list of `{agent, labelKey}` records,
 * because `check-i18n-keys.mjs` resolves an index into an object literal but not a
 * field read off an array element — so the record form left both keys unverifiable
 * (`i18nT(entry.labelKey)` widens to `string`) and exempted them from every catalog
 * check. Indexing a keyed map is the pattern that gate names.
 */
/**
 * The DOUBLE HYPHEN is the registered filename, not a typo.
 *
 * `bridges._safe_link_name` writes each app agent into the kiro agents dir as
 * `{app}--{agent}.json`, and the value here goes straight to `kiro-cli --agent`,
 * which resolves it against that filename. The slash form (`pptx-maker/…`) matches
 * nothing there, and `--agent` FALLS BACK to the default agent rather than failing —
 * so every mode button silently opened a plain chat with none of this app's MCP tools
 * or prompt, and looked like it had worked. `test_pptx_maker_agents.py` derives these
 * three strings from `_safe_link_name` itself so they cannot drift.
 */
const CHAT_AGENTS = [
  'pptx-maker--pptx-maker-spec',
  'pptx-maker--pptx-maker-vibe',
  'pptx-maker--pptx-maker-style',
] as const

/** Banner shown until the presentation engine has been provisioned. */
function EngineBanner() {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['pptx-maker', 'engine'],
    queryFn: () => pptxMakerApi.engine(),
    refetchInterval: (query) =>
      query.state.data?.provision.state === 'running' ? POLL_PROVISION_MS : POLL_IDLE_MS,
  })
  const provisionMutation = useMutation({
    mutationFn: () => pptxMakerApi.provisionEngine(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['pptx-maker', 'engine'] }),
  })

  if (!data || data.ready) return null

  const running = data.provision.state === 'running'
  const failed = data.provision.state === 'error'
  const lastLine = data.provision.log.split('\n').filter(Boolean).pop() ?? ''

  return (
    <Card className="mb-4 animate-rise">
      <div className="flex items-start gap-3 flex-wrap">
        {running ? (
          <Loader2 className="lucide-inline text-accent animate-spin shrink-0" />
        ) : (
          <AlertTriangle className="lucide-inline text-warn shrink-0" />
        )}
        <div className="flex-1 min-w-[240px]">
          <div className="text-sm text-text">
            {running
              ? i18nT('apps.pptxMaker.pptxMakerPage.engine_installing', {
                  seconds: data.provision.elapsed,
                })
              : failed
                ? i18nT('apps.pptxMaker.pptxMakerPage.engine_install_failed')
                : i18nT('apps.pptxMaker.pptxMakerPage.engine_not_installed')}
          </div>
          <div className="text-[12px] text-muted mt-1">
            {i18nT('apps.pptxMaker.pptxMakerPage.engine_requirements', {
              tag: data.pinnedTag,
            })}
          </div>
          {lastLine && (
            <div className="text-[12px] text-muted font-mono mt-1 truncate">{lastLine}</div>
          )}
        </div>
        {!running && (
          <SendBtn onClick={() => provisionMutation.mutate()}>
            <Download className="lucide-inline" />
            {failed
              ? i18nT('apps.pptxMaker.pptxMakerPage.retry_install')
              : i18nT('apps.pptxMaker.pptxMakerPage.install_engine')}
          </SendBtn>
        )}
      </div>
    </Card>
  )
}

/** Non-blocking note about optional preview binaries. */
function DepsNote() {
  const { data } = useQuery({
    queryKey: ['pptx-maker', 'deps'],
    queryFn: () => pptxMakerApi.deps(),
    refetchInterval: POLL_IDLE_MS,
  })
  if (!data || data.missing.length === 0) return null
  const labels = data.missing.map((key) => data.labels[key] ?? key).join(' / ')
  return (
    <div className="mb-4 text-[12px] text-muted flex items-start gap-2">
      <AlertTriangle className="lucide-inline text-warn shrink-0" />
      <span>{i18nT('apps.pptxMaker.pptxMakerPage.optional_deps_missing', { labels })}</span>
    </div>
  )
}

/** The deck output directory. */
function SettingsView() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<string | null>(null)
  const { data, isLoading } = useQuery({
    queryKey: ['pptx-maker', 'config'],
    queryFn: () => pptxMakerApi.config(),
  })
  const saveMutation = useMutation({
    mutationFn: (value: string) => pptxMakerApi.setDeckRoot(value),
    onSuccess: () => {
      setDraft(null)
      void queryClient.invalidateQueries({ queryKey: ['pptx-maker', 'config'] })
      void queryClient.invalidateQueries({ queryKey: ['pptx-maker', 'decks'] })
    },
  })

  if (isLoading || !data) {
    return (
      <Card>
        <div className="text-sm text-muted">{i18nT('apps.pptxMaker.pptxMakerPage.loading')}</div>
      </Card>
    )
  }

  const value = draft ?? data.deckRoot

  return (
    <Card>
      <CardTitle>
        {i18nT('apps.pptxMaker.pptxMakerPage.deck_output_directory')}{' '}
        <InfoTip text={i18nT('apps.pptxMaker.pptxMakerPage.deck_output_directory_tip')} />
      </CardTitle>
      <div className="flex items-center gap-2 flex-wrap max-w-2xl">
        <Input
          value={value}
          aria-label={i18nT('apps.pptxMaker.pptxMakerPage.deck_output_directory')}
          placeholder={data.default}
          onChange={(event) => setDraft(event.target.value)}
          className="flex-1 min-w-[220px]"
        />
        <SendBtn
          onClick={() => saveMutation.mutate(value.trim())}
          disabled={saveMutation.isPending || !value.trim() || value === data.deckRoot}
        >
          {i18nT('apps.pptxMaker.pptxMakerPage.save')}
        </SendBtn>
        {draft !== null && <Btn onClick={() => setDraft(null)}>{i18nT('apps.pptxMaker.pptxMakerPage.reset')}</Btn>}
      </div>
      {saveMutation.isError && (
        <div className="mt-3 text-[13px] text-danger">
          {(saveMutation.error as Error).message}
        </div>
      )}
      {saveMutation.isSuccess && (
        <div className="mt-3 text-[13px] text-ok">
          {i18nT('apps.pptxMaker.pptxMakerPage.saved')}
        </div>
      )}
    </Card>
  )
}

export default function PptxMakerPage() {
  const navigate = useNavigate()
  const [view, setView] = useState<MainView>('decks')
  const [libraryKind, setLibraryKind] = useState<LibraryKind>('styles')
  const [query, setQuery] = useState('')
  const [selectedDeck, setSelectedDeck] = useState<string | null>(null)

  const decksQuery = useQuery({
    queryKey: ['pptx-maker', 'decks'],
    queryFn: () => pptxMakerApi.decks(),
    refetchInterval: POLL_DECKS_MS,
  })
  // Memoized so the `?? []` fallback is not a fresh array on every render, which
  // would invalidate the derived memos below on each poll.
  const decks = useMemo(() => decksQuery.data?.decks ?? [], [decksQuery.data])

  const stylesQuery = useQuery({
    queryKey: ['pptx-maker', 'styles'],
    queryFn: () => pptxMakerApi.styles(),
  })
  const templatesQuery = useQuery({
    queryKey: ['pptx-maker', 'templates'],
    queryFn: () => pptxMakerApi.templates(),
  })

  const filtered = useMemo(() => filterDecks(decks, query), [decks, query])
  const slideTotal = useMemo(
    () => decks.reduce((sum, deck) => sum + deck.slideCount, 0),
    [decks],
  )
  const finishedCount = useMemo(
    () => decks.filter((deck) => Boolean(deck.pptxUrl)).length,
    [decks],
  )

  // Deck generation is a chat activity, so "New deck" opens a session on one of
  // this app's agents rather than embedding a reduced chat here.
  const startChat = useMutation({
    mutationFn: async (agent: string) => {
      const result = await api.createChatSlot(undefined, agent)
      return result as { key?: string }
    },
    onSuccess: (result) => {
      navigate(result.key ? `/chat?sid=${encodeURIComponent(result.key)}` : '/chat')
    },
  })

  const activeDeck = selectedDeck ?? filtered[0]?.deckId ?? null

  return (
    <>
      <PageHeader
        title={i18nT('apps.pptxMaker.pptxMakerPage.title')}
        subtitle={i18nT('apps.pptxMaker.pptxMakerPage.subtitle')}
        actions={
          <SegmentedControl
            segments={[
              { key: 'decks', label: i18nT('apps.pptxMaker.pptxMakerPage.view_decks') },
              { key: 'library', label: i18nT('apps.pptxMaker.pptxMakerPage.view_library') },
              { key: 'settings', label: i18nT('apps.pptxMaker.pptxMakerPage.view_settings') },
            ]}
            value={view}
            onChange={(next) => setView(next as MainView)}
            layoutId="pptx-view"
            collapse={false}
          />
        }
      />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <EngineBanner />
        <DepsNote />

        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard
            label={i18nT('apps.pptxMaker.pptxMakerPage.stat_decks')}
            value={decks.length}
            accent
          />
          <StatCard
            label={i18nT('apps.pptxMaker.pptxMakerPage.stat_slides')}
            value={slideTotal}
          />
          <StatCard
            label={i18nT('apps.pptxMaker.pptxMakerPage.stat_finished')}
            value={finishedCount}
          />
          <StatCard
            label={i18nT('apps.pptxMaker.pptxMakerPage.stat_styles')}
            value={stylesQuery.data?.styles.length ?? 0}
          />
          <StatCard
            label={i18nT('apps.pptxMaker.pptxMakerPage.stat_templates')}
            value={templatesQuery.data?.templates.length ?? 0}
          />
        </div>

        {view === 'decks' && (
          <>
            <Card className="mb-4">
              <CardTitle>
                {i18nT('apps.pptxMaker.pptxMakerPage.start_a_deck')}{' '}
                <InfoTip text={i18nT('apps.pptxMaker.pptxMakerPage.start_a_deck_tip')} />
              </CardTitle>
              <div className="flex items-start gap-3 flex-wrap">
                {CHAT_AGENTS.map((agent) => (
                  <div key={agent} className="flex flex-col gap-1 max-w-[230px]">
                    <SendBtn
                      onClick={() => startChat.mutate(agent)}
                      disabled={startChat.isPending}
                    >
                      {agent.endsWith('pptx-maker-style') ? (
                        <Sparkles className="lucide-inline" />
                      ) : (
                        <MessageSquarePlus className="lucide-inline" />
                      )}
                      {i18nT(CHAT_AGENT_LABEL_KEY[agent])}
                    </SendBtn>
                    <span className="text-[12px] text-muted">
                      {i18nT(CHAT_AGENT_HINT_KEY[agent])}
                    </span>
                  </div>
                ))}
              </div>
              {startChat.isError && (
                <div className="mt-3 text-[13px] text-danger">
                  {(startChat.error as Error).message}
                </div>
              )}
            </Card>

            <Card className="flex flex-col min-h-0">
              <CardTitle>
                {i18nT('apps.pptxMaker.pptxMakerPage.decks')}{' '}
                <InfoTip text={i18nT('apps.pptxMaker.pptxMakerPage.decks_tip')} />
              </CardTitle>
              <SearchInput
                value={query}
                aria-label={i18nT('apps.pptxMaker.pptxMakerPage.search_decks')}
                placeholder={i18nT('apps.pptxMaker.pptxMakerPage.search_decks')}
                onChange={(event) => setQuery(event.target.value)}
                className="mb-3"
              />
              {decksQuery.isLoading && (
                <div className="text-sm text-muted">
                  {i18nT('apps.pptxMaker.pptxMakerPage.loading')}
                </div>
              )}
              {!decksQuery.isLoading && decks.length === 0 && (
                <EmptyState
                  icon={<Presentation className="lucide-inline" />}
                  title={i18nT('apps.pptxMaker.pptxMakerPage.no_decks_yet')}
                  subtitle={i18nT('apps.pptxMaker.pptxMakerPage.no_decks_yet_hint')}
                />
              )}
              {decks.length > 0 && filtered.length === 0 && (
                <EmptyState
                  icon={<Layers className="lucide-inline" />}
                  title={i18nT('apps.pptxMaker.pptxMakerPage.no_matching_decks')}
                />
              )}
              {filtered.length > 0 && (
                <div className="flex gap-4 flex-1 min-h-0">
                  <div className="w-60 shrink-0 overflow-y-auto border-r border-border pr-3">
                    {filtered.map((deck) => (
                      <Clickable
                        key={deck.deckId}
                        onClick={() => setSelectedDeck(deck.deckId)}
                        className={`w-full text-left px-2.5 py-2 rounded-md mb-1 text-sm cursor-pointer transition-colors hover:bg-bg-elevated ${
                          activeDeck === deck.deckId ? 'bg-bg-elevated text-accent' : 'text-text'
                        }`}
                      >
                        <div className="truncate font-medium">{deck.name}</div>
                        <div className="text-[12px] text-muted">
                          {i18nT('apps.pptxMaker.pptxMakerPage.slide_count', {
                            count: deck.slideCount,
                          })}
                        </div>
                      </Clickable>
                    ))}
                  </div>
                  <div className="flex-1 min-w-0 flex flex-col min-h-0">
                    {activeDeck ? (
                      <DeckViewer deckId={activeDeck} />
                    ) : (
                      <div className="text-sm text-muted">
                        {i18nT('apps.pptxMaker.pptxMakerPage.select_a_deck')}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </Card>
          </>
        )}

        {view === 'library' && (
          <>
            <div className="mb-4">
              <SegmentedControl
                segments={[
                  { key: 'styles', label: i18nT('apps.pptxMaker.pptxMakerPage.view_styles') },
                  {
                    key: 'templates',
                    label: i18nT('apps.pptxMaker.pptxMakerPage.view_templates'),
                  },
                ]}
                value={libraryKind}
                onChange={(next) => setLibraryKind(next as LibraryKind)}
                layoutId="pptx-library"
                collapse={false}
              />
            </div>
            <LibraryPanel kind={libraryKind} />
          </>
        )}

        {view === 'settings' && <SettingsView />}
      </div>
    </>
  )
}
