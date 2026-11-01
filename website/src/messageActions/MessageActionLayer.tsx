/**
 * MessageActionLayer — runs app-contributed message actions (`contributes.messageActions`).
 *
 * The host owns the trigger: an item in a message's More menu, named with the
 * action's title and its app. No app code runs until it is clicked. Then a popover
 * opens next to the message with an app frame in it (`appFrame/`): an iframe that
 * is `sandbox="allow-scripts"` with no `allow-same-origin`, loading the gateway's
 * frame document, which imports the app's ESM `entry` and calls its
 * `activate(ctx)`. The frame gets that one message's text over `postMessage` and
 * nothing else: no other message, no session identity, no DOM. Copy and Open are
 * buttons this layer draws under the frame; the frame only offers their values.
 *
 * One invocation per click, with a host-minted id, a deadline that each resize or
 * offer extends, and a cancel when the popover closes. Every offer is answered
 * with `result.ack` or `result.error`. That contract is the shared one
 * (`appFrame/invocation.ts`, `HostInvocation`); this point adds its own
 * `message.invoke`. The module contract is in docs/app-kit/manifest-reference.md.
 *
 * @module messageActions/MessageActionLayer
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject } from 'react'

import { useQuery } from '@tanstack/react-query'
import { AppWindow, Check, Copy, ExternalLink, X } from 'lucide-react'
import { api } from '../api/client'
import { Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { OverlayPortal, useCloseWhenCrewHidden } from '../components/overlayRoot'
import { useFrameLinkBridge } from '../hooks/useFrameLinkBridge'
import { openExternal } from '../hooks/useFrameOpenLink'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'
import {
  MAX_HOST_MESSAGE_BYTES,
  displaySafe,
  freshRate,
  messageBytes,
  readInvocationMessage,
  takeMessage,
} from '../appFrame/frameProtocol'
import { HostInvocation } from '../appFrame/invocation'
import {
  createSandboxedFrame,
  currentTheme,
  loadMintedFrame,
  mintAppFrame,
  postToFrame,
  renewIntervalMs,
} from '../appFrame/sandboxFrame'
import {
  resolveMessageActions,
  setMessageActionOpener,
  setMessageActions,
  setMessageActionsLoadError,
  type MessageActionRequest,
  type MessageRole,
  type ResolvedMessageAction,
} from './messageActionRegistry'
import { messageTextFor } from './messageText'

/** An invocation with no resize and no accepted offer for this long ends. */
export const INVOCATION_DEADLINE_MS = 120_000

/** A frame that has not said `ready` by then failed. */
export const READY_TIMEOUT_MS = 10_000
/** The frame's height before it reports its own. */
const DEFAULT_HEIGHT = 160
const MIN_HEIGHT = 40
const MARGIN = 8
const GAP = 6

export type CancelReason = 'closed' | 'deadline' | 'other' | 'revoked'

function selectActions(d: unknown): ResolvedMessageAction[] {
  const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
  return resolveMessageActions((apps as never[]) || [])
}

/** Overridable in tests: how a frame's URL and capability are minted. */
/** Mints for one click: the gateway checks the click's role and format against
 *  the live grant (`_admit_message_action`) before any text is sent. */
type Mint = (action: ResolvedMessageAction, role: MessageRole, frameId?: string) => ReturnType<typeof mintAppFrame>
const defaultMint: Mint = (action, role, frameId) =>
  mintAppFrame(action.appName, 'messageActions', action.id, frameId, { role, format: action.format })
let mintForActions: Mint = defaultMint
export function setMessageActionMintForTest(mint?: Mint): void {
  mintForActions = mint ?? defaultMint
}

interface Open extends MessageActionRequest {
  /** Bumped per click, so the same action opened twice gets a fresh popover. */
  n: number
}

