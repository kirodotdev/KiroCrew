/**
 * The Bot page side panel's Sessions tab: the Bot's sessions as a list, and one
 * session opened inside the tab to read and type to.
 *
 * The list is the Profile card's (`MateSessionsList`); a row click opens that
 * session here. The open session is a `ChatPane` bound to the worker's own slot,
 * the way split view hosts a second session: it reads that slot's history and
 * sends to it without making it the chat page's active session, so the Bot's
 * thread on the left stays where it is. Back or Escape returns to the list.
 *
 * Which session is open is the host's state (`openKey`), kept per Bot, so it
 * survives a switch to the Dashboard tab and back, and the Profile card can open
 * a session here. The Dashboard tab never learns about it.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronLeft, ExternalLink } from 'lucide-react'
import ChatPane from '../../components/ChatPane'
import ErrorNotice from '../../components/ErrorNotice'
import { RunInTerminalScope } from '../../components/runInTerminalScope'
import { useSlotRunsHere } from '../../components/sessionEnvironmentHooks'
import { useRunInTerminalBridge, useRunInTerminalRefs } from '../../hooks/useRunInTerminalBridge'
import { anotherEscapeOwnerIsOpen } from '../../components/chat-input/escapeOwners'
import { escapeStoppedSpeech } from '../../utils/speechStopKey'
import { api } from '../../api/client'
import { useAppDispatch, useAppSelector } from '../../store'
import { addSlotOptimistic, updateSlot } from '../../store/dashboardSlice'
import { slotRowByKey } from '../../store/slotIndex'
import { CREW_GROUP_TONE, crewRouteNotServed, crewWorkerGroupOfRow } from '../../lib/crewWorkers'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import type { ChatSlot } from '../../types'
import MateSessionsList, { MateSessionRecap, useMateSessionWorkers } from './MateSessionsList'

export interface BotSessionsPanelTabProps {
  /** The Bot thread's slot key: the root its sessions are stamped with. */
  root: string
  mateName: string
  /** The Bot's open sessions from the store. */
  liveSlots: readonly ChatSlot[]
  slotsLoaded: boolean
  /** The session open in the tab, or null for the list. */
  openKey: string | null
  onOpenKey: (key: string | null) => void
  /** Leave for the chat page on that session. */
  onOpenInChat: (key: string) => void
  /** Whether the open session's pane holds focus, so the host can hand it the
   *  page's one dictation key and take it from the Bot thread. */
  onPaneFocusChange?: (focused: boolean) => void
  /** The side panel is mounted but hidden (`display:none`): a pane nobody can
   *  see gives the dictation key back. */
  panelHidden?: boolean
}

export default function BotSessionsPanelTab({ root, mateName, liveSlots, slotsLoaded, openKey, onOpenKey, onOpenInChat, onPaneFocusChange, panelHidden = false }: BotSessionsPanelTabProps) {
  const { t } = useTranslation()
  if (openKey) {
    return (
      <BotSessionView key={openKey} root={root} sessionKey={openKey} liveSlots={liveSlots} slotsLoaded={slotsLoaded}
        onBack={() => onOpenKey(null)} onReopened={onOpenKey} onOpenInChat={onOpenInChat} onPaneFocusChange={onPaneFocusChange} panelHidden={panelHidden} />
    )
  }
  return (
    <div className="h-full min-h-0 overflow-y-auto p-3" data-testid="bot-sessions-panel">
      <MateSessionsList root={root} mateName={mateName} liveSlots={liveSlots} slotsLoaded={slotsLoaded}
        onOpen={w => onOpenKey(w.key)}
        empty={<p className="m-0 text-[13px] text-muted" data-testid="bot-sessions-panel-empty">{t('pages.membersPage.sessions_panel_empty')}</p>} />
    </div>
  )
}

