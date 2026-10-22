import { useState, useCallback, useMemo } from 'react'
import { createTranscriptRenderers, type TranscriptRendererOptions } from '../pages/chat/transcriptRenderers'
import type { ComposerBusyMode } from './ChatInput'
import type { CrewmateIdentity } from '../pages/chat/CrewmateMessage'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { i18nT } from '../i18n/t'
import { earlierConversationRenderer, foldEarlierConversation } from './EarlierConversation'
import type { ChatPaneProps } from './ChatPaneProps'

/**
 * `ChatPane`'s transcript content. `useChatPaneRenderers` builds the row set the
 * pane hands `ChatMessageList`: the same store-connected host renderers the
 * single-chat surface uses, the tool rows' expanded state held above the rows,
 * and the fresh-start "Earlier conversation" fold. `ChatPaneAboveRows` is what
 * the transcript draws above its first row: the history-load failure with its
 * retry, the empty or crewmate-quiet hint, and the link to the full session.
 */

type Messages = Parameters<typeof foldEarlierConversation>[0]

export function useChatPaneRenderers({ slotKey, busyMode, onFileOpen, crewmate, crewmateTranscript, onSessionOpen, sessions, activeSession, messages, foldBefore, resume: { continuable, interrupted, continuing, onContinue } }: {
  slotKey: string
  busyMode: ComposerBusyMode
  onFileOpen?: ChatPaneProps['onFileOpen']
  crewmate?: CrewmateIdentity
  crewmateTranscript: TranscriptRendererOptions['crewmateTranscript']
  onSessionOpen?: (key: string) => void
  sessions?: ReadonlyMap<string, string>
  activeSession?: string
  messages: Messages
  foldBefore?: string
  /** The Resume press for this slot (`useChatPaneContinue`), for the newest error row. */
  resume: { continuable: boolean; interrupted: boolean; continuing: boolean; onContinue: () => void }
}) {
  // Split-view panes draw the SAME transcript rows as the single-chat surface,
  // through the SDK's row registry: the live ToolCallLine (purpose / input /
  // output / live status), the workflow and sub-agent launch cards, thinking
  // traces, sent files, auto-nudge turns, recovery injects, workflow
  // completions. The SDK's built-in registry is store-free by design and so
  // draws weaker rows — or nothing at all — for most of these; the
  // store-connected set is supplied here as host entries instead, which is the
  // registry's intended extension path and keeps app-sdk/ChatMessageList
  // Redux-free for the embed SDK.
  //
  // The tool rows' expanded state is held ABOVE the rows: a row remounts
  // whenever the message list updates, and would otherwise forget it.
  const [toolDisclosure, setToolDisclosure] = useState<Record<string, boolean>>({})
  const setToolDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setToolDisclosure((prev) => ({ ...prev, [key]: expanded }))
  }, [])
  // The fresh-start fold opens per fresh start: a newer one closes it again.
  const [openFold, setOpenFold] = useState<string | undefined>(undefined)
  const foldOpen = !!foldBefore && openFold === foldBefore
  const listMessages = useMemo(
    () => foldEarlierConversation(messages, foldBefore, foldOpen),
    [messages, foldBefore, foldOpen],
  )
  const baseRenderers = useMemo(
    () => createTranscriptRenderers({
      slot: slotKey,
      toolDisclosure,
      onToolDisclosureChange: setToolDisclosureFor,
      // A steer-only surface has no steer/queue concept to explain, so a
      // confirmed steer draws as an ordinary message: no badge, no tint.
      hideSteerBadge: busyMode === 'steer-only',
      // Attachment cards / @mention chips open through the host's file viewer
      // (#9487); without it they render without an opener.
      onFileOpen,
      crewmate,
      crewmateTranscript,
      // Session links resolve through the SAME renderer path the single-chat
      // page uses; there is no second resolver. Absent from the host = the
      // renderer's own gate leaves them plain.
      onSessionOpen,
      sessions,
      activeSession,
      // The newest error row's Resume, with the row set's own per-row remedies
      // (a sign-in, a fresh session) still withholding it where a retry cannot help.
      continuable,
      interrupted,
      continuing,
      onContinue,
    }),
    [slotKey, toolDisclosure, setToolDisclosureFor, busyMode, onFileOpen, crewmate, crewmateTranscript, onSessionOpen, sessions, activeSession, continuable, interrupted, continuing, onContinue],
  )
  const renderers = useMemo(
    () => (foldBefore
      ? [earlierConversationRenderer(foldOpen, () => setOpenFold(foldOpen ? undefined : foldBefore)), ...baseRenderers]
      : baseRenderers),
    [baseRenderers, foldBefore, foldOpen],
  )

  return { listMessages, renderers }
}