export default function MessageActionLayer() {
  const { data, isError, refetch } = useQuery({
    queryKey: ['apps'],
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select: selectActions,
  })
  const actions = data
  useEffect(() => { if (actions) setMessageActions(actions) }, [actions])
  // With no list loaded yet, a failure would look like "no app declares an
  // action". The menus show it instead, with a retry.
  const failed = isError && !actions
  const retry = useCallback(() => { void refetch() }, [refetch])
  useEffect(() => { setMessageActionsLoadError(failed ? retry : null) }, [failed, retry])
  useEffect(() => () => { setMessageActions([]); setMessageActionsLoadError(null) }, [])

  const [open, setOpen] = useState<Open | null>(null)
  // Why the open popover is going away; read by its cleanup for `invoke.cancel`.
  const reasonRef = useRef<CancelReason>('closed')
  const counter = useRef(0)

  useEffect(() => {
    setMessageActionOpener((req) => {
      setOpen((prev) => {
        // The same item on the same message again: a second click closes it.
        if (prev && prev.action.key === req.action.key && prev.anchor === req.anchor) {
          reasonRef.current = 'closed'
          return null
        }
        reasonRef.current = 'other'
        counter.current += 1
        return { ...req, n: counter.current }
      })
    })
    return () => setMessageActionOpener(null)
  }, [])

  // The app was disabled, uninstalled, updated, or the action's grant revoked.
  useEffect(() => {
    if (!open || !actions) return
    const live = actions.find((a) => a.key === open.action.key)
    if (!live || live.revision !== open.action.revision || live.format !== open.action.format || !live.roles.includes(open.source.role)) {
      reasonRef.current = 'revoked'
      setOpen(null)
    }
  }, [actions, open])

  const close = useCallback((reason: CancelReason = 'closed') => {
    reasonRef.current = reason
    setOpen(null)
  }, [])

  if (!open) return null
  return <MessageActionPopover key={open.n} req={open} reasonRef={reasonRef} onClose={close} />
}

type Status = 'loading' | 'ready' | 'failed' | 'error' | 'unreadable'

