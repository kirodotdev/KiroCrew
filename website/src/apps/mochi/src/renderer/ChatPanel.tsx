/**
 * Mochi - Chat panel (embedded below pet in expanded mode)
 * Uses three-channel model: chunks via chat:chunk, done via chat:done, messages via chat:message
 *
 * This module keeps the panel component (header, transcript, composer, slash
 * commands, the send-failure line and the context ring) and is the one import
 * path for the panel's parts, which live in sibling owners:
 * `ChatPanelPinnedFiles.tsx` (the pinned-files side panel), `ChatPanelMarkdown.tsx`
 * (message markdown) and `ChatPanelBubble.tsx` (one message bubble). Owners never
 * import this module.
 */
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  Eraser,
  LayoutDashboard,
  LoaderCircle,
  Palette,
  PawPrint,
  RotateCcw,
  Settings,
  Square,
  SquarePen,
  Trash2,
  Unplug,
  X,
} from 'lucide-react'
import Clickable from '../../../../components/Clickable'
import ErrorNotice from '../../../../components/ErrorNotice'
import { useImeGuard } from '../../../../hooks/useImeGuard'
import type { ChatMessage } from '../shared/types'
import { applyTheme, type ThemeId } from '../shared/themes'
import { formatShortcut } from '../shared/shortcut'
import { ContextMenu } from './ContextMenu'

import { api } from '../mochiApi'
import { DEFAULT_PET_NAME, resolvePetName } from '../../builtinPacks'
import {
  attachmentsFrom, composeMessage, cropToFile, filesFrom, ingestFiles,
  type PendingAttachment,
} from '../../panel/composerDrop'
import { PendingAttachments } from '../../panel/PendingAttachments'
import { reportStat, SendRefusedError, slotRefusalReason, isDefiniteRefusal } from '../../panel/panelBridge'
import type { ApprovalTarget } from '../../panel/panelBridge'
import { i18nT } from '../../../../i18n/t'
import { moodLabel, stateLabel } from '../../i18nKeys'
import { StreamingMarkdown } from './ChatPanelMarkdown'
import { Bubble, externalApprovalApproved } from './ChatPanelBubble'

/**
 * Slash-command descriptions, keyed by the command itself.
 *
 * One map instead of a list of `{ cmd, descKey }` pairs so the render site can index it
 * directly: `check-i18n-keys.mjs` cannot resolve a key read off a mapped element, and an
 * unresolvable key is one it cannot verify exists in the catalog. The command list is
 * derived from these keys so the two can never drift.
 */
const SLASH_DESC_KEY = {
  '/new': 'apps.mochi.slash.new',
  '/clear': 'apps.mochi.slash.clear',
  '/compact': 'apps.mochi.slash.compact',
  '/mcp': 'apps.mochi.slash.mcp',
  '/model': 'apps.mochi.slash.model',
  '/prompts': 'apps.mochi.slash.prompts',
  '/tools': 'apps.mochi.slash.tools',
  '/usage': 'apps.mochi.slash.usage',
  '/context': 'apps.mochi.slash.context',
  '/help': 'apps.mochi.slash.help',
} as const

const SLASH_COMMANDS: { cmd: keyof typeof SLASH_DESC_KEY }[] =
  (Object.keys(SLASH_DESC_KEY) as (keyof typeof SLASH_DESC_KEY)[]).map((cmd) => ({ cmd }))

// The panel's parts live in sibling owners (see the header); ChatApp, the
// capture harnesses and the specs keep importing them from this module.
export { PinnedSidePanel } from './ChatPanelPinnedFiles'
export { fixStreamingFences } from './ChatPanelMarkdown'
export { Bubble, externalApprovalApproved, parseApproval } from './ChatPanelBubble'

// ── Chat Panel ──────────────────────────────────────────────────────────────

interface ChatPanelProps {
  onToggleWatch?: () => void
  watchPanelVisible?: boolean
  onTogglePinned?: () => void
  pinnedPanelVisible?: boolean
  pinnedFileCount?: number
}

/**
 * The two fields of a `slots:update` entry this panel reads.
 *
 * Named locally rather than pulled from the bridge, which publishes the frame as
 * `unknown[]`: the panel cares only about finding the mochi slot and whether it is
 * running, and a wider claim would assert fields nothing here checks.
 */
interface SlotFrame {
  key?: string
  running?: boolean
}