export function ChatPaneAboveRows({ slotKey, activeSlot, messages, paneMessages, crewmate, crewmateLive, running, slotDetailFailed, refetchSlotDetail, hideEmptyHint, crewmateQuietUnproven, onOpenCrewWorkLog, warmHasMore, pagesPane, onOpenFull }: {
  slotKey: string
  activeSlot: string | null
  messages: Messages
  paneMessages: Messages
  crewmate?: CrewmateIdentity
  crewmateLive: boolean
  running: boolean
  slotDetailFailed: boolean
  refetchSlotDetail: () => unknown
  hideEmptyHint?: boolean
  crewmateQuietUnproven: boolean
  onOpenCrewWorkLog?: () => void
  warmHasMore?: boolean
  pagesPane: boolean
  onOpenFull?: ChatPaneProps['onOpenFull']
}) {
  return (
    <>
      {slotDetailFailed && (
        <div className="mx-4 my-2 flex items-start gap-2">
          {/* No hand-off: the composer draft (`input`) in this pane is unsaved local
              state. The retry is the recovery path for the hydration read. */}
          <ErrorNotice
            className="flex-1"
            testId="chat-pane-hydrate-error"
            message={i18nT('components.chatPane.history_load_failed')}
          />
          <Btn onClick={() => { void refetchSlotDetail() }}>{i18nT('components.chatPane.retry')}</Btn>
        </div>
      )}
      {/* A crewmate whose whole history is machinery (a patroller that
          has not spoken yet) filters to an empty chat. That is not a
          fresh thread, so it must not read as one: say who has not
          spoken and where the work went, instead of "type a message
          to start" beside a summary that counts its wakes. Said only
          once no older history is left to page (`crewmateQuietUnproven`
          in ChatPane.tsx): a bounded window with no speech in it is not proof.
          Nor while the crewmate is at work (`crewmateLive`, the same
          liveness the status line and footer read): its current step
          is on screen, so "hasn't said anything" would sit under a
          line that shows it busy. */}
      {messages.length === 0 && !(crewmate ? crewmateLive : running) && !slotDetailFailed && !hideEmptyHint && !crewmateQuietUnproven && (
        <div className="text-center text-muted text-[13px] px-4 py-8" data-testid={crewmate && paneMessages.length > 0 ? 'crewmate-quiet-hint' : undefined}>
          {crewmate && paneMessages.length > 0 ? (
            <>
              <div>{i18nT('components.chatPane.crewmate_quiet', { name: crewmate.label || crewmate.name })}</div>
              {/* Where the work went: Profile > Sessions. A link when the host
                  can open that tab — the words read as a destination,
                  so they must be one. */}
              {onOpenCrewWorkLog ? (
                <button
                  type="button"
                  onClick={onOpenCrewWorkLog}
                  data-testid="crewmate-quiet-where"
                  className="mt-1 text-accent underline bg-transparent border-none cursor-pointer hover:text-accent-hover transition-colors"
                >
                  {i18nT('components.chatPane.crewmate_quiet_where')}
                </button>
              ) : (
                <div className="mt-1" data-testid="crewmate-quiet-where">{i18nT('components.chatPane.crewmate_quiet_where')}</div>
              )}
            </>
          ) : (
            i18nT('components.chatPane.session_ready_type_a_message_to_start')
          )}
        </div>
      )}
      {/* Suppressed on the active slot: that pane renders the store's full
          history, so the bound does not apply and the row would be false.
          Also suppressed while the pane can page itself: the bar above
          already loads the older rows in place. */}
      {warmHasMore && slotKey !== activeSlot && !pagesPane && onOpenFull && (
        <button
          onClick={() => onOpenFull(slotKey, messages[0]?.ts, messages[0]?.meta?.mid as string | undefined)}
          className="block w-full text-center text-accent text-[12px] underline py-2 bg-transparent border-none cursor-pointer hover:text-accent-hover transition-colors"
        >
          {i18nT('components.chatPane.earlier_messages_open_session')}
        </button>
      )}
    </>
  )
}
