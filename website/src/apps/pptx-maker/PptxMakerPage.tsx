/**
 * PptxMakerPage — the PPTX Maker studio.
 *
 * A BUILTIN dashboard page (rendered by BuiltinAppRoute inside the main React
 * tree), so it talks to its in-gateway routes with same-origin fetch and the
 * dashboard's session cookie — not the app-sdk data hooks, which need the SDK's
 * scoped-API layer that a builtin page mounts for itself (`AppScopedApiProvider`).
 * App identity is published for every builtin page by BuiltinAppRoute.
 *
 * Three views behind one segmented control:
 * - **Decks** — the deck list beside the tabbed deliverable viewer. This is where
 *   a deck being built is watched: the viewer follows whichever deliverable the
 *   agent just wrote.
 * - **Library** — styles and .pptx templates (import, rename, pin, delete).
 * - **Settings** — the deck output directory.
 *
 * Deck GENERATION happens in chat, not here: the app ships one `pptx-maker`
 * agent and passes the selected Spec / Vibe / Style role as silent context before
 * opening its chat. The engine's `start_*` tools then supply the role instructions.
 * Keeping generation in the real chat surface means the user gets the full native
 * chat (follow-up chips, question cards, tool groups, steer-send) rather than a
 * reduced embed.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  Download,
  Layers,
  Loader2,
  Maximize2,
  MessageSquarePlus,
  MessageSquareReply,
  Presentation,
  Sparkles,
  X,
} from 'lucide-react'
import {
  Btn,
  Card,
  CardTitle,
  EmptyState,
  IconButton,
  Input,
  PageHeader,
  SendBtn,
  StatCard,
} from '../../components/ui'
import ChatPane from '../../components/ChatPane'
import Clickable from '../../components/Clickable'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import ResizeHandle from '../../components/ResizeHandle'
import SegmentedControl from '../../components/SegmentedControl'
import { SearchInput } from '../../components/ui'
import { api } from '../../api/client'
import type { ChatSlot } from '../../types'
import { useColumnResize } from '../../hooks/useColumnResize'
import { i18nT } from '../../i18n/t'
import { pptxMakerApi } from './api'
import { MODE_CONTEXT } from './modeContext.prompt'
import DeckViewer from './DeckViewer'
import LibraryPanel from './LibraryPanel'
import { POLL_DECKS_MS, POLL_IDLE_MS, POLL_PROVISION_MS, filterDecks } from './lib'
import {
  CHAT_WIDTH_KEY,
  MAX_CHAT_WIDTH,
  MIN_CHAT_WIDTH,
  deckStartedSince,
  loadChatWidth,
  clearStudioChat,
  loadStudioChat,
  saveStudioChat,
  type StudioChat,
} from './studioLayout'

type MainView = 'decks' | 'library' | 'settings'
type LibraryKind = 'styles' | 'templates'

const CHAT_MODES = ['spec', 'vibe', 'style'] as const
type ChatMode = (typeof CHAT_MODES)[number]

/**
 * The three roles offered by the page, in display order. They all dispatch the
 * single declared `pptx-maker` agent; the mode is a server-consumed hint rather
 * than an agent identifier.
 *
 * The catalog keys are FULL literals rather than a suffix interpolated at the call
 * site: a key assembled from parts exists nowhere in the source, so the extractor
 * and the unused-key tooling cannot see it and it renders as the raw dotted string
 * if it ever goes missing (`dynamicKeys.test.ts`).
 */
const CHAT_MODE_LABEL_KEY = {
  spec: 'apps.pptxMaker.pptxMakerPage.mode_spec',
  vibe: 'apps.pptxMaker.pptxMakerPage.mode_vibe',
  style: 'apps.pptxMaker.pptxMakerPage.mode_style',
} as const

const CHAT_MODE_HINT_KEY = {
  spec: 'apps.pptxMaker.pptxMakerPage.mode_spec_hint',
  vibe: 'apps.pptxMaker.pptxMakerPage.mode_vibe_hint',
  style: 'apps.pptxMaker.pptxMakerPage.mode_style_hint',
} as const