export const ChatPanel: React.FC<ChatPanelProps> = ({ onToggleWatch, watchPanelVisible, onTogglePinned, pinnedPanelVisible, pinnedFileCount }) => {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [allHistory, setAllHistory] = useState<ChatMessage[]>([])
  const INITIAL_HISTORY = 10
  const LOAD_MORE_COUNT = 10
  const MAX_LOADED = 200 // safety cap — prevents DOM from growing unbounded
  const [input, setInput] = useState('')
  const [cmdIdx, setCmdIdx] = useState(0) // highlighted command in autocomplete
  const [editingTs, setEditingTs] = useState<string | null>(null)
  const [isAtBottom, setIsAtBottom] = useState(true)
  const [screenshot, setScreenshot] = useState<string | null>(null)
  const [ssDisplay, setSsDisplay] = useState<string | null>(null)
  // ADDED (not upstream): drag-and-drop / paste of a file or photo onto the box.
  // The ingest rules live in panel/composerDrop.ts so this file stays diffable
  // against the original.
  const [dropActive, setDropActive] = useState(false)
  const [dropError, setDropError] = useState('')
  // A FAILED send is kept apart from `dropError`: a dropped-file refusal is a
  // client-side validation hint, but a send failure is an error (a gateway
  // refusal or a dead connection), so it renders through `ErrorNotice` — the one
  // surface that recovers the request's structured context — rather than the
  // hand-written drop-hint line. See the `errors-use-error-notice` rule.
  const [sendError, setSendError] = useState('')
  // Queued attachments live HERE, not in the composer text: the reference
  // markdown is composed only at send time so the box the user types in is
  // never filled with plumbing.
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const [ssAnim, setSsAnim] = useState('')
  const ssRef = useRef<HTMLDivElement>(null)

  // Pinned files state removed — managed by ChatApp

  useEffect(() => {
    if (screenshot) {
      setSsDisplay(screenshot)
      setSsAnim('ssIn')
    } else if (ssDisplay) {
      setSsAnim('ssOut')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- drives the thumbnail animation off a `screenshot` transition only; `ssDisplay` is this effect's OWN output, read as the latest value at fire time purely to ask "is there still a thumbnail to animate out". Depending on it would re-run the effect on its own write and replay 'ssIn' over a running animation.
  }, [screenshot])

  const onSsAnimEnd = () => {
    if (ssAnim === 'ssOut') { setSsDisplay(null); setSsAnim('') }
  }
  const removeScreenshot = () => setScreenshot(null)

  // Lightbox state for image preview
  const openLightbox = (src: string) => { api?.openLightbox?.(src) }

  const [streaming, setStreaming] = useState('')
  const [isWaiting, setIsWaiting] = useState(false)
  const [slotRunning, setSlotRunning] = useState(false)
  /**
   * Is an agent turn in flight? Drives the paw indicator.
   *
   * Separate from `slotRunning` because slots:update is NOT a turn boundary. Right
   * after a send the backend pushes a slot update while the slot is still idle, so
   * keying the paw on `running` alone made it vanish the instant the user hit send
   * and not come back until the first chunk arrived — the user saw no paw at all
   * while waiting, then only the streaming cursor. This flag is raised on send and
   * lowered only at a REAL end of turn (a running -> not-running transition, or a
   * stop), so a paw is present for the whole turn.
   */
  const [turnActive, setTurnActive] = useState(false)
  /** Previous `running`, so only a true -> false transition ends the turn. */
  const wasRunningRef = useRef(false)
  const [isOnline, setIsOnline] = useState(false)
  const [showOffline, setShowOffline] = useState(false)
  useEffect(() => {
    if (!isOnline) {
      const t = setTimeout(() => setShowOffline(true), 1500)
      return () => clearTimeout(t)
    }
    setShowOffline(false)
    setCloudAutoConnecting(false)
    setGatewayStarting(false)
    setGatewayMsg('')
  }, [isOnline])
  // Only the setter is used upstream; the array itself is never read. It holds
  // nothing but the request id because that is all the three predicates below it
  // (dedup on arrival, clear on resolve) ever touch — the frames come off the
  // bridge as untyped records with the bridge's own shape (`tool`, `toolInput`,
  // ...), not an `ApprovalRequest`, so typing it as one would be a claim the
  // value contradicts.
  const [, setPendingApprovals] = useState<{ id: string }[]>([])
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null)
  const [showClearConfirm, setShowClearConfirm] = useState(false)
  const [showResetConfirm, setShowResetConfirm] = useState(false)
  const [petName, setPetName] = useState(DEFAULT_PET_NAME)
  const [shortcuts, setShortcuts] = useState({
    toggleWindow: formatShortcut('CommandOrControl+Shift+M'),
    screenCapture: formatShortcut('CommandOrControl+Shift+X'),
    hideAll: formatShortcut('CommandOrControl+Shift+H'),
  })
  const [petState, setPetState] = useState('offline')
  const [petMood, setPetMood] = useState('')
  const [contextPct, setContextPct] = useState<number | undefined>(undefined)
  const [isPeeking, setIsPeeking] = useState(false)
  const [gatewayStarting, setGatewayStarting] = useState(false)
  const [gatewayMsg, setGatewayMsg] = useState<string>('')
  // In cloud-desktop mode, assume connecting on startup until WS connects
  const [cloudAutoConnecting, setCloudAutoConnecting] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const wasNearBottomRef = useRef(true)
  const loadingMoreRef = useRef(false)
  const [cleared, setCleared] = useState(false) // suppresses auto-load after /clear
  const clearedRef = useRef(false)
  clearedRef.current = cleared
  const prevScrollStateRef = useRef<{ height: number; top: number } | null>(null)
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const allHistoryRef = useRef(allHistory)
  allHistoryRef.current = allHistory

  // Fix scroll position BEFORE paint when messages are prepended
  useLayoutEffect(() => {
    const snap = prevScrollStateRef.current
    const el = scrollerRef.current
    if (snap && el) {
      const delta = el.scrollHeight - snap.height
      if (delta > 0) {
        el.scrollTop = snap.top + delta
      }
      prevScrollStateRef.current = null
    }
  }, [messages])

  // Auto-load more when content doesn't fill the container
  const [loadTrigger, setLoadTrigger] = useState(0)
  useEffect(() => {
    if (cleared) return // suppressed after /clear
    const el = scrollerRef.current
    if (!el) return
    if (el.scrollHeight <= el.clientHeight && messages.length < allHistory.length && messages.length < MAX_LOADED && !loadingMoreRef.current) {
      loadingMoreRef.current = true
      prevScrollStateRef.current = { height: el.scrollHeight, top: el.scrollTop }
      const moreCount = Math.min(messages.length + LOAD_MORE_COUNT, allHistory.length, MAX_LOADED)
      setMessages(allHistory.slice(-moreCount))
      setTimeout(() => { loadingMoreRef.current = false; setLoadTrigger(c => c + 1) }, 150)
    }
  }, [messages, allHistory, loadTrigger, cleared])

  // IntersectionObserver — observe the "load earlier" indicator itself
  useEffect(() => {
    const sentinel = sentinelRef.current
    const scroller = scrollerRef.current
    if (!sentinel || !scroller) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !loadingMoreRef.current && !clearedRef.current) {
          const msgs = messagesRef.current
          const hist = allHistoryRef.current
          if (msgs.length >= hist.length || msgs.length >= MAX_LOADED) return
          loadingMoreRef.current = true
          // Snapshot scroll state BEFORE triggering re-render
          const el = scrollerRef.current
          if (el) {
            prevScrollStateRef.current = { height: el.scrollHeight, top: el.scrollTop }
          }
          const moreCount = Math.min(msgs.length + LOAD_MORE_COUNT, hist.length, MAX_LOADED)
          setMessages(hist.slice(-moreCount))
          setTimeout(() => { loadingMoreRef.current = false; setLoadTrigger(c => c + 1) }, 150)
        }
      },
      { root: scroller, threshold: 0 }
    )
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, []) // mount once — uses refs for latest state

  const inputRef = useRef<HTMLTextAreaElement>(null)
  // Enter sends here, so the guard has to own the key: it also covers WebKit, where
  // the keydown committing an IME candidate reports the native flag as false.
  const ime = useImeGuard()

  // Height of the bottom stack (banners + attachments + composer), measured

  // so the floating stop capsule and scroll pill always clear it.

  const composerRef = useRef<HTMLDivElement>(null)

  const [composerH, setComposerH] = useState(0)
  useEffect(() => {
    inputRef.current?.focus()
    const onFocus = () => inputRef.current?.focus()
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [])
  // Rotating placeholder tips
  const tips = React.useMemo(() => [
    i18nT('apps.mochi.chat.ask_placeholder', { name: petName }),
    i18nT('apps.mochi.tip.shortcut_toggle', { shortcut: shortcuts.toggleWindow }),
    i18nT('apps.mochi.tip.shortcut_screenshot', { shortcut: shortcuts.screenCapture }),
    i18nT('apps.mochi.tip.shortcut_hide', { shortcut: shortcuts.hideAll }),
    i18nT('apps.mochi.tip.shortcut_voice'),
    i18nT('apps.mochi.tip.watch_flight'),
    i18nT('apps.mochi.tip.drink_water'),
    i18nT('apps.mochi.tip.stretch'),
    i18nT('apps.mochi.tip.remind'),
    i18nT('apps.mochi.tip.calendar'),
    i18nT('apps.mochi.tip.watch_restock'),
    i18nT('apps.mochi.tip.weather'),
    i18nT('apps.mochi.tip.bedtime'),
    i18nT('apps.mochi.tip.watch_page'),
    i18nT('apps.mochi.tip.just_chat'),
    i18nT('apps.mochi.tip.drag_pet', { name: petName }),
    i18nT('apps.mochi.tip.silent_mode'),
    i18nT('apps.mochi.tip.edit_soul', { name: petName }),
    i18nT('apps.mochi.tip.learn_preference'),
    i18nT('apps.mochi.tip.multi_display', { name: petName }),
    i18nT('apps.mochi.tip.trust_mode'),
  ], [petName, shortcuts])
  const [tipIdx, setTipIdx] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setTipIdx(i => (i + 1) % tips.length), 10000)
    return () => clearInterval(timer)
  }, [tips.length])

  // Re-measure on every value change, not just on keystrokes: a height set
  // while the box was multi-line otherwise survived a programmatic clear (send,
  // /clear, edit-resend), leaving the box tall with nothing in it.
  useEffect(() => {
    if (inputRef.current) autoResize(inputRef.current)
  }, [input])

  useEffect(() => {
    const el = composerRef.current
    if (el === null || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setComposerH(el.offsetHeight))
    ro.observe(el)
    setComposerH(el.offsetHeight)
    return () => ro.disconnect()
  }, [])

  const autoResize = (el: HTMLTextAreaElement) => {
    // Empty box: DROP the inline height instead of measuring. scrollHeight of
    // an empty textarea includes the WRAPPED PLACEHOLDER, so measuring grew
    // the box to fit a long placeholder and it never shrank back (the stop
    // capsule then sat glued to the composer, since its offset assumes the
    // one-row height). Handled here so the onChange path agrees.
    if (el.value === '') {
      el.style.height = ''
      return
    }
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 120) + 'px'
  }

  // Theme
  useEffect(() => {
    api?.getMochiConfig?.().then((c) => {
      setPetName(resolvePetName(c))
      // `theme` is not part of PetConfig — the pet bridge types only the appearance
      // fields it reacts to — so it is read through a narrow view of the same
      // settings object rather than by widening that type.
      applyTheme((c as { theme?: string } | undefined)?.theme || 'mocha')
    })
    api?.getConfig?.().then((c) => {
      // Upstream read agentBackend.mode here to show a "connecting" banner while
      // it started a REMOTE gateway. The builtin is same-origin — the gateway is
      // already up if this page loaded at all.
      if (c?.shortcuts) {
        const fmt = formatShortcut
        setShortcuts({
          toggleWindow: fmt(c.shortcuts.toggleWindow || 'CommandOrControl+Shift+M'),
          screenCapture: fmt(c.shortcuts.screenCapture || 'CommandOrControl+Shift+X'),
          hideAll: fmt(c.shortcuts.hideAll || 'CommandOrControl+Shift+H'),
        })
      }
    })
    // Seed state AND mood: the mood was only ever learned from a live frame,
    // and moods self-clear after a few seconds, so a panel opened between two
    // changes showed no mood at all.
    api?.getPetStateInfo?.().then((info: { state: string; mood: string }) => {
      if (info?.state) setPetState(info.state)
      setPetMood(info?.mood && info.mood !== 'neutral' ? info.mood : '')
    })
    const off = api?.onStateChange?.((s: string) => setPetState(s))
    const offMood = api?.onMood?.((mood: string) => setPetMood(mood === 'neutral' ? '' : mood))
    const offPeek = api?.onPeeking?.((v: boolean) => setIsPeeking(v))
    const offConfig = api?.onConfigUpdated?.((m) => {
      setPetName(resolvePetName(m))
      if (m?.theme) applyTheme(m.theme as ThemeId)
      // Refresh shortcuts from full config (onConfigUpdated only sends mochi slice)
      api?.getConfig?.().then((c) => {
        if (c?.shortcuts) {
          const f = formatShortcut
          setShortcuts({
            toggleWindow: f(c.shortcuts.toggleWindow || 'CommandOrControl+Shift+M'),
            screenCapture: f(c.shortcuts.screenCapture || 'CommandOrControl+Shift+X'),
            hideAll: f(c.shortcuts.hideAll || 'CommandOrControl+Shift+H'),
          })
        }
      })
    })
    return () => { off?.(); offMood?.(); offPeek?.(); offConfig?.() }
  }, [])

  useEffect(() => {
    // Load history from KiroCrew slot
    api?.getChatHistory?.().then((history) => {
      if (Array.isArray(history)) {
        // The bridge answers untyped rows, so the shape is asserted per FIELD here
        // rather than trusted wholesale: the filter is what guarantees `role` is one
        // of the two this panel renders, and a row missing a timestamp gets 0.
        // The server row id (`meta.mid`) is kept so a patch frame for a row loaded
        // here still finds it (see the `_patch` branch below).
        const mapped = history
          .filter((m) => m.role === 'user' || m.role === 'assistant')
          .map((m, i): ChatMessage => {
            const mid = (m.meta as { mid?: unknown } | null | undefined)?.mid
            return {
              id: `hist-${i}`, role: m.role as ChatMessage['role'], content: m.content as string,
              timestamp: (m.timestamp as number) || 0,
              ...(typeof mid === 'string' && mid ? { meta: { mid } } : {}),
            }
          })
        setAllHistory(mapped)
        setMessages(mapped.slice(-INITIAL_HISTORY))
        wasNearBottomRef.current = true
        // Scroll to bottom after history renders
        const scrollToEnd = () => {
          const el = scrollerRef.current
          if (el) el.scrollTop = el.scrollHeight
        }
        setTimeout(scrollToEnd, 50)
        setTimeout(scrollToEnd, 150)
        setTimeout(scrollToEnd, 300)
      }
    })

    // Channel 1: streaming chunks — throttled to reduce re-renders
    let streamBuffer = ''
    let streamRaf: number | null = null
    const offChunk = api?.onChatChunk?.((content: string) => {
      setIsWaiting(false)
      streamBuffer += content
      if (!streamRaf) {
        streamRaf = requestAnimationFrame(() => {
          setStreaming((prev: string) => prev + streamBuffer)
          streamBuffer = ''
          streamRaf = null
        })
      }
    })

    // Channel 1: stream done — flush any remaining buffer and cancel pending RAF
    const offDone = api?.onChatDone?.(() => {
      if (streamRaf) { cancelAnimationFrame(streamRaf); streamRaf = null }
      if (streamBuffer) { setStreaming((prev: string) => prev + streamBuffer); streamBuffer = '' }
      // Clearing the turn indicators is normally the job of the slots frame's
      // running true->false TRANSITION, because a bare `!running` also describes
      // the idle pushes that happen mid-turn. But a transition needs the RISING
      // edge to have been observed, and slot pushes are batched before they are
      // flushed, so `running: true` and `running: false` can arrive coalesced
      // into one frame — after which `wasRunningRef` never went true and the
      // Stop button and waiting paws stayed up for the rest of the session even
      // though the turn had ended.
      //
      // The gateway pushes the slots update BEFORE `chat_done` at end of turn
      // (chat_runner: `slot.task = None; push_slots_update(); broadcast chat_done`),
      // so if the last frame we saw already says not-running, this is the end and
      // it is safe to clear. Still running means a multi-step turn continues, and
      // the transition path handles that.
      if (!wasRunningRef.current) {
        setTurnActive(false)
        setIsWaiting(false)
      }
      // Force scroll to bottom after message completes if user was following along
      if (wasNearBottomRef.current) {
        setTimeout(() => {
          scrollerRef.current?.scrollTo({ top: scrollerRef.current!.scrollHeight, behavior: 'smooth' })
        }, 50)
      }
    })

    // Complete messages (user + assistant)
    const offMsg = api?.onChatMessage?.((msg) => {
      // A retraction frame: the bridge is undoing an optimistic echo it added
      // (a send the gateway definitely refused). Remove that row by id from both
      // arrays — it carries no renderable role, so it falls through the normal
      // append path below.
      const retractId = (msg as { _retract?: unknown })._retract
      if (typeof retractId === 'string') {
        setMessages((prev: ChatMessage[]) => prev.filter(m => m.id !== retractId))
        setAllHistory((prev: ChatMessage[]) => prev.filter(m => m.id !== retractId))
        return
      }
      // A patch frame: new content for a committed row, named by the server
      // row id (`meta.mid`) the row carries, from its own frame or from the
      // history load. The gateway gives back the last run of text it held across
      // a tool call or approval prompt this way. Like a retraction it carries no
      // renderable role.
      const patchMid = (msg as { _patch?: unknown })._patch
      if (typeof patchMid === 'string') {
        const content = String(msg.content ?? '')
        const patch = (prev: ChatMessage[]) => prev.map(m =>
          (m as { meta?: { mid?: unknown } }).meta?.mid === patchMid ? { ...m, content } : m)
        setMessages(patch)
        setAllHistory(patch)
        return
      }
      if (msg.role) {
        // The frame IS a chat message; the bridge types it as an untyped record, so
        // the shape is claimed once here instead of at each read below.
        const withTs = { ...msg, timestamp: msg.timestamp || Date.now() } as ChatMessage
        // Cancel any pending streaming RAF and clear buffer to prevent stale chunks
        if (streamRaf) { cancelAnimationFrame(streamRaf); streamRaf = null }
        streamBuffer = ''
        // Clear streaming FIRST to avoid duplicate display (streaming footer + committed message)
        setStreaming('')
        setMessages((prev: ChatMessage[]) => [...prev, withTs])
        setAllHistory((prev: ChatMessage[]) => [...prev, withTs])
        // Message counters for the Memories view. Counted HERE because this is
        // the one place both directions land; `backfill` marks history replay,
        // which must not re-count messages already counted when they happened.
        // The OPTIMISTIC echo (`_optimistic`) is NOT counted here — it is
        // rendered before the gateway's receipt is known, and `sendMessage`
        // counts the send itself once the receipt is non-refused, so a refused
        // send never inflates the durable counter. The received direction still
        // counts here: an assistant frame only arrives when the turn ran.
        const optimistic = (msg as { _optimistic?: unknown })._optimistic === true
        if (!msg.backfill) {
          if (msg.role === 'user') { if (!optimistic) reportStat('message_sent') }
          else if (msg.role === 'assistant') reportStat('message_received')
        }
        if (msg.role === 'user' && !msg.backfill) {
          setIsWaiting(true)
          setTurnActive(true)
          // User just sent a message — scroll to bottom after Footer (waiting indicator) renders
          setTimeout(() => {
            scrollerRef.current?.scrollTo({ top: scrollerRef.current!.scrollHeight, behavior: 'smooth' })
          }, 50)
          // Second scroll after Footer has rendered with waiting indicator
          setTimeout(() => {
            scrollerRef.current?.scrollTo({ top: scrollerRef.current!.scrollHeight, behavior: 'smooth' })
          }, 150)
          wasNearBottomRef.current = true
        }
        if (msg.role === 'assistant') {
          // Don't clear isWaiting — slot may still be running (multi-step).
          // slots:update will handle the final state transition.
          // Scroll to bottom when assistant message completes if user was following
          if (wasNearBottomRef.current) {
            setTimeout(() => {
              scrollerRef.current?.scrollTo({ top: scrollerRef.current!.scrollHeight, behavior: 'smooth' })
            }, 50)
          }
        }
      }
    })

    const offStatus = api?.onBackendStatus?.((online: boolean) => setIsOnline(online))
    api?.getBackendStatus?.().then((online: boolean) => { if (online != null) setIsOnline(online) })
    const offSwitching = api?.onBackendSwitching?.((switching: boolean) => {
      setCloudAutoConnecting(switching)
      if (switching) setIsOnline(false)
    })
    const offSlots = api?.onSlotsUpdate?.((slots) => {
      // Track whether the mochi-pet slot is running
      const mochiSlot = (slots as SlotFrame[]).find((s) => s.key === 'mochi')
      const running = mochiSlot?.running ?? false
      setSlotRunning(running)
      // A TRANSITION out of running is the end of a turn. A bare `!running` is not:
      // the backend pushes slot updates while the slot is still idle (right after
      // the user's message lands, for one), and treating those as the end cleared
      // the waiting paw a moment after every send — which is why no paw appeared
      // until the first chunk arrived.
      if (wasRunningRef.current && !running) {
        setTurnActive(false)
        setIsWaiting(false)
        // Don't clear streaming here — let chat:done handle final flush
      }
      wasRunningRef.current = running
    })
    // A crop takes the SAME road as a dropped image: uploaded, then referenced by
    // path. The legacy `screenshot` slot kept the base64 in this window only —
    // it rode along as `meta.screenshot`, which nothing outside this panel
    // renders, so a screenshot looked sent here and was invisible in the
    // dashboard (and never reached the agent as an image). Going through
    // `ingestFiles` fixes all three at once, and lifts the one-image limit the
    // single slot imposed.
    const offCapture = api?.onCaptureDone?.((b64: string) => {
      void (async () => {
        const file = cropToFile(b64)
        const result = await ingestFiles([file])
        if (result.error !== undefined) {
          setDropError(result.error)
          // Fall back to the local slot rather than losing the capture outright:
          // the user still sees it, even if only here.
          setScreenshot(b64)
          return
        }
        const added = attachmentsFrom(result)
        if (added.length === 0) {
          setScreenshot(b64)
          return
        }
        setAttachments((prev) => [...prev, ...added])
      })()
    })
    const offApproval = api?.onApprovalRequest?.((req) => {
      // Dedup by request_id: the same pending approval can arrive more than once
      // (a re-broadcast, or a permission frame replayed alongside history), and a
      // duplicate would add a second card with a colliding React key.
      const reqId = String(req.id)
      const msgId = `approval-${reqId}`
      setPendingApprovals(prev => prev.some(a => a.id === reqId) ? prev : [...prev, { id: reqId }])
      setMessages(prev => prev.some(m => m.id === msgId) ? prev : [...prev, {
        id: msgId,
        role: 'assistant',
        content: `__approval__${JSON.stringify(req)}`,
        timestamp: Date.now(),
      }])
    })
    const offApprovalResolved = api?.onApprovalResolvedExternal?.((data?: Record<string, unknown>) => {
      // Approval was handled elsewhere (dashboard, Slack, another surface). Carry
      // the REAL verdict — a reject resolved externally must not read "Approved".
      const approved = externalApprovalApproved(data)
      const verdict = approved
        ? i18nT('apps.mochi.approval.approved')
        : i18nT('apps.mochi.approval.rejected')
      const rid = (data as { id?: unknown } | undefined)?.id
      if (rid !== undefined && rid !== null) {
        // Frame identifies which request resolved — relabel only that card.
        const key = String(rid)
        setPendingApprovals(prev => prev.filter(a => a.id !== key))
        setMessages(prev => prev.map(m =>
          m.id === `approval-${key}` ? { ...m, content: verdict } : m
        ))
      } else {
        // No id on the frame — clear all pending, but with the real verdict.
        setPendingApprovals([])
        setMessages(prev => prev.map(m =>
          m.content.startsWith('__approval__') ? { ...m, content: verdict } : m
        ))
      }
    })

    const offTheme = api?.onThemeChanged?.((themeId: string) => { applyTheme(themeId) })

    const offCtx = api?.onContextUsage?.((pct: number) => setContextPct(pct))

    return () => {
      if (streamRaf) cancelAnimationFrame(streamRaf)
      offChunk?.(); offDone?.(); offMsg?.(); offStatus?.(); offSlots?.(); offSwitching?.(); offCapture?.(); offApproval?.(); offApprovalResolved?.(); offTheme?.(); offCtx?.()
    }
  }, [])

  // Auto-scroll during streaming
  useEffect(() => {
    if (streaming && wasNearBottomRef.current) {
      const raf = requestAnimationFrame(() => {
        const el = scrollerRef.current
        if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
      })
      return () => cancelAnimationFrame(raf)
    }
  }, [streaming])

  // Auto-scroll when waiting indicator appears in Footer
  useEffect(() => {
    if (isWaiting && wasNearBottomRef.current) {
      const raf = requestAnimationFrame(() => {
        const el = scrollerRef.current
        if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
      })
      return () => cancelAnimationFrame(raf)
    }
  }, [isWaiting])

  const screenshotRef = useRef(screenshot)
  screenshotRef.current = screenshot

  const sendText = useCallback(async (text: string) => {
    if (!text && !screenshotRef.current) return
    setIsWaiting(true)
    setTurnActive(true)
    setSendError('')
    try {
      await api?.sendMessage?.(text, screenshotRef.current || undefined)
      setScreenshot(null)
    } catch (err) {
      // The send did not go through. handleSend already cleared the composer
      // before awaiting, so without this the typed text is lost, no error shows,
      // and the spinner sticks forever. Restore the text (composer is empty on
      // this path), clear the stuck waiting state, and surface the failure.
      setIsWaiting(false)
      setTurnActive(false)
      setInput((prev) => (prev ? prev : text))
      // A REFUSAL carries the gateway's own reason (a busy-slot or agent-mismatch
      // 409, or ensureSlot's binding refusal): show that reason the way the
      // dashboard does, so the user looks at the slot and not at their
      // connection. A rejected fetch (offline, DNS) carries no gateway reason, so
      // it keeps the connection copy — which is what that failure actually is.
      setSendError(sendFailureMessage(err))
    }
  }, [])

  const handleApproval = useCallback(async (
    id: string,
    action: string,
    pattern?: string,
    trustGrantable = false,
    target?: ApprovalTarget,
  ) => {
    const res = target
      ? await api?.respondApproval?.(id, action, pattern, trustGrantable, target)
      : await api?.respondApproval?.(id, action, pattern, trustGrantable)
    if (res !== undefined && res !== null && res.ok === false) {
      // The POST failed. Do NOT relabel the card "Approved"/"Trusted" — that
      // would claim a security decision that never reached the agent, which is
      // still blocked. Leave the card (and its buttons) in place so the user can
      // retry, and surface the failure instead of lying about the outcome.
      setMessages(prev => [...prev, {
        id: `approval-error-${id}-${Date.now()}`,
        role: 'assistant',
        // A stale pre-owner session is an AUTH failure, not a network one:
        // the generic "check your connection" copy would send the user
        // debugging the wrong thing, so name the real remedy (sign in again).
        content: res.staleOwnerSession
          ? i18nT('api.client.stale_owner_session_sign_in_again')
          : i18nT('apps.mochi.chat.send_failed'),
        timestamp: Date.now(),
      }])
      return
    }
    setPendingApprovals(prev => prev.filter(a => a.id !== id))
    setMessages(prev => prev.map(m => m.id === `approval-${id}`
      ? { ...m, content: action === 'reject' ? i18nT('apps.mochi.approval.rejected') : action.startsWith('trust') ? i18nT('apps.mochi.approval.trusted') : i18nT('apps.mochi.approval.approved') }
      : m))
  }, [])

  const openLightboxCb = useCallback((src: string) => { api?.openLightbox?.(src) }, [])

  /** Drop / paste ingest. Images fill the pending image slot; other files are
   *  uploaded and referenced in the text. */
  const ingestDropped = useCallback(async (files: File[]) => {
    setDropError('')
    const result = await ingestFiles(files)
    if (result.images.length > 0 || result.files.length > 0) {
      // Referenced by PATH rather than stuffed into the single `screenshot` slot,
      // which is what limited the fork to one image. Core's ACP client inlines
      // every image path it finds, so the count is unbounded.
      setAttachments((prev) => [...prev, ...attachmentsFrom(result)])
      // The box grows when references are appended; keep the caret visible.
    }
    if (result.error !== undefined) setDropError(result.error)
  }, [])

  const handleSend = async () => {
    const typed = input.trim()
    if (!typed && !screenshot && attachments.length === 0) return
    // Clear any prior refusal banner on EVERY send attempt, not just the plain
    // `sendText` path: the edit-resend and slash-command branches below return
    // without touching it, so a red ErrorNotice from an earlier refused send
    // otherwise stayed pinned above the composer while the new turn streamed in.
    setSendError('')
    // Attachment references are appended HERE, not kept in the composer.
    const text = composeMessage(input, attachments)
    setInput('')
    setAttachments([])
    // Re-measure rather than only clearing the inline height: an explicit height
    // set while the box was multi-line otherwise survived the clear, so the box
    // stayed tall after sending.
    // Height is re-measured by the effect that watches `input`.

    // Edit-resend: if we have a pending edit timestamp, use the edit-resend API
    if (editingTs && !text.startsWith('/')) {
      const editTsStr = editingTs
      setEditingTs(null)
      setIsWaiting(true)
      // Remove messages from the edited point onward in local state. This is
      // display-only until the next mount re-reads the gateway, which rebuilds
      // the authoritative transcript either way — so a refusal does NOT restore
      // the slice: a fallback send that was actually delivered (an `unknown`
      // transport outcome) must keep its post-send state, and re-adding the
      // edited-from rows there would duplicate a turn the gateway already ran
      // (GPT F3). The composer draft, which the gateway never saw on a definite
      // refusal, is the only thing worth handing back.
      const editTsNum = parseInt(editTsStr)
      setMessages(prev => {
        const idx = prev.findIndex(m => m.role === 'user' && m.timestamp === editTsNum)
        return idx >= 0 ? prev.slice(0, idx) : prev
      })
      setAllHistory(prev => {
        const idx = prev.findIndex(m => m.role === 'user' && m.timestamp === editTsNum)
        return idx >= 0 ? prev.slice(0, idx) : prev
      })
      setTimeout(() => {
        scrollerRef.current?.scrollTo({ top: scrollerRef.current!.scrollHeight, behavior: 'smooth' })
      }, 50)
      wasNearBottomRef.current = true
      const result = await api?.editResend?.(text, editTsStr)
      if (!result?.ok) {
        // Fallback: send as a normal message. `sendMessage` echoes the turn
        // itself (and retracts that echo on a definite refusal), so nothing is
        // added locally here. A definite refusal REJECTS this call (a
        // rebound/busy slot, a 403); the composer was already cleared above, so
        // without a catch the typed text is lost and the spinner sticks. Mirror
        // `sendText`'s recovery: hand the draft back and show the gateway reason.
        // A transport failure (fetch rejected) also lands here, but there the
        // draft is deliberately NOT restored (see the catch) — the send may have
        // run, and re-filling the composer would invite a double-executed turn.
        setIsWaiting(true)
        try {
          await api?.sendMessage?.(text, screenshot || undefined)
        } catch (err) {
          setIsWaiting(false)
          setTurnActive(false)
          // Restore the draft ONLY for a DEFINITE refusal (nothing was sent):
          // `isDefiniteRefusal` is true for ANY `SendRefusedError`/tagged slot
          // refusal — including a non-2xx whose body is not JSON (an aiohttp 500
          // or proxy 502 page), where the reason is `undefined`. The earlier
          // `slotRefusalReason(err) !== undefined` test wrongly dropped the
          // draft on exactly that ordinary failure (a reason-less definite
          // refusal) because it cannot tell a reason-less refusal from a
          // transport error. A transport rejection is NOT a definite refusal:
          // it keeps its optimistic echo (the text stays visible in the
          // transcript), so re-filling the composer would invite a re-send that
          // executes an already-run, side-effecting turn a SECOND time — leave
          // it cleared there.
          if (isDefiniteRefusal(err)) {
            setInput((prev) => (prev ? prev : text))
          }
          setSendError(sendFailureMessage(err))
        }
      }
      return
    }
    // Clear edit mode for slash commands
    setEditingTs(null)

    // /new — start a fresh session
    if (text === '/new') {
      const sysMsg = (content: string) => {
        const msg: ChatMessage = { id: `sys-${Date.now()}`, role: 'assistant', content, timestamp: Date.now() }
        setMessages(prev => [...prev, msg])
        setAllHistory(prev => [...prev, msg])
      }
      sysMsg(i18nT('apps.mochi.newSession.starting'))
      try {
        await api?.newSession?.()
        setContextPct(0) // Reset context usage — new session starts fresh
        // Add a separator — keep history visible
        const doneMsg: ChatMessage = { id: `sys-${Date.now()}`, role: 'assistant', content: i18nT('apps.mochi.newSession.done'), timestamp: Date.now() }
        setMessages(prev => [...prev, doneMsg])
        setAllHistory(prev => [...prev, doneMsg])
      } catch {
        sysMsg(i18nT('apps.mochi.newSession.error'))
      }
      return
    }

    // /clear — visual clear only, history preserved
    if (text === '/clear') {
      setMessages([])
      setCleared(true)
      return
    }

    sendText(text || i18nT('apps.mochi.chat.what_is_this'))
  }

  // Float the capsule/pill a fixed gap above the measured bottom stack; the
  // fallback covers the first paint before the observer reports.
  //
  // Upstream's fixed `bottom: 52` left only ~10px over its composer, which reads
  // as the capsule sitting ON the text box. Measuring the stack (so the gap
  // survives a grown textarea or an attachment strip) plus a deliberately
  // visible gap is the fix; this is a considered divergence from upstream, not
  // an accident.
  const FLOAT_GAP = 18
  const floatBottom = (composerH || 44) + FLOAT_GAP

  return (
    <div
      // Layout shell, not a widget: the click handler only dismisses the context
      // menu, so it is explicitly presentational rather than something a screen
      // reader should announce as actionable.
      role="presentation"
      style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, height: '100vh', position: 'relative' }}
      onContextMenu={(e) => {
        if (window.getSelection()?.toString()) return
        e.preventDefault(); setCtxMenu({ x: e.clientX, y: e.clientY })
      }}
      onClick={() => { if (ctxMenu) { setCtxMenu(null) } }}
    >
      {/* Header — draggable */}
      <div style={{
        padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 6,
        borderBottom: '1px solid var(--border)', fontSize: 12,
        background: 'var(--bg)',
        WebkitAppRegion: 'drag', cursor: 'grab',
      } as React.CSSProperties}>
        <span style={{ fontWeight: 600, color: 'var(--text)' }}>{petName}</span>
        <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> · {stateLabel(isPeeking && (petState === 'idle' || petState === 'thinking') ? (petState === 'thinking' ? 'peekThinking' : 'peeking') : petState)}{petMood ? ` · ${moodLabel(petMood)}` : ''}</span>
        <span style={{
          marginLeft: 'auto', width: 6, height: 6, borderRadius: '50%',
          background: isOnline ? 'var(--success)' : 'var(--danger)',
        }} />
        {contextPct !== undefined && <ContextRing pct={contextPct} />}
        <button onClick={() => onTogglePinned?.()} title={i18nT('apps.mochi.chatPanel.pinned_files')} aria-label={i18nT('apps.mochi.chatPanel.pinned_files')} style={{
          background: pinnedPanelVisible ? 'var(--accent)' : 'none',
          border: pinnedPanelVisible ? 'none' : '1px solid var(--border)',
          color: pinnedPanelVisible ? 'var(--accent-text)' : 'var(--text-muted)',
          fontSize: 10, fontWeight: 600, letterSpacing: 0.3,
          borderRadius: 4, padding: '1px 6px',
          cursor: 'pointer', WebkitAppRegion: 'no-drag',
          transition: 'all 0.15s',
          opacity: (pinnedFileCount ?? 0) > 0 ? 1 : 0.4,
        } as React.CSSProperties}>{i18nT('apps.mochi.chatPanel.pins')}</button>
        <button onClick={() => onToggleWatch?.()} title={i18nT('apps.mochi.watchPanel.title')} aria-label={i18nT('apps.mochi.watchPanel.title')} style={{
          background: watchPanelVisible ? 'var(--accent)' : 'none',
          border: watchPanelVisible ? 'none' : '1px solid var(--border)',
          color: watchPanelVisible ? 'var(--accent-text)' : 'var(--text-muted)',
          fontSize: 10, fontWeight: 600, letterSpacing: 0.3,
          borderRadius: 4, padding: '1px 6px',
          cursor: 'pointer', WebkitAppRegion: 'no-drag',
          transition: 'all 0.15s',
        } as React.CSSProperties}>{i18nT('apps.mochi.chatPanel.watchlist')}</button>
        <button onClick={() => api?.closeChat?.()} aria-label={i18nT('apps.mochi.watchPanel.close')} style={{
          background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: 14,
          cursor: 'pointer', padding: '0 2px', WebkitAppRegion: 'no-drag',
          display: 'inline-flex', alignItems: 'center',
        } as React.CSSProperties}><X size={14} /></button>
      </div>

      {/* Disconnected banner */}
      {showOffline && (
        <div style={{
          padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 8,
          background: cloudAutoConnecting ? 'rgba(34, 197, 94, 0.1)' : 'rgba(255, 107, 107, 0.1)',
          borderBottom: `1px solid ${cloudAutoConnecting ? 'rgba(34, 197, 94, 0.2)' : 'rgba(255, 107, 107, 0.2)'}`,
          fontSize: 12, color: 'var(--text-muted)',
        }}>
          <span>
            {cloudAutoConnecting
              ? <><LoaderCircle size={11} style={{ display: 'inline', verticalAlign: '-1px', marginRight: 4 }} />{i18nT('apps.mochi.gateway.starting')}</>
              : <><Unplug size={11} style={{ display: 'inline', verticalAlign: '-1px', marginRight: 4 }} />{i18nT('apps.mochi.gateway.disconnected')}</>}
          </span>
          {!cloudAutoConnecting && !gatewayStarting && (
            <button onClick={async () => {
              setGatewayStarting(true)
              setGatewayMsg('')
              try {
                const res = await api?.retryConnect?.()
                if (res?.ok) {
                  setGatewayMsg(i18nT('apps.mochi.gateway.starting'))
                  // Give WS time to connect before clearing the starting state
                  setTimeout(() => setGatewayStarting(false), 8000)
                } else {
                  setGatewayMsg(res?.message || i18nT('apps.mochi.gateway.timeout'))
                  setGatewayStarting(false)
                }
              } catch {
                setGatewayMsg(i18nT('apps.mochi.gateway.timeout'))
                setGatewayStarting(false)
              }
            }} style={{
              marginLeft: 'auto', background: 'var(--accent)', border: 'none', borderRadius: 6,
              padding: '3px 10px', color: '#fff', fontSize: 11, fontWeight: 600,
              cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0,
            }}>{i18nT('apps.mochi.gateway.start_btn')}</button>
          )}
          {gatewayStarting && (
            <span style={{ marginLeft: 'auto', fontSize: 11, opacity: 0.7 }}>{i18nT('apps.mochi.gateway.starting_btn')}</span>
          )}
        </div>
      )}
      {showOffline && gatewayMsg && !gatewayStarting && (
        <div style={{
          padding: '4px 12px', fontSize: 11, color: 'var(--text-muted)',
          background: 'rgba(255, 107, 107, 0.05)',
          borderBottom: '1px solid rgba(255, 107, 107, 0.1)',
        }}>{gatewayMsg}</div>
      )}

      {/* Reset confirmation dialog */}
      {showResetConfirm && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 1000,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <div style={{
            background: 'var(--bg-elevated)', borderRadius: 10, padding: '16px 20px', width: 240,
            border: '1px solid var(--border)', boxShadow: '0 8px 24px var(--shadow)',
          }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 8 }}>{i18nT('apps.mochi.reset.title', { name: petName })}</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 16, lineHeight: 1.4 }}>
              {i18nT('apps.mochi.reset.desc', { name: petName })}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => setShowResetConfirm(false)} style={{
                background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6,
                padding: '5px 12px', color: 'var(--text)', fontSize: 12, cursor: 'pointer',
              }}>{i18nT('apps.mochi.reset.cancel')}</button>
              <button disabled={actionLoading} onClick={async () => {
                setActionLoading(true)
                try { await api?.resetMochi?.(); setMessages([]); setAllHistory([]) } finally { setActionLoading(false); setShowResetConfirm(false) }
              }} style={{
                background: 'var(--danger)', border: 'none', borderRadius: 6,
                padding: '5px 12px', color: '#fff', fontSize: 12, fontWeight: 600,
                cursor: actionLoading ? 'wait' : 'pointer', opacity: actionLoading ? 0.6 : 1,
              }}>{actionLoading ? '...' : i18nT('apps.mochi.reset.confirm')}</button>
            </div>
          </div>
        </div>
      )}

      {/* Context menu */}
      {ctxMenu && (
        <ContextMenu
          x={ctxMenu.x} y={ctxMenu.y}
          /* SUBTRACTED four rows, each of which rendered but did nothing:
             editSoul (the soul concept was removed — each avatar carries its own
             persona), doctor (all eight of upstream's checks verified that a
             SELF-INSTALLING standalone app had installed itself; as a builtin the
             app manager owns that and `kirocrew doctor` covers the host), quit
             (KiroCrew owns the app lifecycle; the pet is disabled from the App
             Store). resetMochi is BACK and implemented — see mochiApi.resetMochi
             and the /reset route. */
          items={[
            { label: i18nT('apps.mochi.menu.settings'), action: 'settings', icon: Settings },
            { label: i18nT('apps.mochi.menu.gallery'), action: 'gallery', icon: Palette },
            { label: i18nT('apps.mochi.menu.dashboard'), action: 'dashboard', icon: LayoutDashboard },
            { separator: true },
            { label: i18nT('apps.mochi.menu.clear_screen'), action: 'clearScreen', icon: Eraser },
            { label: i18nT('apps.mochi.menu.delete_history'), action: 'deleteHistory', danger: true, icon: Trash2 },
            { label: i18nT('apps.mochi.menu.reset_mochi', { name: petName }), action: 'resetMochi', danger: true, icon: RotateCcw },
          ]}
          onAction={async (action) => {
            switch (action) {
              case 'clearScreen': setMessages([]); setCleared(true); break
              case 'deleteHistory': setShowClearConfirm(true); break
              case 'resetMochi': setShowResetConfirm(true); break
              case 'settings': api?.openSettings?.(); break
              case 'gallery': api?.galleryOpen?.(); break
              case 'dashboard': api?.openDashboard?.(); break
            }
          }}
          onClose={() => setCtxMenu(null)}
        />
      )}

      {/* Clear chat confirmation */}
      {showClearConfirm && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 1000,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <div style={{
            background: 'var(--bg-elevated)', borderRadius: 10, padding: '16px 20px', width: 240,
            border: '1px solid var(--border)', boxShadow: '0 8px 24px var(--shadow)',
          }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 8 }}>{i18nT('apps.mochi.deleteHistory.title')}</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 16, lineHeight: 1.4 }}>
              {i18nT('apps.mochi.deleteHistory.desc', { name: petName })}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => setShowClearConfirm(false)} style={{
                background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6,
                padding: '5px 12px', color: 'var(--text)', fontSize: 12, cursor: 'pointer',
              }}>{i18nT('apps.mochi.clear.cancel')}</button>
              <button disabled={actionLoading} onClick={async () => {
                setActionLoading(true)
                try { await api?.deleteHistory?.(); setMessages([]); setAllHistory([]) } finally { setActionLoading(false); setShowClearConfirm(false) }
              }} style={{
                background: 'var(--danger)', border: 'none', borderRadius: 6,
                padding: '5px 12px', color: '#fff', fontSize: 12, fontWeight: 600,
                cursor: actionLoading ? 'wait' : 'pointer', opacity: actionLoading ? 0.6 : 1,
              }}>{actionLoading ? '...' : i18nT('apps.mochi.deleteHistory.confirm')}</button>
            </div>
          </div>
        </div>
      )}


      {/* Messages — plain flow layout with overflow-anchor for scroll stability */}
      <div
        ref={scrollerRef}
        className="chat-scroll"
        style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}
        onScroll={() => {
          const el = scrollerRef.current
          if (!el) return
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 150
          setIsAtBottom(atBottom)
          wasNearBottomRef.current = atBottom
        }}
      >
        {/* Load more indicator — also serves as IntersectionObserver sentinel */}
        {/* Single stable sentinel — never unmounted, observer always watches same node */}
        <div ref={sentinelRef} style={{ display: 'flex', justifyContent: 'center', padding: allHistory.length > messages.length && messages.length < MAX_LOADED ? '8px 10px' : 0 }}>
          {allHistory.length > messages.length && messages.length < MAX_LOADED ? (
            cleared ? (
              <button onClick={() => {
                setCleared(false)
                const el = scrollerRef.current
                if (el) prevScrollStateRef.current = { height: el.scrollHeight, top: el.scrollTop }
                const moreCount = Math.min(messages.length + LOAD_MORE_COUNT, allHistory.length, MAX_LOADED)
                setMessages(allHistory.slice(-moreCount))
              }} style={{
                background: 'var(--bg-input)', border: '1px solid var(--border)',
                borderRadius: 12, padding: '4px 14px', color: 'var(--text-muted)', fontSize: 11,
                cursor: 'pointer',
              }}>{i18nT('apps.mochi.chat.load_earlier')}</button>
            ) : (
              <span style={{ fontSize: 11, color: 'var(--text-muted)', opacity: 0.6 }}>
                {i18nT('apps.mochi.chat.load_earlier')}…
              </span>
            )
          ) : null}
        </div>

        {/* Message items — normal flow. An assistant row committed with no text
            yet is not drawn: all of its text is a run the gateway holds across
            the tool call or approval prompt after it, and it arrives as a patch
            (the dashboard hides the same row). */}
        {messages.filter((msg) => msg.role !== 'assistant' || msg.content).map((msg) => (
          <div key={msg.id} style={{ padding: '3px 10px', display: 'flex', flexDirection: 'column' }}>
            <Bubble message={msg} onOption={sendText} onImageClick={openLightboxCb}
              animate={!msg.id.startsWith('hist-')}
              onApproval={handleApproval}
              onEdit={(content) => {
                api?.stopGeneration?.() // no-op when idle, avoids stale closure issue
                setIsWaiting(false)
                setTurnActive(false)
                setStreaming('')
                setInput(content)
                setEditingTs(msg.timestamp ? String(msg.timestamp) : null)
                setTimeout(() => {
                  if (inputRef.current) {
                    inputRef.current.focus()
                    inputRef.current.style.height = 'auto'
                    inputRef.current.style.height = Math.min(inputRef.current.scrollHeight, 120) + 'px'
                  }
                }, 50)
              }} />
          </div>
        ))}

        {/* Footer: streaming + waiting indicator */}
        <div style={{ padding: '0 10px 6px', display: 'flex', flexDirection: 'column' }}>
          {streaming && (
            <div style={{ alignSelf: 'flex-start', maxWidth: '85%' }}>
              <div style={{ position: 'relative' }}>
                <div style={{
                  padding: '6px 10px',
                  borderRadius: '10px 10px 10px 2px',
                  background: 'var(--bubble-assistant)',
                }}>
                  <div style={{ wordBreak: 'break-word', fontSize: 12, lineHeight: 1.4, overflow: 'hidden' }}>
                    <StreamingMarkdown content={streaming} />
                  </div>
                </div>
              </div>
            </div>
          )}
          {(turnActive || isWaiting || slotRunning) && !streaming && (
            <div style={{ ...bStyle('assistant'), opacity: 0.6, display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ display: 'inline-flex', gap: 3, alignItems: 'center' }}>
                {[0, 1, 2].map((i) => (
                  <span key={i} style={{ animation: `pawBounce 1.4s infinite ${i * 0.2}s`, display: 'inline-flex' }}><PawPrint size={13} /></span>
                ))}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Stop generation capsule — always visible while slot is running */}
      {(turnActive || slotRunning || isWaiting || streaming) && (
        <div style={{ position: 'absolute', bottom: floatBottom, left: 0, right: 0, display: 'flex', justifyContent: 'center', zIndex: 11, pointerEvents: 'none' }}>
          <button onClick={async () => {
            await api?.stopGeneration?.()
            setIsWaiting(false)
            setTurnActive(false)
            setStreaming('')
          }} style={{
            background: 'var(--danger)', border: 'none',
            borderRadius: 14, padding: '4px 10px', fontSize: 10, fontWeight: 600,
            color: '#fff', cursor: 'pointer', pointerEvents: 'auto',
            display: 'flex', alignItems: 'center', gap: 4,
            boxShadow: '0 2px 8px var(--shadow)',
            transition: 'opacity 0.15s',
          }}
            onMouseEnter={(e) => (e.currentTarget.style.opacity = '0.8')}
            onMouseLeave={(e) => (e.currentTarget.style.opacity = '1')}
          >
            <Square size={8} fill="currentColor" strokeWidth={0} />
            {i18nT('apps.mochi.chat.stop')}
          </button>
        </div>
      )}

      {/* Scroll to bottom pill */}
      {!isAtBottom && !turnActive && !slotRunning && !isWaiting && !streaming && (
        <div style={{ position: 'absolute', bottom: floatBottom, left: 0, right: 0, display: 'flex', justifyContent: 'center', zIndex: 10, pointerEvents: 'none', transition: 'bottom 0.15s' }}>
          <button onClick={() => {
            scrollerRef.current?.scrollTo({ top: scrollerRef.current!.scrollHeight, behavior: 'smooth' })
          }} style={{
            background: 'var(--accent)', border: 'none',
            borderRadius: 14, padding: '4px 12px', fontSize: 11, color: 'var(--accent-text)',
            cursor: 'pointer', boxShadow: '0 2px 8px var(--shadow)',
            display: 'flex', alignItems: 'center', gap: 4,
            animation: 'pillIn 0.2s ease-out', pointerEvents: 'auto',
            transition: 'opacity 0.15s', fontWeight: 500,
          }}
            onMouseEnter={(e) => (e.currentTarget.style.opacity = '0.85')}
            onMouseLeave={(e) => (e.currentTarget.style.opacity = '1')}
          >
            {i18nT('apps.mochi.chat.scroll_bottom')}
          </button>
        </div>
      )}

      {/* The whole bottom stack in ONE measured container. The stop capsule and
          the scroll pill float above it, and a fixed offset cannot hold: the
          drop-error banner, attachment strip, screenshot preview, edit banner
          and command autocomplete all add rows, and the textarea itself grows to
          120px. Measuring the stack is what makes the gap real in every state
          (upstream's fixed 52 was tuned for its own single layout). */}
      <div ref={composerRef}>
      {/* A FAILED send renders through the shared ErrorNotice. This cross-origin
          panel receives ONLY custom-property CSS (installCoreThemeVars strips
          every Tailwind utility, and the panel entry never imports index.css),
          so the component's `block` variant — whose banner box is pure Tailwind
          (`rounded-lg border bg-danger/10 px-3 py-2`) — renders as unstyled
          debris here (a lone triangle, a text line, a raw browser-chrome ✕).
          Follow the SettingsPanel pattern instead: use `variant="inline"` (whose
          layout is text-only, nothing to lose when utilities drop) inside an
          inline-styled wrapper that supplies the alert's banner surface —
          danger color, tinted background, border, padding, size — via
          `var(--*)` so it does not depend on Tailwind at all. The inline
          variant's dismiss ✕ carries `bg-transparent border-none p-0`, also
          stripped here, so a scoped inline `<style>` resets it to a flat icon
          button rather than the UA's grey-chrome box. No agent hand-off
          (`askAgent={false}`): the hand-off unmounts this tree and would destroy
          the restored draft the user is about to retry. */}
      {sendError !== '' && (
        <div
          className="mochi-send-error-banner"
          role="presentation"
          style={{
            display: 'flex',
            alignItems: 'center',
            margin: '6px 10px',
            padding: '6px 10px',
            borderRadius: 8,
            border: '1px solid var(--danger, #e5484d)',
            background: 'color-mix(in srgb, var(--danger, #e5484d) 12%, transparent)',
            color: 'var(--danger, #e5484d)',
            fontSize: 13,
          }}
        >
          <style>{`.mochi-send-error-banner button{background:transparent;border:none;padding:0;cursor:pointer;color:inherit;display:inline-flex;align-items:center;}`}</style>
          <ErrorNotice
            variant="inline"
            message={sendError}
            askAgent={false}
            onDismiss={() => setSendError('')}
          />
        </div>
      )}
      {/* ADDED (not upstream): why a dropped file was refused. Reporting it is
          the point — the fork discarded such files silently, which reads as the
          app being broken rather than the file being unsupported. A refused drop
          is a client-side validation hint, not a failure, so it is NOT an
          `ErrorNotice` (see the `errors-use-error-notice` rule's own carve-out
          for validation hints). */}
      {dropError !== '' && (
        <div style={{
          padding: '4px 10px', borderTop: '1px solid var(--border)',
          fontSize: 11, color: 'var(--danger)', display: 'flex', gap: 6,
        }}>
          <span style={{ flex: 1 }}>{dropError}</span>
          <button onClick={() => setDropError('')} aria-label={i18nT('apps.mochi.chatPanel.dismiss')} style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            cursor: 'pointer', fontSize: 12, padding: 0,
          }}><X size={12} /></button>
        </div>
      )}

      {/* ADDED (not upstream): thumbnails for the pending drop/paste
          attachments (state-backed; references are composed at send time)
          what will be sent. */}
      <PendingAttachments
        items={attachments}
        onRemove={(path) => setAttachments((prev) => prev.filter((a) => a.path !== path))}
      />

      {/* Screenshot preview */}
      {ssDisplay && (
        <div ref={ssRef} onAnimationEnd={onSsAnimEnd} style={{
          padding: '4px 10px', borderTop: '1px solid var(--border)',
          animation: `${ssAnim} 0.2s ease forwards`,
        }}>
          {/* Opening the crop full size is a CONTROL, so it must be tab-reachable and
              fire on Enter/Space — and it belongs on this CONTAINER rather than on the
              <img>. An image has no interactive role it may take, and the dismiss dot
              is absolutely positioned against this box, so it cannot move inside a
              wrapper button either. `Clickable` supplies role=button, tabIndex and the
              key handling, and its keydown guard already ignores keys arriving from the
              nested dismiss <button>. The hover listeners stay where they were: they
              only fade that dot in and out. */}
          <Clickable
            onClick={() => openLightbox(ssDisplay)}
            aria-label={i18nT('apps.mochi.chatPanel.open_image')}
            style={{ position: 'relative', width: 40, height: 40, display: 'inline-block', cursor: 'zoom-in' }}
            onMouseEnter={(e) => { const b = e.currentTarget.querySelector('.x-btn') as HTMLElement; if (b) b.style.opacity = '1' }}
            onMouseLeave={(e) => { const b = e.currentTarget.querySelector('.x-btn') as HTMLElement; if (b) b.style.opacity = '0' }}
          >
            {/* Empty alt because the container carries the accessible name: describing
                the thumbnail again here would announce one control twice. */}
            <img
              src={`data:image/png;base64,${ssDisplay}`}
              alt=""
              style={{ width: 40, height: 40, objectFit: 'cover', borderRadius: 4, border: '1px solid var(--border)', cursor: 'zoom-in' }}
            />
            {/* Dismiss must not also open the lightbox now that the container is the
                control the click would otherwise reach. */}
            <button className="x-btn" onClick={(e) => { e.stopPropagation(); removeScreenshot() }} aria-label={i18nT('apps.mochi.chatPanel.remove_screenshot')} style={{
              position: 'absolute', top: -4, right: -4, width: 16, height: 16,
              background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '50%',
              color: 'var(--text-muted)', fontSize: 10, lineHeight: '14px', textAlign: 'center',
              cursor: 'pointer', opacity: 0, transition: 'opacity 0.15s', padding: 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}><X size={10} /></button>
          </Clickable>
        </div>
      )}
      <style>{`
        @keyframes ssIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes ssOut { from { opacity: 1; transform: translateY(0); } to { opacity: 0; transform: translateY(8px); } }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
        .approval-in { animation: ssIn 0.2s ease; }
        .approval-out { animation: ssOut 0.2s ease forwards; }
        @keyframes lbIn { from { opacity: 0; transform: scale(0.85); } to { opacity: 1; transform: scale(1); } }
        @keyframes lbOut { from { opacity: 1; transform: scale(1); } to { opacity: 0; transform: scale(0.85); } }
        @keyframes msgIn { from { opacity: 0; transform: translateY(6px) scale(0.97); } to { opacity: 1; transform: translateY(0) scale(1); } }
        @keyframes pawBounce { 0%,60%,100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-4px) scale(1.1); opacity: 1; } }
        @keyframes pillIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0; } }
        .msg-bubble:hover .copy-md-btn { opacity: 1 !important; }
        .msg-bubble .copy-md-btn { transition-delay: 0s; }
        .msg-bubble:not(:hover) .copy-md-btn { transition-delay: 0.3s; }
      `}</style>

      {/* Edit mode banner */}
      {editingTs && (
        <div style={{
          padding: '5px 12px', borderTop: '1px solid var(--border)',
          background: 'rgba(79, 195, 247, 0.08)',
          display: 'flex', alignItems: 'center', gap: 6,
          animation: 'msgIn 0.15s ease',
        }}>
          <SquarePen size={12} color="var(--accent)" />
          <span style={{ fontSize: 11, color: 'var(--accent)', fontWeight: 500, flex: 1 }}>
            {i18nT('apps.mochi.chat.edit_mode')}
          </span>
          <button onClick={() => { setEditingTs(null); setInput('') }} style={{
            background: 'none', border: 'none', color: 'var(--danger)',
            fontSize: 11, cursor: 'pointer', padding: '2px 6px', borderRadius: 4,
            fontWeight: 500, transition: 'opacity 0.15s',
          }}
            onMouseEnter={(e) => (e.currentTarget.style.opacity = '0.7')}
            onMouseLeave={(e) => (e.currentTarget.style.opacity = '1')}
          >{i18nT('apps.mochi.chat.edit_cancel')}</button>
        </div>
      )}

      {/* Command autocomplete */}
      {input.startsWith('/') && (() => {
        const filtered = SLASH_COMMANDS.filter(c => c.cmd.startsWith(input))
        if (filtered.length === 0 || input === filtered[0]?.cmd) return null
        return (
          <div style={{
            padding: '4px 10px', borderTop: '1px solid var(--border)',
            background: 'var(--bg-elevated, var(--bg))',
            animation: 'msgIn 0.12s ease',
          }}>
            {filtered.map((c, i) => (
              <Clickable key={c.cmd} onClick={() => { setInput(c.cmd); setCmdIdx(0); inputRef.current?.focus() }}
                style={{
                  padding: '4px 8px', borderRadius: 6, cursor: 'pointer', fontSize: 12,
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  background: i === cmdIdx ? 'var(--bg-input)' : 'transparent',
                }}
                onMouseEnter={() => setCmdIdx(i)}
              >
                <code style={{ color: 'var(--accent)', fontSize: 11 }}>{c.cmd}</code>
                <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>{i18nT(SLASH_DESC_KEY[c.cmd])}</span>
              </Clickable>
            ))}
          </div>
        )
      })()}

      {/* Input */}
      <div style={{
        padding: '6px 10px', borderTop: '1px solid var(--border)',
        display: 'flex', gap: 4, alignItems: 'center',
      }}>
        {/* Screenshot button - hidden until polished
        <button onClick={() => api?.startCapture?.()} title={i18nT('apps.mochi.chatPanel.screenshot')} aria-label={i18nT('apps.mochi.chatPanel.screenshot')} style={{
          background: 'none', border: 'none', fontSize: 16, cursor: 'pointer',
          padding: 0, lineHeight: 1, flexShrink: 0,
          transition: 'transform 0.15s ease',
        }}
          onMouseEnter={(e) => (e.currentTarget.style.transform = 'scale(1.15)')}
          onMouseLeave={(e) => (e.currentTarget.style.transform = 'scale(1)')}
        ><img src={cameraIcon} style={{ width: 18, height: 18 }} /></button>
        */}

        <textarea
          ref={inputRef}
          {...ime.bindComposition<HTMLTextAreaElement>({
            onFocus: (e) => {
              e.currentTarget.style.borderColor = editingTs ? 'var(--accent)' : 'var(--border-focus)'
              e.currentTarget.style.boxShadow = '0 0 0 3px var(--accent-glow)'
            },
            onBlur: (e) => {
              e.currentTarget.style.borderColor = editingTs ? 'var(--accent)' : 'var(--border)'
              e.currentTarget.style.boxShadow = editingTs ? '0 0 0 2px var(--accent-glow)' : 'none'
            },
          })}
          value={input} onChange={(e) => { setInput(e.target.value); setCmdIdx(0); autoResize(e.currentTarget) }}
          onKeyDown={(e) => {
            // Command autocomplete navigation
            if (input.startsWith('/')) {
              const filtered = SLASH_COMMANDS.filter(c => c.cmd.startsWith(input))
              const showAutocomplete = filtered.length > 0 && input !== filtered[0]?.cmd
              if (showAutocomplete) {
                if (e.key === 'ArrowDown') { e.preventDefault(); setCmdIdx(i => (i + 1) % filtered.length); return }
                if (e.key === 'ArrowUp') { e.preventDefault(); setCmdIdx(i => (i - 1 + filtered.length) % filtered.length); return }
                if (e.key === 'Tab') {
                  // Tab accepts the highlighted command INTO the composer, so an
                  // IME cycling its candidate list with Tab must not rewrite the
                  // draft mid-composition. Claimed like the Enter branch below.
                  if (!ime.claimKey(e)) return
                  e.preventDefault(); setInput(filtered[cmdIdx].cmd); setCmdIdx(0); return
                }
                // While the picker is open Enter belongs to it, so the key is claimed
                // here either way and never falls through to the send branch below.
                if (e.key === 'Enter' && !e.shiftKey) {
                  if (ime.claimEnter(e)) { setInput(filtered[cmdIdx].cmd); setCmdIdx(0) }
                  return
                }
              }
            }
            if (e.key === 'Enter' && !e.shiftKey) { if (ime.claimEnter(e)) handleSend() }
          }}
          onPaste={(e) => {
            const files = filesFrom(e.clipboardData)
            if (files.length === 0) return // ordinary text paste
            e.preventDefault()
            void ingestDropped(files)
          }}
          onDragEnter={(e) => { e.preventDefault(); setDropActive(true) }}
          onDragOver={(e) => { e.preventDefault(); setDropActive(true) }}
          onDragLeave={() => setDropActive(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDropActive(false)
            void ingestDropped(filesFrom(e.dataTransfer))
          }}
          placeholder={tips[tipIdx]}
          rows={1}
          style={{
            flex: 1, background: 'var(--bg-input)',
            border: dropActive ? '1px dashed var(--accent)'
              : editingTs ? '1px solid var(--accent)' : '1px solid var(--border)',
            borderRadius: 8, padding: '5px 8px', color: 'var(--text)', fontSize: 12,
            resize: 'none', outline: 'none', fontFamily: 'inherit',
            transition: 'border-color 0.2s, box-shadow 0.2s',
            lineHeight: '1.4',
            maxHeight: 120, overflowY: 'auto',
            boxShadow: editingTs ? '0 0 0 2px var(--accent-glow)' : undefined,
          }}
        />
        <button onClick={handleSend} title={i18nT('apps.mochi.chat.send')} aria-label={i18nT('apps.mochi.chat.send')} style={{
          background: 'var(--accent)', border: 'none', borderRadius: '50%',
          width: 28, height: 28, flexShrink: 0,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--accent-text)', fontSize: 15, cursor: 'pointer',
          transition: 'transform 0.15s ease',
        }}
          onMouseDown={(e) => (e.currentTarget.style.transform = 'scale(0.88)')}
          onMouseUp={(e) => (e.currentTarget.style.transform = 'scale(1)')}
          onMouseLeave={(e) => (e.currentTarget.style.transform = 'scale(1)')}
        ><PawPrint size={16} /></button>
      </div>
      </div>
    </div>
  )
}

