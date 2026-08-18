/**
 * Mochi - Chat panel (embedded below pet in expanded mode)
 * Uses three-channel model: chunks via chat:chunk, done via chat:done, messages via chat:message
 */
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  Ban,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Eraser,
  Eye,
  File,
  Folder,
  Handshake,
  LayoutDashboard,
  LoaderCircle,
  Palette,
  PawPrint,
  Pin,
  RotateCcw,
  Settings,
  Shield,
  ShieldCheck,
  ShieldPlus,
  Square,
  SquarePen,
  Trash2,
  Unplug,
  Wrench,
  X,
} from 'lucide-react'
import Clickable from '../../../../components/Clickable'
import { familyGrantIsDistinct, trustBasePattern, truncateCommandLabel } from '../shared/trustPatterns'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { rehypeSanitize, remarkVerbatimUnknownTags } from '../../../../components/MarkdownRenderer'
import { mdImageDestToPath } from '../../../../utils/fileTokens'
import { classifyPlatform } from '../../../../hooks/useGatewayPlatform'
import type { ApprovalRequest, ChatMessage } from '../shared/types'
import { applyTheme, type ThemeId } from '../shared/themes'
import { PINNED_PANEL_WIDTH } from '../shared/constants'
import { formatShortcut } from '../shared/shortcut'
import { ContextMenu } from './ContextMenu'
import { WidgetFrame, parseWidgets, hasWidgets } from './WidgetFrame'

import { api } from '../mochiApi'
import { DEFAULT_PET_NAME, resolvePetName } from '../../builtinPacks'
import {
  attachmentsFrom, composeMessage, cropToFile, filesFrom, ingestFiles,
  type PendingAttachment,
} from '../../panel/composerDrop'
import { PendingAttachments } from '../../panel/PendingAttachments'
import { MochiCodeBlock } from '../../panel/MochiCodeBlock'
import { reportStat } from '../../panel/panelBridge'
import { i18nT } from '../../../../i18n/t'
import { i18next } from '../../../../i18n'
import { electronPlatform, isElectron } from '../../../../lib/electron'
import { moodLabel, stateLabel } from '../../i18nKeys'

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

// ── Pinned Files Side Panel ──────────────────────────────────────────────────

/** Local re-declaration of PinnedFileEntry (matches src/main/pinnedFilesService.ts) */
interface PinnedFileEntry {
  path: string
  label: string
  pinnedAt: number
  updatedAt?: number
}

interface PinnedSidePanelProps {
  pins: PinnedFileEntry[]
  updatedPaths: Set<string>
  deletedPaths: Set<string>
  visible: boolean
  lang?: string
  onMarkSeen?: (path: string) => void
  /** ADDED (not upstream): the empty hint names the PET, which is renameable. */
  petName?: string
}

/** Vertical side panel showing pinned file chips — renders on the right side of chat. */
export const PinnedSidePanel: React.FC<PinnedSidePanelProps> = ({ pins, updatedPaths, deletedPaths, visible, onMarkSeen, petName }) => {
  if (!visible) return null

  return (
    <div className="pin-panel-root" style={{
      width: PINNED_PANEL_WIDTH,
      minWidth: PINNED_PANEL_WIDTH,
      height: '100vh',
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column',
      flexShrink: 0,
      background: 'var(--bg)',
      borderLeft: '1px solid var(--border)',
    }}>
      <style>{`
        .pin-panel-root { animation: pin-panel-enter 0.2s ease-out both; }
        @keyframes pin-panel-enter { from { opacity: 0; transform: translateX(-8px); } to { opacity: 1; transform: translateX(0); } }
        @keyframes pulse-dot {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.6; transform: scale(0.85); }
        }
      `}</style>
      <div style={{
        flex: 1,
        overflowY: 'auto',
        overflowX: 'hidden',
        padding: '8px 6px',
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
      }}>
        {pins.length === 0 ? (
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', flex: 1, padding: '16px 8px', textAlign: 'center',
            opacity: 0.5,
          }}>
            <Pin size={24} strokeWidth={1.5} style={{ marginBottom: 8, color: 'rgba(255,255,255,0.4)' }} />
            <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)', lineHeight: 1.4 }}>
              {i18nT('apps.mochi.pinned.empty_hint', { name: petName || DEFAULT_PET_NAME })}
            </span>
          </div>
        ) : (() => {
          // Group pins by full parent path (use full path as key to avoid collisions)
          const folderMap = new Map<string, PinnedFileEntry[]>()
          for (const pin of pins) {
            const parts = pin.path.split('/')
            parts.pop() // remove filename
            const fullParent = parts.join('/') || '/'
            if (!folderMap.has(fullParent)) folderMap.set(fullParent, [])
            folderMap.get(fullParent)!.push(pin)
          }
          const groups: { folder: string; fullPath: string; items: PinnedFileEntry[] }[] = []
          for (const [fullPath, items] of folderMap) {
            const folder = fullPath.split('/').pop() || '/'
            groups.push({ folder, fullPath, items })
          }
          return groups.map((group) => (
            <div key={group.fullPath} style={{ marginBottom: 4 }}>
              <div style={{
                fontSize: 10, fontWeight: 600, color: 'rgba(255,255,255,0.35)',
                padding: '4px 8px 2px', textTransform: 'uppercase', letterSpacing: '0.03em',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }} title={group.fullPath}>
                {group.folder}
              </div>
              {group.items.map((pin) => (
                <PinnedChip
                  key={pin.path}
                  pin={pin}
                  isUpdated={updatedPaths.has(pin.path)}
                  isDeleted={deletedPaths.has(pin.path)}

                  onMarkSeen={onMarkSeen}
                />
              ))}
            </div>
          ))
        })()}
      </div>
    </div>
  )
}