/** Banner shown until the presentation engine has been provisioned. */
/**
 * `handOff` decides the provision-failure notice's agent hand-off per placement:
 * the studio layout holds an unsent ChatPane draft that navigating away would
 * discard, the plain decks view holds none.
 */
function EngineBanner({ handOff }: { handOff: boolean }) {
  const queryClient = useQueryClient()
  const { data, error: statusError, isError: statusFailed } = useQuery({
    queryKey: ['pptx-maker', 'engine'],
    queryFn: () => pptxMakerApi.engine(),
    refetchInterval: (query) =>
      query.state.data?.provision.state === 'running' ? POLL_PROVISION_MS : POLL_IDLE_MS,
  })
  const provisionMutation = useMutation({
    mutationFn: () => pptxMakerApi.provisionEngine(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['pptx-maker', 'engine'] }),
  })

  // The POST itself failing (gateway down, refused) never reaches the job state,
  // so it is reported on its own, whatever the job last said.
  const requestError = provisionMutation.isError ? (
    <ErrorNotice
      className="mb-4"
      title={i18nT('apps.pptxMaker.pptxMakerPage.engine_request_failed')}
      message={
        provisionMutation.error instanceof Error ? provisionMutation.error.message : String(provisionMutation.error)
      }
      askAgent={handOff}
      onDismiss={() => provisionMutation.reset()}
      testId="engine-request-error"
    />
  ) : null

  // Rendered here, not under the start buttons, so the studio layout -- which
  // has no start card -- reports it too. Without an answer the start buttons
  // stay disabled, so this is also why they are.
  const statusNotice = statusFailed ? (
    <ErrorNotice
      className="mb-4"
      title={i18nT('apps.pptxMaker.pptxMakerPage.engine_status_failed')}
      message={statusError instanceof Error ? statusError.message : String(statusError)}
      askAgent={handOff}
      testId="engine-status-error"
    />
  ) : null

  if (!data || (data.ready && data.agentReady)) {
    return (
      <>
        {requestError}
        {statusNotice}
      </>
    )
  }

  const running = data.provision.state === 'running'
  const failed = data.provision.state === 'error'
  // `updateRequired` is only meaningful for a marker that names an installed tag.
  // Treat an inconsistent null tag as a first install rather than displaying a
  // fabricated version to the user.
  const updateRequired = data.updateRequired && data.installedTag !== null
  // Built but not registered: the build finished and the agent registration
  // after it failed (or is still pending on a job that ended). Provisioning is
  // idempotent, so the same action retries the registration.
  const agentMissing = data.ready && !data.agentReady
  const lastLine = data.provision.log.split('\n').filter(Boolean).pop() ?? ''

  // Any failed provision -- first install, update, or the agent registration
  // after a build -- is an error, so it goes through ErrorNotice (structured
  // context, retry), titled by what was being attempted. Update-required and
  // agent-missing with no failure are states, not errors, and keep the banner.
  if (failed && !running) {
    const failedTitle = updateRequired
      ? i18nT('apps.pptxMaker.pptxMakerPage.engine_update_failed')
      : agentMissing
        ? i18nT('apps.pptxMaker.pptxMakerPage.engine_setup_failed')
        : i18nT('apps.pptxMaker.pptxMakerPage.engine_install_failed')
    return (
      <>
      {requestError}
      <ErrorNotice
        className="mb-4"
        title={failedTitle}
        message={lastLine || failedTitle}
        askAgent={handOff}
        footer={
          <SendBtn onClick={() => provisionMutation.mutate()}>
            <Download className="lucide-inline" />
            {updateRequired
              ? i18nT('apps.pptxMaker.pptxMakerPage.update_engine')
              : agentMissing
                ? i18nT('apps.pptxMaker.pptxMakerPage.finish_setup')
                : i18nT('apps.pptxMaker.pptxMakerPage.retry_install')}
          </SendBtn>
        }
      />
      </>
    )
  }

  return (
    <>
    {requestError}
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
              : updateRequired
                ? i18nT('apps.pptxMaker.pptxMakerPage.engine_update_required', {
                    installedTag: data.installedTag,
                    pinnedTag: data.pinnedTag,
                  })
                : agentMissing
                  ? i18nT('apps.pptxMaker.pptxMakerPage.engine_agent_not_ready')
                  : i18nT('apps.pptxMaker.pptxMakerPage.engine_not_installed')}
          </div>
          <div className="text-[12px] text-muted mt-1">
            {i18nT('apps.pptxMaker.pptxMakerPage.engine_requirements', {
              tag: data.pinnedTag,
            })}
          </div>
          {/* Progress only: a failure never reaches this banner. */}
          {running && lastLine && (
            <div className="text-[12px] text-muted font-mono mt-1 truncate">{lastLine}</div>
          )}
        </div>
        {!running && (
          <SendBtn onClick={() => provisionMutation.mutate()}>
            <Download className="lucide-inline" />
            {updateRequired
              ? i18nT('apps.pptxMaker.pptxMakerPage.update_engine')
              : agentMissing
                ? i18nT('apps.pptxMaker.pptxMakerPage.finish_setup')
                : i18nT('apps.pptxMaker.pptxMakerPage.install_engine')}
          </SendBtn>
        )}
      </div>
    </Card>
    </>
  )
}