function BotSessionView({ root, sessionKey, liveSlots, slotsLoaded, onBack, onReopened, onOpenInChat, onPaneFocusChange, panelHidden }: {
  root: string
  sessionKey: string
  liveSlots: readonly ChatSlot[]
  slotsLoaded: boolean
  onBack: () => void
  onReopened: (key: string) => void
  onOpenInChat: (key: string) => void
  onPaneFocusChange?: (focused: boolean) => void
  panelHidden: boolean
}) {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  const { listed, workers } = useMateSessionWorkers(root, liveSlots)
  const listFailed = listed.isError && !crewRouteNotServed(listed.error)
  const worker = workers.find(w => w.key === sessionKey)
  // Any open slot counts, not only the Bot's stamped ones: a session just
  // reopened is listed before the next slots frame carries its root stamp.
  const slot = useAppSelector(s => slotRowByKey(s.dashboard.slots, sessionKey))
  const [recapOpen, setRecapOpen] = useState(false)
  const [reopening, setReopening] = useState(false)
  const [reopenFailed, setReopenFailed] = useState(false)
  const title = sessionDisplayTitle({ key: sessionKey, title: worker?.title || slot?.title || '' })
  const group = worker?.group ?? (slot ? crewWorkerGroupOfRow(slot) : undefined)
  const remote = worker?.remote_crew
  // The view is keyed on the open session, so Back or opening another session
  // unmounts it: a reopen that answers after that changes nothing.
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  // The resume route, listed in the slot store the way the chat page's resume
  // lists it, but without `resumeFromHistory`: that thunk also makes the session
  // the chat page's active one, and nothing on this page may move that.
  const reopen = async () => {
    if (reopening) return
    setReopening(true)
    setReopenFailed(false)
    let opened = ''
    try {
      const title = worker?.title || ''
      const d = await api.resumeChatSlot(`dashboard_${sessionKey}`, title)
      if (d.ok) {
        opened = d.key || sessionKey
        dispatch(addSlotOptimistic({ key: opened, title: title || opened, messages: 0, running: false, mode: d.mode, surface: d.surface ?? d.mode, memory_mode: d.memory_mode }))
        dispatch(updateSlot({ key: opened, mode: d.mode, surface: d.surface ?? d.mode }))
      }
    } catch { /* said below */ }
    if (!mounted.current) return
    setReopening(false)
    if (opened) onReopened(opened)
    else setReopenFailed(true)
  }
  // Escape inside the open session is Back, unless an owner inside took the
  // press first (the composer stops a running turn or closes a picker). Read on
  // document bubble, after React's root-delegated handlers have run, so their
  // `preventDefault` is visible here; a press outside the view is not ours.
  const viewRef = useRef<HTMLDivElement>(null)
  const backRef = useRef(onBack)
  backRef.current = onBack
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      // A press that cancels an IME composition, silences read-back, or belongs
      // to an open dialog is not Back.
      if (e.isComposing || e.keyCode === 229 || escapeStoppedSpeech(e) || anotherEscapeOwnerIsOpen()) return
      if (!(e.target instanceof Node) || !viewRef.current?.contains(e.target)) return
      e.preventDefault()
      backRef.current()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])
  // "Run in terminal" from this session's code blocks answers here, not on the
  // page's bridge: the shell starts in the worker's own project and is bound to
  // the worker's slot and placement, never the Bot thread's.
  const queryClient = useQueryClient()
  const [actionError, setActionError] = useState('')
  const terminalRefs = useRunInTerminalRefs()
  // Ready only on the record a slots frame delivered (it carries the Bot's
  // root stamp and the project): a just-reopened session is listed first as an
  // optimistic row with no project, and a shell opened then would start in the
  // wrong directory.
  const framed = slotsLoaded ? liveSlots.find(s => s.key === sessionKey) : undefined
  const cwdRef = useRef<string | undefined>(framed?.project || undefined)
  cwdRef.current = framed?.project || undefined
  const readyRef = useRef(!!framed)
  readyRef.current = !!framed
  const runsHere = useSlotRunsHere(slot ? sessionKey : null)
  const runsHereRef = useRef(runsHere)
  runsHereRef.current = runsHere
  const slotRef = useRef<string | null>(sessionKey)
  slotRef.current = sessionKey
  const terminalScope = useRunInTerminalBridge({
    queryClient, showActionError: setActionError, cwdRef, readyRef, runsHereRef, slotRef, ...terminalRefs,
  })
  // This pane is the focused one while the person works in it, so a page-wide
  // composer lookup (a portalled Quote action, a shortcut, the dictation key)
  // lands in this session and not the Bot thread. Any press or focus outside the
  // view clears it, read in the capture phase so that the pane's own React
  // capture handler (which also sees its portalled pickers) can claim it back
  // for the same event.
  const [paneFocused, setPaneFocused] = useState(false)
  useEffect(() => {
    const release = (e: Event) => {
      if (!(e.target instanceof Node) || !viewRef.current?.contains(e.target)) setPaneFocused(false)
    }
    document.addEventListener('focusin', release, true)
    document.addEventListener('mousedown', release, true)
    return () => {
      document.removeEventListener('focusin', release, true)
      document.removeEventListener('mousedown', release, true)
    }
  }, [])
  const focusChangeRef = useRef(onPaneFocusChange)
  focusChangeRef.current = onPaneFocusChange
  // A hidden panel can keep focus inside it (the keyboard shortcut hides it
  // without moving focus), so the claim also drops while the panel is hidden.
  const claimsKeys = paneFocused && !panelHidden
  useEffect(() => { focusChangeRef.current?.(claimsKeys) }, [claimsKeys])
  useEffect(() => () => focusChangeRef.current?.(false), [])
  let body: ReactNode
  if (remote) {
    body = <p className="m-0 p-4 text-[13px] text-muted" data-testid="bot-session-remote">{t('pages.membersPage.sessions_panel_remote', { crew: remote })}</p>
  } else if (slot) {
    body = (
      <RunInTerminalScope.Provider value={terminalScope}>
        <ChatPane slotKey={sessionKey} frameless focused={claimsKeys || undefined} pushToTalk={claimsKeys}
          onFocus={() => setPaneFocused(true)} />
      </RunInTerminalScope.Provider>
    )
  } else if (!slotsLoaded) {
    body = <p className="m-0 p-4 text-[13px] text-muted">{t('pages.membersPage.recap_loading')}</p>
  } else {
    body = (
      <div className="p-4 flex flex-col items-start gap-2" data-testid="bot-session-closed">
        <p className="m-0 text-[13px] text-muted">{t('pages.membersPage.sessions_panel_closed')}</p>
        {/* aria-disabled, not disabled: focus stays on the button while it works. */}
        <button type="button" onClick={() => { void reopen() }} aria-disabled={reopening || undefined} aria-busy={reopening || undefined}
          data-testid="bot-session-reopen"
          className="min-h-9 inline-flex items-center rounded-lg border border-border bg-bg-elevated px-3 py-1.5 text-[12.5px] font-medium text-text hover:bg-bg-hover cursor-pointer aria-disabled:opacity-60 aria-disabled:cursor-default">
          {t('pages.membersPage.sessions_panel_reopen')}
        </button>
        {/* No hand-off: the Members page holds unsaved Profile, schedule and crew-editor drafts. */}
        {reopenFailed && <ErrorNotice variant="inline" message={t('commandCenter.worker_reopen_failed')} />}
      </div>
    )
  }
  return (
    <div ref={viewRef} role="group" aria-label={title} className="h-full min-h-0 flex flex-col" data-testid="bot-session-view">
      {/* Two rows, so a narrow panel or a long translation never squeezes the
          title out: the controls on top (they wrap when they must), the
          session's status and title under them. */}
      <div className="shrink-0 flex flex-col gap-1 px-3 py-2 border-b border-border">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <button type="button" onClick={onBack} data-testid="bot-session-back"
            className="inline-flex items-center gap-0.5 rounded-md px-1.5 py-1 text-[12.5px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer">
            <ChevronLeft size={14} aria-hidden="true" />
            {t('pages.membersPage.profile_sessions_tab')}
          </button>
          <span className="flex-1" aria-hidden="true" />
          <button type="button" onClick={() => setRecapOpen(o => !o)} aria-expanded={recapOpen} data-testid="bot-session-recap-toggle"
            className="inline-flex items-center gap-0.5 rounded-md px-1.5 py-1 text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer">
            {t('pages.membersPage.sessions_panel_recap')}
            <ChevronDown size={13} className={`transition-transform ${recapOpen ? 'rotate-180' : ''}`} aria-hidden="true" />
          </button>
          {/* A link, not a button: it leaves for the chat page, and the guarded
              handler asks about unsaved drafts first. */}
          {!remote && (
            <a href={`/chat?sid=${encodeURIComponent(sessionKey)}`} data-testid="bot-session-open-in-chat"
              onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); onOpenInChat(sessionKey) }}
              className="inline-flex items-center gap-1 px-1 py-1 text-[12px] text-muted hover:text-text hover:underline">
              {t('pages.membersPage.sessions_panel_open_in_chat')}
              <ExternalLink size={12} aria-hidden="true" />
            </a>
          )}
        </div>
        <div className="flex items-center gap-2 px-1.5 min-w-0">
          {group && <span className={`shrink-0 inline-block h-2 w-2 rounded-full bg-current ${CREW_GROUP_TONE[group]}`} aria-hidden="true" />}
          <span className="flex-1 min-w-0 truncate text-[13px] font-semibold" data-testid="bot-session-title">{title}</span>
        </div>
      </div>
      {/* No hand-off: the Members page holds unsaved Profile, schedule and crew-editor drafts. */}
      {listFailed && <div className="shrink-0 px-3 pt-2"><ErrorNotice variant="inline" message={t('pages.membersPage.sessions_read_failed')} /></div>}
      {actionError && <div className="shrink-0 px-3 pt-2"><ErrorNotice variant="inline" message={actionError} onDismiss={() => setActionError('')} /></div>}
      {recapOpen && (
        <div className="shrink-0 max-h-[40%] overflow-y-auto px-3.5 py-2.5 border-b border-border">
          <MateSessionRecap root={root} worker={sessionKey} rowTitle={title} />
        </div>
      )}
      <div className="flex-1 min-h-0 flex flex-col">{body}</div>
    </div>
  )
}