export function MessageActionPopover({ req, reasonRef, onClose }: {
  req: MessageActionRequest
  reasonRef: MutableRefObject<CancelReason>
  onClose: (reason?: CancelReason) => void
}) {
  const { action, source, anchor } = req
  const popRef = useRef<HTMLDivElement | null>(null)
  const slotRef = useRef<HTMLDivElement | null>(null)
  const frameRef = useRef<HTMLIFrameElement | null>(null)
  // Fixed for this popover: the text was read from the bubble at the click.
  const [payload] = useState(() => messageTextFor(source, action.format, action.maxChars))
  // An unreadable bubble refuses up front: no frame is minted, nothing is sent.
  const [status, setStatus] = useState<Status>(payload ? 'loading' : 'unreadable')
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  const [offers, setOffers] = useState<{ copy?: string; open?: string }>({})
  const [copied, setCopied] = useState<'idle' | 'ok' | 'failed'>('idle')
  /** Bumped on each copy offer, so a clipboard write settles only its own offer. */
  const copyVersion = useRef(0)
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: MARGIN, top: MARGIN })
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const chip = useFrameLinkBridge([frameRef])
  // A switch to another Crew closes the popover, so its key listener never hears that Crew's keys.
  const closeOnHide = useCallback(() => onCloseRef.current('closed'), [])
  useCloseWhenCrewHidden(true, closeOnHide)

  // Place under the message, or above it when there is no room below.
  const place = useCallback(() => {
    if (!anchor.isConnected) { onCloseRef.current('closed'); return }
    const r = anchor.getBoundingClientRect()
    const el = popRef.current
    const w = Math.min(action.width, window.innerWidth - 2 * MARGIN)
    const h = el ? el.offsetHeight : height + 80
    const left = Math.max(MARGIN, Math.min(r.right - w, window.innerWidth - w - MARGIN))
    const below = r.bottom + GAP
    const top = below + h <= window.innerHeight - MARGIN ? below : Math.max(MARGIN, r.top - GAP - h)
    setPos({ left, top })
  }, [anchor, action.width, height])
  useLayoutEffect(() => { place() }, [place, status, offers])
  useEffect(() => {
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [place])

  // Escape, or a press outside the popover, closes it. A press inside the frame
  // never reaches this document; the frame reports Escape itself. A press on a
  // menu, or on the trigger that opens one, is not "outside": that is how the user
  // reaches this action's row again, and the second click on it is what closes
  // the popover (the layer's toggle), not a reload of the app.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCloseRef.current('closed') }
    const onDown = (e: PointerEvent) => {
      const t = e.target
      if (t instanceof Node && (popRef.current?.contains(t) || anchor.contains(t))) return
      if (t instanceof Element && t.closest('[role="menu"], [aria-haspopup="menu"]')) return
      onCloseRef.current('closed')
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onDown, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onDown, true)
    }
  }, [anchor])

  // The frame and its one invocation (`appFrame/invocation.ts` owns its states).
  useEffect(() => {
    const slot = slotRef.current
    if (!slot || !payload) return
    const el = createSandboxedFrame(`${action.appLabel}: ${action.title}`)
    el.dataset.messageActionFrame = action.key
    Object.assign(el.style, { width: '100%', height: '100%', border: '0', display: 'block', background: 'transparent', colorScheme: 'normal' })
    frameRef.current = el
    const rate = freshRate()
    // Read at cleanup on purpose: the layer sets why the popover is closing just before it unmounts.
    const reasons = reasonRef
    let loading = true
    let frameId: string | undefined
    let renewal: number | undefined
    let disposed = false
    const invocation = new HostInvocation(el, 'message.invoke', INVOCATION_DEADLINE_MS, {
      // A later offer of the same kind replaces the earlier one; the button still
      // acts only on a click.
      onOffer: (kind, value) => {
        if (kind === 'copy') copyVersion.current += 1
        setOffers((o) => ({ ...o, [kind]: value }))
        setCopied('idle')
      },
      onResize: (_w, h) => setHeight(Math.max(MIN_HEIGHT, Math.min(action.maxHeight, Math.round(h)))),
      onClose: () => onCloseRef.current('closed'),
      onError: () => setStatus('error'),
      onDeadline: () => onCloseRef.current('deadline'),
    })
    const fail = () => {
      if (!loading) return
      loading = false
      setStatus('failed')
    }
    const readyTimer = window.setTimeout(fail, READY_TIMEOUT_MS)
    const renew = async () => {
      try {
        const minted = await mintForActions(action, source.role, frameId)
        if (!disposed) postToFrame(el, { type: 'capability', capability: minted.capability })
      } catch { /* the frame retries on its next refusal */ }
    }
    const onMessage = (e: MessageEvent) => {
      if (!e.source || e.source !== el.contentWindow) return
      // Bounded: an oversized message, or one past the burst allowance, is dropped unread.
      if (!takeMessage(rate)) return
      if (messageBytes(e.data) > MAX_HOST_MESSAGE_BYTES) return
      const msg = readInvocationMessage(e.data)
      if (!msg) return
      switch (msg.type) {
        case 'ready': {
          if (!loading) return
          loading = false
          window.clearTimeout(readyTimer)
          setStatus('ready')
          invocation.start({ text: payload.text, format: action.format, truncated: payload.truncated, role: source.role })
          return
        }
        case 'failed': fail(); return
        case 'capabilityExpired': void renew(); return
        case 'escape': onCloseRef.current('closed'); return
        default: invocation.handle(msg)
      }
    }
    window.addEventListener('message', onMessage)
    mintForActions(action, source.role).then((minted) => {
      if (disposed) return
      frameId = minted.frameId
      loadMintedFrame(el, slot, minted, {
        theme: currentTheme,
        onNavigatedAway: () => {
          // eslint-disable-next-line no-console -- an app's frame navigating away is otherwise silent
          console.warn(`[message-action] ${action.key}: the frame navigated away and was closed`)
          onCloseRef.current('closed')
        },
      })
      renewal = window.setInterval(() => { void renew() }, renewIntervalMs(minted.ttl))
    }, () => {
      window.clearTimeout(readyTimer)
      fail()
    })
    return () => {
      disposed = true
      invocation.cancel(reasons.current)
      postToFrame(el, { type: 'dispose' })
      window.clearTimeout(readyTimer)
      window.clearInterval(renewal)
      window.removeEventListener('message', onMessage)
      el.remove()
      frameRef.current = null
    }
    // One frame per popover: the popover is keyed per click.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const copyOffer = () => {
    if (offers.copy === undefined) return
    // The feedback belongs to the offer that was copied: a write still pending
    // when the app replaces the offer must not mark the new one "Copied".
    const version = copyVersion.current
    const settle = (state: 'ok' | 'failed') => { if (copyVersion.current === version) setCopied(state) }
    copyToClipboard(offers.copy).then((ok) => settle(ok ? 'ok' : 'failed'), () => settle('failed'))
  }
  let openHost = ''
  try { openHost = offers.open ? new URL(offers.open).hostname : '' } catch { openHost = '' }
  const label = i18nT('components.messageActions.popover_label', { app: action.appLabel, title: action.title })

  return (
    <OverlayPortal>
    <div
      ref={popRef}
      role="dialog"
      aria-label={label}
      data-testid="message-action-popover"
      className="fixed z-[2147482000] flex flex-col rounded-lg border border-border bg-bg-elevated shadow-lg overflow-hidden animate-rise"
      // Never taller than the viewport: the frame shrinks first, so the header and
      // the host's Copy and Open buttons always stay on screen.
      style={{ left: pos.left, top: pos.top, width: Math.min(action.width, window.innerWidth - 2 * MARGIN), maxHeight: `calc(100vh - ${2 * MARGIN}px)` }}
    >
      {/* Host chrome: which app this is, always outside the frame. */}
      <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b border-border text-[12px] text-muted">
        <AppWindow size={13} className="shrink-0" />
        <span className="truncate min-w-0 flex-1" data-testid="message-action-attribution">
          <span className="text-text font-medium">{displaySafe(action.appLabel)}</span>
          <span> · {displaySafe(action.title)}</span>
        </span>
        <Btn
          type="button"
          className="border-0 px-1 py-0.5 text-muted hover:text-text"
          aria-label={i18nT('components.messageActions.close')}
          title={i18nT('components.messageActions.close')}
          data-testid="message-action-close"
          onClick={() => onClose('closed')}
        >
          <X size={14} />
        </Btn>
      </div>
      <div ref={slotRef} style={{ height: status === 'ready' ? height : status === 'loading' ? DEFAULT_HEIGHT : 0, minHeight: status === 'ready' ? MIN_HEIGHT : undefined }} className={`shrink min-h-0 ${status === 'ready' ? '' : status === 'loading' ? 'opacity-0' : 'hidden'}`} />
      {status === 'loading' && (
        <div className="absolute inset-x-0 top-10 px-3 text-[12px] text-muted" aria-live="polite">{i18nT('components.messageActions.loading', { app: action.appLabel })}</div>
      )}
      {(status === 'failed' || status === 'error' || status === 'unreadable') && (
        <div className="shrink-0 px-3 py-3" data-testid="message-action-failed">
          {/* No hand-off: the chat this popover sits on can hold an unsaved composer draft. */}
          <ErrorNotice
            variant="inline"
            message={status === 'failed'
              ? i18nT('components.messageActions.load_failed', { app: action.appLabel })
              : status === 'unreadable'
                ? i18nT('components.messageActions.text_unreadable', { app: action.appLabel })
                : i18nT('components.messageActions.app_error', { app: action.appLabel })}
          />
        </div>
      )}
      {(offers.copy !== undefined || offers.open) && status === 'ready' && (
        <div className="shrink-0 flex flex-col gap-2 px-3 py-2 border-t border-border">
          {copied === 'failed' && (
            /* No hand-off: the chat this popover sits on can hold an unsaved composer draft. */
            <ErrorNotice variant="inline" message={i18nT('components.messageActions.copy_failed')} onDismiss={() => setCopied('idle')} />
          )}
          <div className="flex items-center justify-end gap-2">
          {offers.open && openHost && (
            <Btn data-testid="message-action-open" onClick={() => { if (offers.open) openExternal(offers.open) }}>
              <ExternalLink size={13} />
              <span className="truncate max-w-[180px]">{i18nT('components.messageActions.open_host', { host: openHost })}</span>
            </Btn>
          )}
          {offers.copy !== undefined && (
            <Btn primary data-testid="message-action-copy" onClick={copyOffer}>
              {copied === 'ok' ? <Check size={13} /> : <Copy size={13} />}
              <span>{copied === 'ok' ? i18nT('components.messageActions.copied') : i18nT('components.messageActions.copy')}</span>
            </Btn>
          )}
          </div>
        </div>
      )}
      {chip}
    </div>
    </OverlayPortal>
  )
}