/**
 * Non-blocking note about the optional preview binaries still missing.
 *
 * Shows the install command for each one, because the previous copy named the
 * missing tool and stopped there — leaving the user with a warning and no next
 * step. `pdftoppm` no longer appears here at all on a provisioned install (the
 * app ships its own), so in practice this is the LibreOffice note.
 */
function DepsNote() {
  const { data } = useQuery({
    queryKey: ['pptx-maker', 'deps'],
    queryFn: () => pptxMakerApi.deps(),
    refetchInterval: POLL_IDLE_MS,
  })
  if (!data || data.missing.length === 0) return null
  const labels = data.missing.map((key) => data.labels[key] ?? key).join(' / ')
  const hints = data.missing.map((key) => data.hints?.[key]).filter(Boolean) as string[]
  // One key per phrasing rather than a sentence assembled from fragments: a
  // translator has to control the whole word order, and the command's position
  // differs by language.
  const text = hints.length
    ? i18nT('apps.pptxMaker.pptxMakerPage.optional_deps_missing_with_hint', {
        labels,
        command: hints.join(' / '),
      })
    : i18nT('apps.pptxMaker.pptxMakerPage.optional_deps_missing', { labels })
  return (
    <div className="mb-4 text-[12px] text-muted flex items-start gap-2">
      <AlertTriangle className="lucide-inline text-warn shrink-0" />
      <span className="min-w-0">{text}</span>
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
      {/* The hand-off is offered only while no directory is typed above: `draft`
          is that unsaved value, and Save failing is exactly when it is still here. */}
      {saveMutation.isError && (
        <ErrorNotice
          className="mt-3"
          message={(saveMutation.error as Error).message}
          askAgent={draft === null}
        />
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
  const queryClient = useQueryClient()
  const [view, setView] = useState<MainView>('decks')
  const [libraryKind, setLibraryKind] = useState<LibraryKind>('styles')
  const [query, setQuery] = useState('')
  const [selectedDeck, setSelectedDeck] = useState<string | null>(null)
  // Why the studio chat opened without its mode context, if it did.
  const [contextError, setContextError] = useState<string | null>(null)
  // The studio chat lives in the URL so a reload, or a link back from the full
  // chat, reopens the same session beside the preview.
  const [searchParams, setSearchParams] = useSearchParams()
  const urlSlot = searchParams.get('chat') ?? ''
  const [lastStudioChat, setLastStudioChat] = useState<StudioChat | null>(loadStudioChat)
  // Coming back to the page (from another app or the main chat) drops `?chat=`.
  // A chat the user left open is docked from the very first render -- the
  // page comes back the way it was left, without a flash of the decks layout
  // -- and written back into the URL by the effect below.
  const [restoringSlot, setRestoringSlot] = useState(() =>
    !urlSlot && lastStudioChat?.open ? lastStudioChat.slot : '',
  )
  const chatSlot = urlSlot || restoringSlot
  // Deck ids present when the current chat started. The chat's own deck is the
  // first one that appears afterwards; null after a reload, when the chat may
  // be continuing any deck and the user's own selection is left alone.
  const knownDecksRef = useRef<Set<string> | null>(null)
  const chatColumn = useColumnResize(CHAT_WIDTH_KEY, loadChatWidth, MIN_CHAT_WIDTH, MAX_CHAT_WIDTH)

  const decksQuery = useQuery({
    queryKey: ['pptx-maker', 'decks'],
    queryFn: () => pptxMakerApi.decks(),
    refetchInterval: POLL_DECKS_MS,
  })
  // Memoized so the `?? []` fallback is not a fresh array on every render, which
  // would invalidate the derived memos below on each poll.
  const decks = useMemo(() => decksQuery.data?.decks ?? [], [decksQuery.data])

  // Shares EngineBanner's cache entry. The app agent is only registered once the
  // pinned engine is installed, so a chat started before that would bind to an
  // agent that does not exist yet and fail on its first turn.
  const engineQuery = useQuery({
    queryKey: ['pptx-maker', 'engine'],
    queryFn: () => pptxMakerApi.engine(),
  })
  const engineReady = engineQuery.data?.ready === true && engineQuery.data.agentReady === true

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

  // Deck generation is a chat activity, so "New deck" opens the single app agent
  // and silently supplies the selected role before entering the full chat surface.
  const startChat = useMutation({
    mutationFn: async (mode: ChatMode) => {
      const result = await api.createChatSlot(
        undefined, 'pptx-maker', undefined, undefined, 'persistent',
      ) as { key?: string }
      if (result.key) {
        try {
          await api.chatSlotContext(result.key, MODE_CONTEXT[mode], {
            source: 'pptx-maker',
            ephemeral: true,
          })
        } catch (err) {
          // The slot already exists, so the chat still opens; the lost one-shot
          // context is reported beside it rather than swallowed.
          return { ...result, contextError: err instanceof Error ? err.message : String(err) }
        }
      }
      return { ...result, contextError: null as string | null }
    },
    onSuccess: (result) => {
      if (!result.key) {
        navigate('/chat')
        return
      }
      setContextError(result.contextError)
      openStudioChat(result.key)
    },
  })

  // Only a session list fetched after this moment may prune the remembered
  // chat: one fetched earlier -- before the chat was created, or on a previous
  // visit and still in the query cache -- cannot know about it.
  const freshAfterRef = useRef(Date.now())
  const rememberStudioChat = useCallback((chat: StudioChat) => {
    saveStudioChat(chat)
    setLastStudioChat(chat)
    freshAfterRef.current = Date.now()
    void queryClient.invalidateQueries({ queryKey: ['pptx-maker', 'studio-slots'] })
  }, [queryClient])

  const dockStudioChat = useCallback((key: string, replace = false) => {
    setView('decks')
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('chat', key)
      return next
    }, { replace })
  }, [setSearchParams])

  const openStudioChat = useCallback((key: string) => {
    knownDecksRef.current = new Set(decks.map((deck) => deck.deckId))
    setSelectedDeck(null)
    dockStudioChat(key)
  }, [decks, dockStudioChat])

  // Re-dock a chat this page already ran. It may be continuing any deck, so the
  // user's own selection is left alone (no "follow the new deck").
  const resumeStudioChat = useCallback((key: string, replace = false) => {
    knownDecksRef.current = null
    dockStudioChat(key, replace)
  }, [dockStudioChat])

  const undockStudioChat = useCallback(() => {
    knownDecksRef.current = null
    setContextError(null)
    setRestoringSlot('')
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('chat')
      return next
    })
  }, [setSearchParams])

  const closeStudioChat = useCallback(() => {
    if (chatSlot) {
      rememberStudioChat({
        slot: chatSlot,
        open: false,
        ...(lastStudioChat?.slot === chatSlot && lastStudioChat.title ? { title: lastStudioChat.title } : {}),
      })
    }
    undockStudioChat()
  }, [chatSlot, lastStudioChat, rememberStudioChat, undockStudioChat])

  // Whatever docked the chat (a start button, a resume, a shared link), it is
  // now the one to come back to. Keyed on the slot changing only: reacting to
  // `lastStudioChat` too would re-mark a just-closed chat open in the render
  // between the ✕ and `?chat=` clearing.
  const dockedSlotRef = useRef('')
  useEffect(() => {
    if (chatSlot === dockedSlotRef.current) return
    dockedSlotRef.current = chatSlot
    if (chatSlot) rememberStudioChat({ slot: chatSlot, open: true })
  }, [chatSlot, rememberStudioChat])

  // Put the restored chat back into the URL (replacing, so Back does not
  // bounce between docked and undocked), then let the URL own it again.
  useEffect(() => {
    if (!restoringSlot) return
    if (urlSlot) setRestoringSlot('')
    else resumeStudioChat(restoringSlot, true)
  }, [restoringSlot, urlSlot, resumeStudioChat])

  // The remembered session, as the gateway knows it now: its title and
  // whether the agent is still working name the resume button, and a session
  // deleted or archived while away is forgotten rather than offered back.
  const studioSlotsQuery = useQuery<ChatSlot[]>({
    queryKey: ['pptx-maker', 'studio-slots'],
    queryFn: () => api.chatSlots() as Promise<ChatSlot[]>,
    enabled: Boolean(lastStudioChat),
    refetchOnMount: 'always',
    refetchInterval: lastStudioChat && !lastStudioChat.open ? POLL_DECKS_MS : false,
  })
  const rememberedSlot = studioSlotsQuery.data?.find((slot) => slot.key === lastStudioChat?.slot)
  // Keep the remembered title current, so the next visit can name the chat
  // from its first render. Not a re-remember: it must not hold off pruning.
  useEffect(() => {
    if (!lastStudioChat || !rememberedSlot?.title || rememberedSlot.title === lastStudioChat.title) return
    const next = { ...lastStudioChat, title: rememberedSlot.title }
    saveStudioChat(next)
    setLastStudioChat(next)
  }, [lastStudioChat, rememberedSlot])
  useEffect(() => {
    if (!lastStudioChat || !studioSlotsQuery.data || rememberedSlot) return
    if (studioSlotsQuery.dataUpdatedAt <= freshAfterRef.current) return
    clearStudioChat()
    setLastStudioChat(null)
    if (chatSlot === lastStudioChat.slot) undockStudioChat()
  }, [chatSlot, lastStudioChat, rememberedSlot, studioSlotsQuery.data, studioSlotsQuery.dataUpdatedAt, undockStudioChat])

  // Follow the deck the chat creates, once, so the preview shows the brief the
  // moment the agent writes it. After that the user's own clicks win.
  useEffect(() => {
    const known = knownDecksRef.current
    if (!chatSlot || !known) return
    const started = deckStartedSince(decks.map((deck) => deck.deckId), known)
    if (started) {
      setSelectedDeck(started)
      knownDecksRef.current = null
    }
  }, [chatSlot, decks])

  const activeDeck = selectedDeck ?? filtered[0]?.deckId ?? null

  const studioOpen = Boolean(chatSlot) && view === 'decks'
  const openFullChat = useCallback(() => {
    navigate(`/chat?sid=${encodeURIComponent(chatSlot)}`)
  }, [navigate, chatSlot])

  // The way back to a chat closed with ✕: the top of the deck list, the column
  // the chat sat beside, so it costs no space of its own and is gone when there
  // is nothing to go back to. Named after the session so the user knows what
  // it reopens.
  //
  // Shown from memory on the first render rather than after the session list
  // arrives -- nor for a cached list that predates the chat; the list then
  // supplies the live title and working state, and the pruning effect above
  // (fresh lists only) removes the entry if the session is gone.
  const resumeTitle =
    rememberedSlot?.title || lastStudioChat?.title || i18nT('apps.pptxMaker.pptxMakerPage.studio_chat')
  const resumeEntry =
    !studioOpen && lastStudioChat && !lastStudioChat.open ? (
      <Clickable
        onClick={() => resumeStudioChat(lastStudioChat.slot)}
        aria-label={i18nT('apps.pptxMaker.pptxMakerPage.resume_studio_chat', { title: resumeTitle })}
        className="w-full text-left px-2.5 py-2 rounded-md mb-2 text-sm cursor-pointer transition-colors border border-border hover:bg-bg-elevated flex items-center gap-2"
      >
        <MessageSquareReply className="lucide-inline shrink-0 text-accent" />
        {/* Says it is a chat in visible text, not only in the aria-label: it
            sits above deck rows and would otherwise read as a deck. */}
        <span className="flex flex-col flex-1 min-w-0">
          <span className="text-[12px] text-muted">
            {i18nT('apps.pptxMaker.pptxMakerPage.studio_chat_reopen')}
          </span>
          <span className="truncate">{resumeTitle}</span>
        </span>
        {rememberedSlot?.running && (
          <span className="inline-flex items-center gap-1 text-[12px] text-muted shrink-0">
            <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
            {i18nT('apps.pptxMaker.pptxMakerPage.studio_chat_running')}
          </span>
        )}
      </Clickable>
    ) : null

  const decksCard = (
    <Card className="flex flex-col min-h-0 flex-1">
      <CardTitle>
        {i18nT('apps.pptxMaker.pptxMakerPage.decks')}{' '}
        <InfoTip text={i18nT('apps.pptxMaker.pptxMakerPage.decks_tip')} />
      </CardTitle>
      {lastStudioChat && studioSlotsQuery.isError && (
        <>
          {/* No hand-off while the studio chat is docked (askAgent off): it
              would navigate away and discard that ChatPane's unsent draft. In
              the overview layout there is no draft, so the hand-off is on. */}
          <ErrorNotice
            variant="inline"
            className="mb-3 whitespace-normal"
            title={i18nT('apps.pptxMaker.pptxMakerPage.studio_chat_lookup_failed')}
            message={studioSlotsQuery.error instanceof Error ? studioSlotsQuery.error.message : String(studioSlotsQuery.error)}
            askAgent={!studioOpen}
            testId="studio-chat-lookup-error"
          />
        </>
      )}
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
      {filtered.length === 0 && resumeEntry}
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
        <div className="flex flex-col sm:flex-row gap-4 flex-1 min-h-0">
          {/* Stacked while narrow: a 240px deck list beside the viewer left
              it 86px of a 390px viewport -- 44px once the surrounding Card
              padding is counted. The list is bounded when stacked, or its
              `shrink-0` natural height would push the viewer out. */}
          <div className="w-full sm:w-60 shrink-0 max-h-[40vh] sm:max-h-none overflow-y-auto border-b sm:border-b-0 sm:border-r border-border pb-3 sm:pb-0 sm:pr-3">
            {resumeEntry}
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
              // No hand-off in the studio: the ChatPane beside the viewer holds
              // the user's unsent message draft, which navigating away discards.
              <DeckViewer deckId={activeDeck} handOff={!studioOpen} />
            ) : (
              <div className="text-sm text-muted">
                {i18nT('apps.pptxMaker.pptxMakerPage.select_a_deck')}
              </div>
            )}
          </div>
        </div>
      )}
    </Card>
  )

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
      {studioOpen ? (
        <div className="px-4 md:px-6 pb-4 flex-1 min-h-0 flex flex-col">
          {/* No hand-off here: the studio ChatPane beside it may hold a draft. */}
          <EngineBanner handOff={false} />
          {/* The split follows the pane's own width, not the viewport: a docked
              sidebar or a narrow window must not squeeze the deck preview. Below
              STUDIO_SPLIT_MIN the chat stacks above the decks. */}
          <div className="@container/studio flex-1 min-h-0 flex flex-col">
          <div className="flex flex-col @[56rem]/studio:flex-row flex-1 min-h-0">
            <section
              aria-label={i18nT('apps.pptxMaker.pptxMakerPage.studio_chat')}
              className="flex flex-col min-h-0 h-[60vh] @[56rem]/studio:h-auto shrink-0 w-full @[56rem]/studio:w-[min(var(--pptx-chat-width),calc(100cqw-32rem))] border border-border rounded-xl bg-card overflow-hidden"
              style={{ '--pptx-chat-width': `${chatColumn.width}px` } as CSSProperties}
            >
              <div className="flex items-center gap-1 px-3 py-2 border-b border-border shrink-0">
                <span className="text-sm font-medium truncate flex-1">
                  {i18nT('apps.pptxMaker.pptxMakerPage.studio_chat')}
                </span>
                <IconButton
                  onClick={openFullChat}
                  title={i18nT('apps.pptxMaker.pptxMakerPage.open_full_chat')}
                  aria-label={i18nT('apps.pptxMaker.pptxMakerPage.open_full_chat')}
                >
                  <Maximize2 className="lucide-inline" />
                </IconButton>
                <IconButton
                  onClick={closeStudioChat}
                  title={i18nT('apps.pptxMaker.pptxMakerPage.close_studio_chat')}
                  aria-label={i18nT('apps.pptxMaker.pptxMakerPage.close_studio_chat')}
                >
                  <X className="lucide-inline" />
                </IconButton>
              </div>
              {/* No hand-off: asking an agent would navigate away from the studio
                  ChatPane beside it, discarding the composer draft the user may be
                  typing; the chat itself is already the place to say the mode. */}
              <ErrorNotice
                className="mx-3 mt-2"
                title={i18nT('apps.pptxMaker.pptxMakerPage.mode_context_failed')}
                message={contextError}
                onDismiss={() => setContextError(null)}
              />
              <div className="flex-1 min-h-0">
                <ErrorBoundary>
                  <ChatPane
                    key={chatSlot}
                    slotKey={chatSlot}
                    agentLocked
                    frameless
                    followContentWidth
                    onOpenFull={openFullChat}
                  />
                </ErrorBoundary>
              </div>
            </section>
            <div className="hidden @[56rem]/studio:flex">
              <ResizeHandle
                handleProps={chatColumn.handleProps}
                label={i18nT('apps.pptxMaker.pptxMakerPage.resize_studio_chat')}
                onNudge={chatColumn.nudge}
                value={chatColumn.width}
                min={MIN_CHAT_WIDTH}
                max={MAX_CHAT_WIDTH}
              />
            </div>
            <div className="flex-1 min-w-0 min-h-0 flex flex-col mt-4 @[56rem]/studio:mt-0">{decksCard}</div>
          </div>
          </div>
        </div>
      ) : (
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <EngineBanner handOff />
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
                {CHAT_MODES.map((mode) => (
                  <div key={mode} className="flex flex-col gap-1 max-w-[230px]">
                    <SendBtn
                      onClick={() => startChat.mutate(mode)}
                      disabled={startChat.isPending || !engineReady}
                    >
                      {mode === 'style' ? (
                        <Sparkles className="lucide-inline" />
                      ) : (
                        <MessageSquarePlus className="lucide-inline" />
                      )}
                      {i18nT(CHAT_MODE_LABEL_KEY[mode])}
                    </SendBtn>
                    <span className="text-[12px] text-muted">
                      {i18nT(CHAT_MODE_HINT_KEY[mode])}
                    </span>
                  </div>
                ))}
              </div>
              {engineQuery.data && !engineReady && (
                <div className="mt-3 text-[12px] text-muted">
                  {i18nT(
                    // Name the control the banner actually offers.
                    engineQuery.data.updateRequired && engineQuery.data.installedTag !== null
                      ? 'apps.pptxMaker.pptxMakerPage.start_requires_engine_update'
                      : engineQuery.data.ready && !engineQuery.data.agentReady
                        ? 'apps.pptxMaker.pptxMakerPage.start_requires_finish_setup'
                        : 'apps.pptxMaker.pptxMakerPage.start_requires_engine',
                  )}
                </div>
              )}
              {/* The decks view holds no draft (the search box is a filter). */}
              {startChat.isError && (
                <ErrorNotice className="mt-3" message={(startChat.error as Error).message} askAgent />
              )}
            </Card>

            {decksCard}
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
      )}
    </>
  )
}