/**
 * The composer error line for a send that did not go through.
 *
 * A gateway REFUSAL (a `SendRefusedError` on a non-2xx, or `ensureSlot`'s tagged
 * binding refusal) names the real reason the gateway returned — a busy slot, a
 * slot/agent mismatch — and the panel shows it with `pages.chatPage.send_failed_with_error`, the same
 * core-owned framed string the dashboard uses, so the user looks at the slot and
 * not at their connection. A refusal that carries no reason falls back to the
 * unframed `pages.chatPage.send_failed`.
 *
 * A rejected fetch (offline, DNS) throws a plain `TypeError` with no gateway
 * reason, so it keeps `apps.mochi.chat.send_failed` — the connection copy, which
 * is what that failure is.
 *
 * The `i18nT` keys are literals here, not built from the branch, because
 * `check-i18n-keys.mjs` resolves only file-scope literals and a computed key is
 * one it cannot verify.
 */
function sendFailureMessage(err: unknown): string {
  const reason = refusalReason(err)
  if (reason === undefined) return i18nT('apps.mochi.chat.send_failed')
  return reason === ''
    ? i18nT('pages.chatPage.send_failed')
    : i18nT('pages.chatPage.send_failed_with_error', { error: reason })
}

/**
 * The gateway's explanation for a refused send, or `undefined` when the failure
 * is not a refusal and should read as a connection problem.
 *
 * `slotRefusalReason` recognizes exactly two shapes: a `SendRefusedError` (the
 * gateway's own `error` body on a non-2xx) and a tagged slot-binding refusal
 * from `ensureSlot`. For a `SendRefusedError` that named no reason it returns the
 * empty string — the panel then shows the unframed `pages.chatPage.send_failed`.
 * Any OTHER throw — a rejected fetch, or an unexpected internal error — returns
 * `undefined` so the panel keeps the localized connection copy rather than
 * promoting a raw, unlocalized `Error.message` into UI text for every language.
 */