/** File extension to color mapping for visual distinction */
const extColors: Record<string, string> = {
  ts: '#3b82f6', tsx: '#3b82f6', js: '#eab308', jsx: '#eab308',
  py: '#22c55e', md: '#a78bfa', json: '#f97316', yaml: '#f97316', yml: '#f97316',
  css: '#ec4899', html: '#f43f5e', sh: '#6ee7b7', toml: '#fb923c',
}

function getExtColor(filePath: string): string {
  const ext = filePath.split('.').pop()?.toLowerCase() || ''
  return extColors[ext] || 'var(--accent)'
}

/** Individual pinned file chip — horizontal: icon left, filename right. */
const PinnedChip: React.FC<{
  pin: PinnedFileEntry
  isUpdated: boolean
  isDeleted: boolean
  lang?: string
  onMarkSeen?: (path: string) => void
}> = ({ pin, isUpdated, isDeleted, onMarkSeen }) => {
  const [hovered, setHovered] = useState(false)
  const displayName = pin.label || pin.path.split('/').pop() || pin.path
  const extColor = getExtColor(pin.path)

  const handleClick = () => {
    api?.markPinnedSeen?.(pin.path)
    // Preview is a shell-bridge capability; in a browser tab the click can
    // only mark the pin seen, not open the OS previewer.
    if (isElectron) api?.previewFile?.(pin.path)
    onMarkSeen?.(pin.path)
  }

  const handleDismiss = (e: React.MouseEvent) => {
    e.stopPropagation()
    api?.unpinFile?.(pin.path)
  }

  // The row is a click target only while the click has a visible payoff:
  // opening the OS previewer (shell only), or clearing an unseen-update dot
  // (HTTP-backed, works everywhere). Otherwise it renders inert — same
  // treatment as the inline file chip — so a browser tab never shows a
  // live-looking control that silently does nothing. Hover still reveals the
  // unpin button, which is its own control and works everywhere.
  const clickable = isElectron || (isUpdated && !isDeleted)

  const chipStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    padding: '5px 8px',
    borderRadius: 8,
    cursor: clickable ? 'pointer' : 'default',
    background: clickable && hovered ? 'rgba(255,255,255,0.07)' : 'transparent',
    transition: 'background 0.15s ease',
    position: 'relative',
    opacity: isDeleted ? 0.35 : 1,
    flexShrink: 0,
  }

  const body = (
    <>
      {/* Colored file icon */}
      <File size={14} color={isDeleted ? 'var(--text-muted)' : extColor} style={{ flexShrink: 0 }} />

      {/* Filename */}
      <span style={{
        fontSize: 11,
        fontWeight: 400,
        color: isDeleted ? 'var(--text-muted)' : 'rgba(255,255,255,0.8)',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        flex: 1,
        minWidth: 0,
        lineHeight: 1.3,
      }}>
        {displayName}
      </span>

      {/* Update indicator — pulsing green dot */}
      {isUpdated && !isDeleted && (
        <span style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: '#34d399',
          boxShadow: '0 0 4px rgba(52,211,153,0.6)',
          animation: 'pulse-dot 2s ease-in-out infinite',
          flexShrink: 0,
        }} />
      )}

      {/* Dismiss button — appears on hover, macOS red dot style */}
      {hovered && !isDeleted && (
        <button
          onClick={handleDismiss}
          title={i18nT('apps.mochi.pinned.unpin')}
          aria-label={i18nT('apps.mochi.pinned.unpin')}
          style={{
            width: 14,
            height: 14,
            borderRadius: '50%',
            background: 'rgba(239,68,68,0.85)',
            border: 'none',
            color: '#fff',
            fontSize: 8,
            lineHeight: '14px',
            textAlign: 'center',
            cursor: 'pointer',
            padding: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <X size={12} />
        </button>
      )}
    </>
  )

  if (!clickable) {
    return (
      <div
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        title={pin.path}
        style={chipStyle}
      >
        {body}
      </div>
    )
  }

  return (
    <Clickable
      onClick={handleClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title={pin.path}
      style={chipStyle}
    >
      {body}
    </Clickable>
  )
}

// ── Chat Panel ──────────────────────────────────────────────────────────────

interface ChatPanelProps {
  onToggleWatch?: () => void
  watchPanelVisible?: boolean
  onTogglePinned?: () => void
  pinnedPanelVisible?: boolean
  pinnedFileCount?: number
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
  // Only the setter is used upstream; the array itself is never read.
  const [, setPendingApprovals] = useState<ApprovalRequest[]>([])
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
    api?.getMochiConfig?.().then((c: any) => {
      setPetName(resolvePetName(c))
      applyTheme((c?.theme as ThemeId) || 'mocha')
    })
    api?.getConfig?.().then((c: any) => {
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
    const offConfig = api?.onConfigUpdated?.((m: any) => {
      setPetName(resolvePetName(m))
      if (m?.theme) applyTheme(m.theme as ThemeId)
      // Refresh shortcuts from full config (onConfigUpdated only sends mochi slice)
      api?.getConfig?.().then((c: any) => {
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
    api?.getChatHistory?.().then((history: any) => {
      if (Array.isArray(history)) {
        const mapped = history
          .filter((m: any) => m.role === 'user' || m.role === 'assistant')
          .map((m: any, i: number) => ({
            id: `hist-${i}`, role: m.role, content: m.content,
            timestamp: m.timestamp || 0,
          }))
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
    const offMsg = api?.onChatMessage?.((msg: any) => {
      if (msg.role) {
        const withTs = { ...msg, timestamp: msg.timestamp || Date.now() }
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
        if (!msg.backfill) {
          if (msg.role === 'user') reportStat('message_sent')
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
    const offSlots = api?.onSlotsUpdate?.((slots: any[]) => {
      // Track whether the mochi-pet slot is running
      const mochiSlot = slots.find((s: any) => s.key === 'mochi')
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
    const offApproval = api?.onApprovalRequest?.((req: any) => {
      // Dedup by request_id: the same pending approval can arrive more than once
      // (a re-broadcast, or a permission frame replayed alongside history), and a
      // duplicate would add a second card with a colliding React key.
      const msgId = `approval-${req.id}`
      setPendingApprovals(prev => prev.some(a => a.id === req.id) ? prev : [...prev, req])
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

    const offTheme = api?.onThemeChanged?.((themeId: string) => { applyTheme(themeId as any) })

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
    try {
      await api?.sendMessage?.(text, screenshotRef.current || undefined)
      setScreenshot(null)
    } catch {
      // Send failed. handleSend already cleared the composer before awaiting, so
      // without this the typed text is lost, no error shows, and the spinner
      // sticks forever. Restore the text (composer is empty on this path), clear
      // the stuck waiting state, and surface the failure via the existing
      // error banner — the dashboard AddWatchForm "your input is still here,
      // try again" recovery.
      setIsWaiting(false)
      setTurnActive(false)
      setInput((prev) => (prev ? prev : text))
      setDropError(i18nT('apps.mochi.chat.send_failed'))
    }
  }, [])

  const handleApproval = useCallback(async (id: string, action: string, pattern?: string) => {
    const res = await api?.respondApproval?.(id, action, pattern)
    if (res !== undefined && res !== null && res.ok === false) {
      // The POST failed. Do NOT relabel the card "Approved"/"Trusted" — that
      // would claim a security decision that never reached the agent, which is
      // still blocked. Leave the card (and its buttons) in place so the user can
      // retry, and surface the failure instead of lying about the outcome.
      setMessages(prev => [...prev, {
        id: `approval-error-${id}-${Date.now()}`,
        role: 'assistant',
        content: i18nT('apps.mochi.chat.send_failed'),
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
      // Remove messages from the edited point onward in local state
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
        // Fallback: send as normal message — don't add user msg locally,
        // sendMessage will trigger chat:message event which adds it
        setIsWaiting(true)
        await api?.sendMessage?.(text, screenshot || undefined)
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

        {/* Message items — normal flow */}
        {messages.map((msg) => (
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
      {/* ADDED (not upstream): why a dropped file was refused. Reporting it is
          the point — the fork discarded such files silently, which reads as the
          app being broken rather than the file being unsupported. */}
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
          <div style={{ position: 'relative', width: 40, height: 40, display: 'inline-block' }}
            onMouseEnter={(e) => { const b = e.currentTarget.querySelector('.x-btn') as HTMLElement; if (b) b.style.opacity = '1' }}
            onMouseLeave={(e) => { const b = e.currentTarget.querySelector('.x-btn') as HTMLElement; if (b) b.style.opacity = '0' }}
          >
            <img
              src={`data:image/png;base64,${ssDisplay}`}
              onClick={() => openLightbox(ssDisplay)}
              style={{ width: 40, height: 40, objectFit: 'cover', borderRadius: 4, border: '1px solid var(--border)', cursor: 'zoom-in' }}
            />
            <button className="x-btn" onClick={removeScreenshot} aria-label={i18nT('apps.mochi.chatPanel.remove_screenshot')} style={{
              position: 'absolute', top: -4, right: -4, width: 16, height: 16,
              background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '50%',
              color: 'var(--text-muted)', fontSize: 10, lineHeight: '14px', textAlign: 'center',
              cursor: 'pointer', opacity: 0, transition: 'opacity 0.15s', padding: 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}><X size={10} /></button>
          </div>
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
          value={input} onChange={(e) => { setInput(e.target.value); setCmdIdx(0); autoResize(e.currentTarget) }}
          onKeyDown={(e) => {
            // Command autocomplete navigation
            if (input.startsWith('/')) {
              const filtered = SLASH_COMMANDS.filter(c => c.cmd.startsWith(input))
              const showAutocomplete = filtered.length > 0 && input !== filtered[0]?.cmd
              if (showAutocomplete) {
                if (e.key === 'ArrowDown') { e.preventDefault(); setCmdIdx(i => (i + 1) % filtered.length); return }
                if (e.key === 'ArrowUp') { e.preventDefault(); setCmdIdx(i => (i - 1 + filtered.length) % filtered.length); return }
                if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing)) {
                  e.preventDefault(); setInput(filtered[cmdIdx].cmd); setCmdIdx(0); return
                }
              }
            }
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); handleSend() }
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
          onFocus={(e) => {
            e.currentTarget.style.borderColor = editingTs ? 'var(--accent)' : 'var(--border-focus)'
            e.currentTarget.style.boxShadow = '0 0 0 3px var(--accent-glow)'
          }}
          onBlur={(e) => {
            e.currentTarget.style.borderColor = editingTs ? 'var(--accent)' : 'var(--border)'
            e.currentTarget.style.boxShadow = editingTs ? '0 0 0 2px var(--accent-glow)' : 'none'
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

const LocalImage: React.FC<{ path: string; onClickImage?: (src: string) => void }> = ({ path, onClickImage }) => {
  const [src, setSrc] = React.useState<string | null>(null)
  React.useEffect(() => {
    api?.readLocalImage?.(path).then((b64: string | null) => {
      if (b64) setSrc(`data:image/png;base64,${b64}`)
    })
  }, [path])
  if (!src) return <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>...</span>
  // Hands back the PATH, not `src` (the data: URL built from the bytes). Every
  // consumer wants to DO something with the file -- open it in the OS viewer --
  // and a data URL cannot be opened. Passing the rendered URL made each call
  // site look wired while doing nothing.
  return <img src={src} onClick={() => onClickImage?.(path)}
    style={{ maxWidth: 200, maxHeight: 200, borderRadius: 4, marginTop: 4, cursor: 'zoom-in' }} />
}

/**
 * Streaming-aware markdown renderer.
 * Ported from KiroCrew's MarkdownRenderer — handles incomplete code fences
 * during streaming so partial responses render as proper markdown instead of
 * raw text.
 */
const StreamingMarkdown = React.memo<{ content: string }>(({ content }) => {
  const cleaned = content.replace(/^\n+/, '')
  // If there's a complete widget in the stream, render it
  if (hasWidgets(cleaned)) {
    const segments = parseWidgets(cleaned)
    // Anything after the last complete widget may still be streaming
    const lastWidget = segments.findLastIndex(s => s.type === 'widget')
    return (
      <>
        {segments.map((seg, i) => {
          if (seg.type === 'widget') return <WidgetFrame key={i} html={seg.content} title={seg.title} />
          // Last text segment after final widget — still streaming
          if (i > lastWidget) {
            const stripped = seg.content.replace(/<mcwidget[\s\S]*$/, '')
            const prepared = fixStreamingFences(stripped)
            return <React.Fragment key={i}>
              <Markdown remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{prepared}</Markdown>
              <span style={{ animation: 'blink 1s step-end infinite', display: 'inline-flex', verticalAlign: 'middle' }}><PawPrint size={11} /></span>
            </React.Fragment>
          }
          return <Markdown key={i} remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{seg.content}</Markdown>
        })}
      </>
    )
  }
  // Strip any partial/unclosed <mcwidget tag during streaming
  const stripped = cleaned.replace(/<mcwidget[\s\S]*$/, '')
  const prepared = fixStreamingFences(stripped)
  return (
    <>
      <Markdown remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{prepared}</Markdown>
      <span style={{ animation: 'blink 1s step-end infinite', display: 'inline-flex', verticalAlign: 'middle' }}><PawPrint size={11} /></span>
    </>
  )
})

/** Ensure blank line before fences glued to text, and close any unclosed fence. */
function fixStreamingFences(s: string): string {
  // Ensure blank line before opening fences glued to preceding text
  s = s.replace(/([^\n])(\n?)(```\w*\n)/g, (_, pre, nl, fence) =>
    nl ? pre + nl + fence : pre + '\n\n' + fence
  )
  // If there's an odd number of ``` fences, the last one is unclosed — close it
  const fenceCount = (s.match(/^```/gm) || []).length
  if (fenceCount % 2 === 1) s += '\n```'
  return s
}

const MD_REMARK = [remarkGfm, remarkVerbatimUnknownTags]
/**
 * Raw HTML must be ADMITTED, then SANITIZED — in that order.
 *
 * Without `rehype-raw`, react-markdown leaves an `html` node unhandled and its
 * source text is emitted verbatim, so a model writing `<br>` (the only way to
 * break a line inside a GFM table cell) put a literal "<br>" in the bubble.
 * The sanitizer is the core's, imported rather than copied: admitting raw HTML
 * is exactly the point where a second, drifting allowlist would become a hole.
 */
const MD_REHYPE = [rehypeRaw, rehypeSanitize]

const mdComponents: Record<string, React.FC<any>> = {
  a: (p: any) => <a {...p} style={{ color: 'var(--accent)', textDecoration: 'none', cursor: 'pointer' }}
    onMouseEnter={(e: any) => (e.currentTarget.style.textDecoration = 'underline')}
    onMouseLeave={(e: any) => (e.currentTarget.style.textDecoration = 'none')}
    onClick={(e: any) => { e.preventDefault(); const href = p.href; if (href) api?.openExternal?.(href) }} />,
  table: (p: any) => <table style={{ borderCollapse: 'collapse', fontSize: 11, width: '100%', margin: '4px 0' }} {...p} />,
  th: (p: any) => <th style={{ border: '1px solid var(--border)', padding: '3px 6px', textAlign: 'left', fontWeight: 600 }} {...p} />,
  td: (p: any) => <td style={{ border: '1px solid var(--border)', padding: '3px 6px' }} {...p} />,
  code: (p: any) => {
    const match = /language-(\w+)/.exec(p.className || '')
    if (match) {
      return <MochiCodeBlock lang={match[1]} code={String(p.children).replace(/\n$/, '')} />
    }
    // Detect inline code that looks like a file path
    const text = String(p.children)
    if (/^(?:\/(?:Users|home|tmp|var|etc)\/[^\s]+|[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_.-]+)+)\.(?:md|txt|json|yaml|yml|toml|py|ts|js|sh|cfg|conf|log|csv)$/.test(text)) {
      return <FileChip path={text} />
    }
    return <code style={{ background: 'var(--bg-input)', borderRadius: 3, padding: '1px 4px', fontSize: 11 }} {...p} />
  },
  pre: (p: any) => <>{p.children}</>,
  p: (p: any) => {
    // Detect file paths in paragraph text and render as FileChip
    const children = React.Children.toArray(p.children)
    const processed = children.map((child, i) => {
      if (typeof child !== 'string') return child
      // Match absolute file paths (not inside backticks — those go through code component)
      const fileRe = /((?:\/(?:Users|home|tmp|var|etc)\/[^\s,;:'"<>()]+|[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_.-]+)+)\.(?:md|txt|json|yaml|yml|toml|py|ts|js|sh|cfg|conf|log|csv))/g
      const parts: (string | React.ReactElement)[] = []
      let lastIdx = 0
      let match: RegExpExecArray | null
      while ((match = fileRe.exec(child)) !== null) {
        if (match.index > lastIdx) parts.push(child.slice(lastIdx, match.index))
        parts.push(<FileChip key={`fc-${i}-${match.index}`} path={match[1]} />)
        lastIdx = match.index + match[0].length
      }
      if (lastIdx < child.length) parts.push(child.slice(lastIdx))
      return parts.length > 1 ? <React.Fragment key={i}>{parts}</React.Fragment> : child
    })
    return <p style={{ margin: '2px 0' }}>{processed}</p>
  },
  ul: (p: any) => <ul style={{ margin: '2px 0', paddingLeft: 16 }} {...p} />,
  ol: (p: any) => <ol style={{ margin: '2px 0', paddingLeft: 16 }} {...p} />,
  li: (p: any) => <li style={{ marginBottom: 1 }} {...p} />,
  img: (p: any) => {
    const src = p.src
    // Any LOCAL FILE path goes through LocalImage, which reads the bytes over the
    // app API and renders a data URL. A bare `<img src="/Users/…/x.png">` cannot
    // work in a page: the browser resolves it against the gateway origin and 404s.
    //
    // This used to be gated on `.kiro/crew/screenshots/` only, so every UPLOADED
    // image — which the route stores under `uploads/`, and which is how dropped
    // files and now crops are referenced — rendered as a broken image. The test
    // is the shape of the path, not one directory.
    //
    // `startsWith('/')` alone is not enough: gateway-relative URLs like
    // `/assets/logo.png` also start with a slash and must stay ordinary <img>.
    if (isLocalFilePath(src)) {
      // `src` (the path), NOT the data URL LocalImage renders: the OS viewer
      // opens a file, and openLightbox rejects data: URLs for that reason.
      return <LocalImage path={src} onClickImage={() => api?.openLightbox?.(src)} />
    }
    return <img {...p} style={{ maxWidth: '100%', borderRadius: 4, marginTop: 4, cursor: 'zoom-in' }} />
  },
}

/**
 * Does this src name a file on THIS machine (rather than a URL the page can load)?
 *
 * Mirrors the absolute-path roots the paragraph renderer already matches for file
 * chips, plus the app's own data home, so a path under `screenshots/` or
 * `uploads/` is recognised wherever the data home lives.
 */
function isLocalFilePath(src: unknown): src is string {
  if (typeof src !== 'string' || !src.startsWith('/')) return false
  return /^\/(?:Users|home|tmp|var|private|etc)\//.test(src) || src.includes('.kiro/crew/')
}

/** Inline file chip — shows shortened path with preview + reveal buttons */
const FileChip: React.FC<{ path: string }> = ({ path: filePath }) => {
  const parts = filePath.split('/')
  const short = parts.length > 3 ? `…/${parts.slice(-2).join('/')}` : parts.slice(-2).join('/')
  // The SHELL's platform, not the gateway's: `revealFile` is an IPC send Mochi's
  // Electron main process handles, so that host owns which application opens. A
  // browser tab has no shell to report one — and no shell to reveal anything
  // either — so it takes the generic wording.
  const hostPlatform = classifyPlatform(electronPlatform())
  const revealLabel = hostPlatform === 'darwin'
    ? i18nT('apps.mochi.chatPanel.open_in_finder')
    : hostPlatform === 'windows'
      ? i18nT('apps.mochi.chatPanel.open_in_file_explorer')
      : i18nT('apps.mochi.chatPanel.show_in_file_manager')
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      background: 'var(--bg-input)', border: '1px solid var(--border)',
      borderRadius: 6, padding: '2px 6px 2px 5px', margin: '2px 0',
      fontSize: 11, lineHeight: 1.3, verticalAlign: 'middle',
    }}>
      <File size={11} color="var(--text-muted)" />
      {/* Preview and reveal both delegate to the shell bridge (window.mochi),
          published only by the Electron preload — in a browser tab the calls
          are silent no-ops, so the dead controls are withheld rather than
          rendered. Inside the shell the reveal label names that host's own file
          manager, since the shell is what performs the reveal. */}
      {isElectron ? (
        <>
          <span style={{ color: 'var(--text)', cursor: 'pointer', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            title={filePath}
            role="button"
            tabIndex={0}
            onClick={() => api?.previewFile?.(filePath)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); api?.previewFile?.(filePath) } }}
          >{short}</span>
          <button onClick={() => api?.previewFile?.(filePath)} title={i18nT('apps.mochi.chatPanel.preview')} aria-label={i18nT('apps.mochi.chatPanel.preview')} style={{
            background: 'none', border: 'none', padding: '1px', cursor: 'pointer',
            color: 'var(--text-muted)', display: 'flex', alignItems: 'center',
            transition: 'color 0.15s',
          }}
            onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--accent)')}
            onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--text-muted)')}
          ><Eye size={11} /></button>
          <button onClick={() => api?.revealFile?.(filePath)} title={revealLabel} aria-label={revealLabel} style={{
            background: 'none', border: 'none', padding: '1px', cursor: 'pointer',
            color: 'var(--text-muted)', display: 'flex', alignItems: 'center',
            transition: 'color 0.15s',
          }}
            onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--accent)')}
            onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--text-muted)')}
          ><Folder size={11} /></button>
        </>
      ) : (
        <span style={{ color: 'var(--text)', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          title={filePath}
        >{short}</span>
      )}
    </span>
  )
}

/** Sentinel standing in for the tool name while the sentence is translated.
 *  U+0000 cannot occur in a catalog value, so the split is unambiguous. */
const TOOL_SLOT = '\u0000'

/**
 * Render an already-translated sentence with the tool name as a styled `<code>`.
 *
 * The sentence used to be two keys with the tool wedged between them
 * ("Wants to run" + tool + "— OK?"), which forces every language into English
 * word order and gives a translator two fragments neither of which is a
 * sentence. It is now ONE key with a `{{tool}}` placeholder the translation may
 * put anywhere; interpolating a sentinel and splitting on it recovers the
 * `<code>` styling without handing the layout back to the code.
 *
 * The caller passes the RESOLVED string, not the key: `check-i18n-keys.mjs`
 * resolves file-scope literals only, so a key arriving as a parameter is a key
 * the gate cannot verify — which would exempt this whole file from every i18n
 * check. Same reason `i18nKeys.ts` indexes its maps inside the `i18nT(...)` call.
 */
function renderAroundTool(sentence: string, tool: string): React.ReactNode {
  const [before, after = ''] = sentence.split(TOOL_SLOT)
  return (
    <>
      {before}
      <code style={{ background: 'var(--bg-input)', borderRadius: 3, padding: '1px 4px', fontSize: 11 }}>{tool}</code>
      {after}
    </>
  )
}

/** The payload the approval route writes after the `__approval__` marker. */
interface ApprovalPayload {
  id: string
  tool: string
  toolInput?: string
  /** Normalized command, for a grant scoped to exactly this command. */
  fullCommand?: string
  /** Comma-joined base binaries ("cat,wc"), for a grant scoped to the family. */
  baseCommand?: string
}

/** Parse an approval payload, or null when the text is not really one of ours.
 *
 *  Shape-checked, not just JSON-checked: `__approval__"hi"` parses to a string, and
 *  reading `.tool` off it would render `undefined` into the bubble instead of the
 *  message the user typed.
 */
export function parseApproval(raw: string): ApprovalPayload | null {
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const { id, tool } = parsed as Record<string, unknown>
    if (typeof id !== 'string' || typeof tool !== 'string') return null
    return parsed as ApprovalPayload
  } catch {
    return null
  }
}

/**
 * Whether an EXTERNAL approval resolution (dashboard / Slack / another surface)
 * was an approve. Anything that is not an explicit `approved: true` — a reject,
 * a missing flag, a malformed frame — must NOT read as approved: rendering a
 * rejected tool as "Approved" is a fabricated security decision.
 */
export function externalApprovalApproved(data?: Record<string, unknown>): boolean {
  return (data as { approved?: unknown } | undefined)?.approved === true
}

/** Shared look for the scoped-trust rows (full-width, quieter than the verbs). */
const trustScopeBtnStyle: React.CSSProperties = {
  background: 'var(--bg-input)',
  color: 'var(--text)',
  // Longhand: the shorthand value ('1px solid rgba(...)') reads as prose to the
  // i18n lint, which scans module constants. Same border, no word to mistake for copy.
  borderWidth: 1,
  borderStyle: 'solid',
  borderColor: 'rgba(255,180,0,0.25)',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
  borderRadius: 6,
  padding: '3px 8px',
  fontSize: 11,
  cursor: 'pointer',
  textAlign: 'left',
}

const Bubble = React.memo<{ message: ChatMessage; onOption?: (text: string) => void; onImageClick?: (b64: string) => void; onApproval?: (id: string, action: string, pattern?: string) => void; onEdit?: (content: string) => void; animate?: boolean }>(({ message, onOption, onImageClick, onApproval, onEdit, animate = true }) => {
  const mounted = React.useRef(false)
  const shouldAnimate = animate && !mounted.current
  const [copied, setCopied] = React.useState(false)
  // Whether the scoped-trust grants are revealed. Declared here with the other
  // Bubble state: this component early-returns for several message shapes, so a
  // hook added lower down would run conditionally.
  const [trustOpen, setTrustOpen] = React.useState(false)
  React.useEffect(() => { mounted.current = true }, [])

  const content = message.content.replace(/^\n+/, '')

  // Approval message.
  //
  // The prefix is an internal marker the approval route writes, but NOTHING stops a
  // user typing it: the composer echoes the message locally, so `__approval__x`
  // reached this parse and the uncaught SyntaxError took the whole panel down to the
  // error boundary. A payload that does not parse is treated as what it actually is
  // — ordinary text the user wrote.
  const approvalReq = content.startsWith('__approval__') ? parseApproval(content.slice(12)) : null
  if (approvalReq !== null) {
    const req = approvalReq
    // A family grant is only offered when it differs from the exact-command
    // grant — see src/shared/trustPatterns, which must stay equivalent to the
    // dashboard's TrustDropdown so both surfaces offer the same scopes.
    const showTrustBase = familyGrantIsDistinct(req.fullCommand, req.baseCommand)
    const hasTrustScopes = Boolean(req.fullCommand) || showTrustBase
    return (
      <div style={{ alignSelf: 'flex-start', maxWidth: '85%', animation: shouldAnimate ? 'msgIn 0.25s ease-out' : 'none' }}>
        <div style={{
          padding: '8px 10px', borderRadius: '10px 10px 10px 2px',
          background: 'var(--bubble-assistant)', border: '1px solid rgba(255,180,0,0.3)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, marginBottom: 4 }}><Wrench size={13} /> <span>{renderAroundTool(i18nT('apps.mochi.approval.inline_ask', { tool: TOOL_SLOT }), req.tool)}</span></div>
          {req.toolInput && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, whiteSpace: 'pre-wrap', fontFamily: 'monospace', background: 'var(--bg-input)', borderRadius: 4, padding: 4 }}>{req.toolInput}</div>}
          <div style={{ display: 'flex', gap: 6 }}>
            {/* The icon is a component, not a glyph inside the translated
                string: an emoji in the copy can't follow the theme, renders at
                whatever size the font picks, and has to be repeated in every
                locale (AGENTS.md: emoji-as-icon is a bug). */}
            {([
              ['approve', i18nT('apps.mochi.approval.btn_approve'), '#2e7d32', Check],
              ['trust', i18nT('apps.mochi.approval.btn_trust'), '#1565c0', Handshake],
              ['reject', i18nT('apps.mochi.approval.btn_reject'), '#c62828', Ban],
            ] as [string, string, string, React.ComponentType<{ size?: number }>][]).map(
              ([action, label, bg, Icon]) => (
              <button
                key={action}
                // Trust is a SCOPE choice, not one verb. Tapping it reveals the
                // grants inline (see below) instead of firing the broadest one;
                // when the gateway sent no pattern fields there is nothing to
                // scope, so it stays a direct single-action button.
                onClick={() => {
                  if (action === 'trust' && hasTrustScopes) { setTrustOpen(v => !v); return }
                  onApproval?.(req.id, action)
                }}
                aria-expanded={action === 'trust' && hasTrustScopes ? trustOpen : undefined}
                style={{
                background: bg, color: '#fff', border: 'none', display: 'inline-flex',
                alignItems: 'center', gap: 4,
                borderRadius: 6, padding: '3px 10px', fontSize: 11, fontWeight: 600, cursor: 'pointer',
              }}
              ><Icon size={12} />{label}{action === 'trust' && hasTrustScopes ? (trustOpen ? <ChevronDown size={10} /> : <ChevronRight size={10} />) : null}</button>
            ))}
          </div>
          {/* Scoped trust grants, narrowest first — mirroring the dashboard's
              TrustDropdown so the pet is not stuck with only the widest grant.
              Rendered inline rather than in a portaled menu: this panel is a
              ~380px frameless always-on-top window, where a portaled dropdown
              clips at the window edge. */}
          {trustOpen && hasTrustScopes && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 6 }}>
              {req.fullCommand && (
                <button
                  onClick={() => onApproval?.(req.id, 'trust_command', req.fullCommand)}
                  style={trustScopeBtnStyle}
                ><Shield size={11} />{i18nT('apps.mochi.approval.trust_this_command', { cmd: truncateCommandLabel(req.fullCommand) })}</button>
              )}
              {showTrustBase && (
                <button
                  onClick={() => onApproval?.(req.id, 'trust_base', trustBasePattern(req.baseCommand ?? ''))}
                  style={trustScopeBtnStyle}
                ><ShieldPlus size={11} />{i18nT('apps.mochi.approval.trust_all_base', { base: (req.baseCommand ?? '').split(',').join(', ') })}</button>
              )}
              <button
                onClick={() => onApproval?.(req.id, 'trust')}
                style={trustScopeBtnStyle}
              ><ShieldCheck size={11} />{i18nT('apps.mochi.approval.trust_all_tools')}</button>
            </div>
          )}
          {!hasTrustScopes && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 5 }}>
              {i18nT('apps.mochi.approval.trust_hint', { tool: req.tool })}
            </div>
          )}
        </div>
      </div>
    )
  }

  const optMatch = content.match(/\[options:\s*(.+?)\]\s*$/i)
  const text = optMatch ? content.slice(0, optMatch.index).trimEnd() : content
  const options = optMatch ? optMatch[1].split('|').map(o => o.trim()).filter(Boolean) : []
  const isUser = message.role === 'user'

  // Nothing to draw -> draw nothing.
  //
  // A turn whose text is only markup (a lone `<br>`, or whitespace) used to
  // produce a bubble with an empty body and a timestamp. It was masked while raw
  // HTML rendered as literal text -- you saw "<br>" and read it as a glitch in a
  // real message -- and became a visibly blank bubble once the markdown pipeline
  // started honouring the tag. Attachments, options and widgets each count as
  // content, so only a genuinely empty message is dropped.
  const hasVisibleText = text.replace(/<[^>]*>/g, '').trim() !== ''
  const hasOtherContent =
    options.length > 0 ||
    !!(message as any).screenshot ||
    text.includes('![') ||
    hasWidgets(text)
  if (!hasVisibleText && !hasOtherContent) return null

  return (
    <div className="msg-bubble" style={{ alignSelf: isUser ? 'flex-end' : 'flex-start', maxWidth: '85%', width: isUser ? 'fit-content' : undefined, display: 'flex', flexDirection: 'column', alignItems: isUser ? 'flex-end' : 'flex-start', animation: shouldAnimate ? 'msgIn 0.25s ease-out' : 'none' }}>
      {/* Bubble with tail */}
      <div style={{ position: 'relative', display: 'inline-block', maxWidth: '100%' }}>
        <div style={{
          padding: '6px 10px',
          borderRadius: isUser ? '10px 10px 2px 10px' : '10px 10px 10px 2px',
          background: isUser ? 'var(--bubble-user)' : 'var(--bubble-assistant)',
        }}>
          {(message as any).screenshot && (
            <img src={`data:image/png;base64,${(message as any).screenshot}`}
              onClick={() => onImageClick?.((message as any).screenshot)}
              style={{ width: '100%', maxWidth: 200, borderRadius: 4, marginBottom: 4, cursor: 'zoom-in' }} />
          )}
          <div style={{ wordBreak: 'break-word', fontSize: 12, lineHeight: 1.4, overflow: 'hidden' }}>
            {isUser
              ? <span style={{ whiteSpace: 'pre-wrap' }}>
                  {text.split(/\n/).map((line, i) => {
                    // Detect markdown image syntax: ![...](path). The path may
                    // be mdImageDest's `<…>`-wrapped form (attachmentLines in
                    // composerDrop routes uploads through it) — resolve it with
                    // the shared wrap-aware inverse; unwrapped legacy paths
                    // are preserved verbatim (issue #3497).
                    const mdImgMatch = line.match(/^!\[[^\]]*\]\((.+)\)$/)
                    if (mdImgMatch) {
                      return <LocalImage key={i} path={mdImageDestToPath(mdImgMatch[1])} onClickImage={onImageClick} />
                    }
                    // Detect bare image file paths — line starts with / and ends with image extension.
                    const trimmed = line.trim()
                    if (trimmed.startsWith('/') && /\.(?:png|jpg|jpeg|gif|webp)$/i.test(trimmed)) {
                      return <LocalImage key={i} path={trimmed} onClickImage={onImageClick} />
                    }
                    return <React.Fragment key={i}>{i > 0 && '\n'}{line}</React.Fragment>
                  })}
                </span>
              : (() => {
                  // Extract image paths from markdown ![](path) and bare /path.png references.
                  // Paths may contain spaces so match broadly: / ... .ext
                  const imgRe = /!\[[^\]]*\]\(`?(\/[^`\n]+\.(?:png|jpg|jpeg|gif|webp))`?\)/gi
                  const images: string[] = []
                  let cleanText = text.replace(imgRe, (_, p) => { images.push(p.trim()); return '' })
                  const bareRe = /`?(\/[^`\n]+\.(?:png|jpg|jpeg|gif|webp))`?/gi
                  cleanText = cleanText.replace(bareRe, (_, p) => { images.push(p.trim()); return '' }).trim()

                  // Render mcwidget blocks inline
                  if (hasWidgets(cleanText)) {
                    const segments = parseWidgets(cleanText)
                    return <>
                      {segments.map((seg, i) => seg.type === 'widget'
                        ? <WidgetFrame key={i} html={seg.content} title={seg.title} />
                        : <Markdown key={i} remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{seg.content}</Markdown>
                      )}
                      {images.map((p, i) => <LocalImage key={`img-${i}`} path={p} onClickImage={onImageClick} />)}
                    </>
                  }

                  return <>
                    {cleanText && <Markdown remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{cleanText}</Markdown>}
                    {images.map((p, i) => <LocalImage key={i} path={p} onClickImage={onImageClick} />)}
                  </>
                })()
            }
          </div>
          {options.length > 0 && onOption && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
              {options.map((opt) => (
                <button key={opt} onClick={() => onOption(opt)} style={{
                  background: 'var(--accent-glow)', border: '1px solid var(--border-focus)',
                  borderRadius: 6, padding: '3px 10px', color: 'var(--accent)', fontSize: 11,
                  cursor: 'pointer',
                }}>{opt}</button>
              ))}
            </div>
          )}
        </div>
      </div>
      {/* Timestamp + copy/edit */}
      {message.timestamp > 0 && (
        <div style={{
          fontSize: 10, color: 'var(--text-faint)', marginTop: 2,
          display: 'flex', alignItems: 'center', gap: 4,
          justifyContent: isUser ? 'flex-end' : 'flex-start',
          paddingLeft: isUser ? 0 : 4, paddingRight: isUser ? 4 : 0,
        }}>
          {isUser && onEdit && (
            <button
              className="copy-md-btn"
              onClick={() => onEdit(message.content)}
              title={i18nT('apps.mochi.chatPanel.edit_resend')}
              aria-label={i18nT('apps.mochi.chatPanel.edit_resend')}
              style={{
                background: 'none', border: 'none', padding: '2px 3px',
                cursor: 'pointer', color: 'var(--text-faint)',
                opacity: 0, transition: 'opacity 0.15s, transform 0.15s',
                lineHeight: 1, display: 'flex', alignItems: 'center',
                borderRadius: 3,
              }}
              onMouseEnter={(e) => { e.currentTarget.style.transform = 'scale(1.2)'; e.currentTarget.style.color = 'var(--text-muted)' }}
              onMouseLeave={(e) => { e.currentTarget.style.transform = 'scale(1)'; e.currentTarget.style.color = 'var(--text-faint)' }}
            ><SquarePen size={11} /></button>
          )}
          <span>{relativeTime(message.timestamp)}</span>
          {!isUser && (
            <button
              className="copy-md-btn"
              onClick={() => {
                navigator.clipboard.writeText(text)
                setCopied(true)
                setTimeout(() => setCopied(false), 1500)
              }}
              title={copied ? i18nT('apps.mochi.chatPanel.copied') : i18nT('apps.mochi.chatPanel.copy_markdown')}
              aria-label={copied ? i18nT('apps.mochi.chatPanel.copied') : i18nT('apps.mochi.chatPanel.copy_markdown')}
              style={{
                background: 'none', border: 'none', padding: '2px 3px',
                cursor: 'pointer', color: copied ? 'var(--accent)' : 'var(--text-faint)',
                opacity: copied ? 1 : 0, transition: 'opacity 0.15s, transform 0.15s, color 0.15s',
                lineHeight: 1, display: 'flex', alignItems: 'center',
                borderRadius: 3,
              }}
              onMouseEnter={(e) => { e.currentTarget.style.transform = 'scale(1.2)'; e.currentTarget.style.color = 'var(--text-muted)' }}
              onMouseLeave={(e) => { e.currentTarget.style.transform = 'scale(1)'; e.currentTarget.style.color = copied ? 'var(--accent)' : 'var(--text-faint)' }}
            >{copied
              ? <Check size={12} strokeWidth={2.5} />
              : <Copy size={12} />
            }</button>
          )}
        </div>
      )}
    </div>
  )
}, (prev, next) => prev.message.id === next.message.id && prev.message.content === next.message.content && prev.animate === next.animate)

function bStyle(role: string): React.CSSProperties {
  return {
    maxWidth: '85%', padding: '6px 10px', borderRadius: 10,
    alignSelf: role === 'user' ? 'flex-end' : 'flex-start',
    background: role === 'user' ? 'var(--bubble-user)' : 'var(--bubble-assistant)',
  }
}

function relativeTime(ts: number): string {
  // The active locale is already a BCP-47 tag, which is what Intl wants. The
  // language-NAME mapper this replaced existed only because the setting used to
  // hold 'English'/'Chinese' rather than a code.
  const locale = i18next.language || 'en'
  const diff = Math.floor((Date.now() - ts) / 1000)
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  if (Math.abs(diff) < 10) return rtf.format(0, 'second')
  if (Math.abs(diff) < 60) return rtf.format(-diff, 'second')
  const mins = Math.floor(diff / 60)
  if (mins < 60) return rtf.format(-mins, 'minute')
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return rtf.format(-hrs, 'hour')
  const days = Math.floor(hrs / 24)
  if (days < 7) return rtf.format(-days, 'day')
  return new Intl.DateTimeFormat(locale, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(ts)
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