function refusalReason(err: unknown): string | undefined {
  if (err instanceof SendRefusedError) return err.reason ?? ''
  return slotRefusalReason(err)
}

function bStyle(role: string): React.CSSProperties {
  return {
    maxWidth: '85%', padding: '6px 10px', borderRadius: 10,
    alignSelf: role === 'user' ? 'flex-end' : 'flex-start',
    background: role === 'user' ? 'var(--bubble-user)' : 'var(--bubble-assistant)',
  }
}


/** Compact circular context-usage indicator for the chat header. */
function ContextRing({ pct }: { pct: number }) {
  const p = Math.round(Math.min(pct, 100))
  const fill = p >= 70 ? 'var(--danger)' : p >= 50 ? 'var(--warning, orange)' : 'var(--text-muted)'
  // Drawn with a conic-gradient ring rather than an inline svg: this is a
  // chart, so lucide has no equivalent, and CSS renders the identical ring.
  // Divergence: the old stroke-dashoffset 500ms ease is gone -- conic-gradient
  // stops do not transition without @property registration, which is not worth
  // a paint-time feature gate for a 20px indicator.
  const ringMask = 'radial-gradient(farthest-side, transparent calc(100% - 2.5px), #000 calc(100% - 2px))'
  return (
    <div title={i18nT('apps.mochi.chatPanel.context_pct', { pct: p })} style={{
      position: 'relative', width: 20, height: 20, flexShrink: 0,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{
        position: 'absolute', inset: 1, borderRadius: '50%',
        background: `conic-gradient(${fill} ${p * 3.6}deg, color-mix(in srgb, currentColor 15%, transparent) 0)`,
        WebkitMask: ringMask, mask: ringMask,
      }} />
      {p > 0 && (
        <span style={{ color: fill, fontSize: 7, fontFamily: 'monospace', fontWeight: 700, lineHeight: 1 }}>{p}</span>
      )}
    </div>
  )
}
