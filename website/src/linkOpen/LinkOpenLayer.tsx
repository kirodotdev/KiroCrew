/**
 * LinkOpenLayer — runs app-contributed link opening (`contributes.linkOpen`).
 *
 * The host owns the click. A plain left click on an `https:` link in a chat
 * message whose host an enabled app claims (and the owner approved) is held, and
 * the host asks where to open it: in the app, or in the browser. "Always" remembers
 * the answer for that app's entry and that one host. Every other click (a modifier
 * key, a middle click, a link nobody claims, a link outside chat) is left alone and
 * behaves exactly as it did.
 *
 * Opening in the app shows a host-drawn dialog holding the app's frame, the shared
 * app frame (`appFrame/sandboxFrame.ts`): `sandbox="allow-scripts"` with no
 * `allow-same-origin`. The frame is created on the choice and removed when the
 * dialog closes. Each open is one invocation on the shared contract
 * (`appFrame/invocation.ts`): `link.open { invocationId, deadlineMs, url, theme }`.
 * The frame's size report (`invocation.resize`) means the app drew the link: the
 * frame is shown and the deadline stops. `invocation.error`, or no answer before
 * the deadline, fails the open; closing the dialog first cancels it. The URL has no
 * query and no fragment unless the owner approved whole URLs. An offer from the app
 * (a copy, an `https:` link) becomes an item in the host's overflow menu; nothing
 * acts without a click. A held link is re-resolved whenever the app list changes,
 * so it never keeps a grant the owner withdrew.
 *
 * Opening in the browser is always the host's own button, run inside the user's
 * click, so it is never an asynchronous open a popup blocker refuses.
 *
 * @module linkOpen/LinkOpenLayer
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { MoreHorizontal } from 'lucide-react'
import ErrorNotice from '../components/ErrorNotice'
import Modal from '../components/Modal'
import { Btn } from '../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { copyToClipboard } from '../utils/clipboard'
import { useFrameLinkBridge } from '../hooks/useFrameLinkBridge'
import { openExternal } from '../hooks/useFrameOpenLink'
import { i18nT } from '../i18n/t'
import { freshRate, messageBytes, MAX_HOST_MESSAGE_BYTES, readInvocationMessage, takeMessage, type OfferKind } from '../appFrame/frameProtocol'
import { HostInvocation } from '../appFrame/invocation'
import {
  createSandboxedFrame,
  currentTheme,
  loadMintedFrame,
  mintAppFrame,
  postToFrame,
  renewIntervalMs,
  type Minted,
} from '../appFrame/sandboxFrame'
import { withoutQuery } from '../linkPreview/LinkPreviewLayer'
import {
  openerFor,
  rememberChoice,
  rememberedChoice,
  resolveLinkOpeners,
  type ResolvedLinkOpener,
} from './linkOpenRegistry'

/** Waiting for the module to activate; a frame that has not said `ready` by then failed. */
export const READY_TIMEOUT_MS = 10_000
/** How long the app has to accept the link once its module is ready. */
export const OPEN_DEADLINE_MS = 10_000

function selectOpeners(d: unknown): ResolvedLinkOpener[] {
  const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
  return resolveLinkOpeners((apps as never[]) || [])
}
const NONE: ResolvedLinkOpener[] = []

/** Overridable in tests: how a frame's URL and capability are minted. */
type Mint = (
  appName: string,
  point: string,
  pointId: string,
  frameId?: string,
  request?: Readonly<Record<string, string>>,
) => Promise<Minted>
let mintForFrames: Mint = mintAppFrame
export function setLinkOpenMintForTest(mint?: Mint): void {
  mintForFrames = mint ?? mintAppFrame
}

/** A held click: the link, its claimed host, and the entry that claims it. */
export interface HeldLink {
  href: string
  host: string
  opener: ResolvedLinkOpener
}

/** The plain left click on a claimed chat link this event is, or null. */
export function heldLinkOf(
  e: MouseEvent,
  openers: readonly ResolvedLinkOpener[],
  dashboardOrigin: string = window.location.origin,
): HeldLink | null {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return null
  const path = e.composedPath()
  // Chat messages only: a link elsewhere in the dashboard never reaches an app.
  if (!path.some((n) => n instanceof HTMLElement && n.hasAttribute('data-link-preview-scope'))) return null
  const a = path.find((n): n is HTMLAnchorElement => n instanceof HTMLAnchorElement && Boolean(n.href))
  if (!a) return null
  // The dashboard's own links (artifacts, `?sid=` sessions) belong to its own click
  // handlers, which run after this capture listener: a host grant that happens to
  // cover the dashboard's host never hands them to an app.
  if (originOf(a.href) === dashboardOrigin) return null
  const hit = openerFor(a.href, openers)
  return hit ? { href: a.href, host: hit.host, opener: hit.opener } : null
}

/** `held` re-resolved against the current openers, or null when the entry that
 *  claimed it no longer claims it. */
export function reheld(held: HeldLink, openers: readonly ResolvedLinkOpener[]): HeldLink | null {
  const hit = openerFor(held.href, openers)
  if (!hit || hit.opener.key !== held.opener.key) return null
  return { href: held.href, host: hit.host, opener: hit.opener }
}

function originOf(url: string): string {
  try { return new URL(url).origin } catch { return '' }
}

function hostOf(url: string): string {
  try { return new URL(url).hostname } catch { return '' }
}

export default function LinkOpenLayer() {
  const { data } = useQuery({
    queryKey: ['apps'],
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select: selectOpeners,
  })
  const openers = data ?? NONE
  const openersRef = useRef(openers)
  openersRef.current = openers
  const [asking, setAsking] = useState<HeldLink | null>(null)
  const [opened, setOpened] = useState<HeldLink | null>(null)

  useEffect(() => {
    if (!openers.length) return
    const onClick = (e: MouseEvent) => {
      const held = heldLinkOf(e, openersRef.current)
      if (!held) return
      const remembered = rememberedChoice(held.opener.key, held.host)
      // A remembered "browser" is an ordinary link again.
      if (remembered === 'browser') return
      e.preventDefault()
      e.stopPropagation()
      if (remembered === 'app') setOpened(held)
      else setAsking(held)
    }
    document.addEventListener('click', onClick, true)
    return () => document.removeEventListener('click', onClick, true)
  }, [openers.length])

  // A held link follows the current grants, not the ones it was clicked under. An
  // app that went away, or no longer claims the host, takes its question and its
  // open dialog with it; the question picks up a narrower grant. An open dialog
  // whose whole-URL grant changed closes: its invocation was handed the URL under
  // the old grant.
  useEffect(() => {
    setAsking((h) => (h ? reheld(h, openers) : null))
    setOpened((h) => {
      const next = h ? reheld(h, openers) : null
      if (!h || !next || next.opener.fullUrl !== h.opener.fullUrl) return null
      // Same object, so the panel keeps its frame across an ordinary list refetch.
      return h
    })
  }, [openers])

  return (
    <>
      {asking ? (
        <LinkOpenChooser
          held={asking}
          onClose={() => setAsking(null)}
          onOpenInApp={(always) => {
            if (always) rememberChoice(asking.opener.key, asking.host, 'app')
            setAsking(null)
            setOpened(asking)
          }}
          onOpenInBrowser={(always) => {
            if (always) rememberChoice(asking.opener.key, asking.host, 'browser')
            setAsking(null)
            // Inside the user's click on this button: never popup-blocked.
            openExternal(asking.href)
          }}
        />
      ) : null}
      {opened ? <LinkOpenPanel held={opened} onClose={() => setOpened(null)} /> : null}
    </>
  )
}

function LinkOpenChooser({ held, onClose, onOpenInApp, onOpenInBrowser }: {
  held: HeldLink
  onClose: () => void
  onOpenInApp: (always: boolean) => void
  onOpenInBrowser: (always: boolean) => void
}) {
  const [always, setAlways] = useState(false)
  const app = held.opener.label
  const sent = held.opener.fullUrl ? held.href : withoutQuery(held.href)
  return (
    <Modal
      open
      onClose={onClose}
      maxWidth={440}
      title={i18nT('components.linkOpen.ask_title', { host: held.host, app })}
      footer={(
        <div className="flex flex-wrap justify-end gap-2" data-testid="link-open-chooser">
          <Btn onClick={() => onOpenInBrowser(always)}>{i18nT('components.linkOpen.open_in_browser')}</Btn>
          <Btn primary onClick={() => onOpenInApp(always)}>{i18nT('components.linkOpen.open_in_app', { app })}</Btn>
        </div>
      )}
    >
      <div className="flex flex-col gap-3 text-[13px]">
        <p className="text-muted">{i18nT('components.linkOpen.ask_body', { app })}</p>
        <code className="block break-all rounded border border-border bg-bg-elevated px-2 py-1 text-[12px] text-text">{sent}</code>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={always} onChange={(e) => setAlways(e.target.checked)} aria-label={i18nT('components.linkOpen.always', { host: held.host })} />
          <span>{i18nT('components.linkOpen.always', { host: held.host })}</span>
        </label>
      </div>
    </Modal>
  )
}

type PanelState = 'loading' | 'opening' | 'open' | 'failed'

function LinkOpenPanel({ held, onClose }: { held: HeldLink; onClose: () => void }) {
  const app = held.opener.label
  const url = held.opener.fullUrl ? held.href : withoutQuery(held.href)
  const holderRef = useRef<HTMLDivElement>(null)
  const frameRef = useRef<HTMLIFrameElement | null>(null)
  const [frameRefs, setFrameRefs] = useState<{ current: HTMLIFrameElement | null }[]>([])
  const [state, setState] = useState<PanelState>('loading')
  // What the app offered for this open: host menu items, acted on only on a click.
  const [offers, setOffers] = useState<Partial<Record<OfferKind, string>>>({})
  const [copyFailed, setCopyFailed] = useState(false)
  const copyOffer = async (text: string) => {
    setCopyFailed(false)
    setCopyFailed(!(await copyToClipboard(text)))
  }
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  // The live invocation, so closing the dialog can cancel it.
  const invRef = useRef<HostInvocation | null>(null)
  // A link inside the app's frame opens through the link bridge.
  const chip = useFrameLinkBridge(frameRefs)

  useEffect(() => {
    const holder = holderRef.current
    if (!holder) return
    const { appName, id, key } = held.opener
    const el = createSandboxedFrame(app)
    el.dataset.linkOpenFrame = key
    Object.assign(el.style, { width: '100%', height: '100%', border: '0', background: 'transparent', colorScheme: 'normal', visibility: 'hidden' })
    frameRef.current = el
    setFrameRefs([frameRef])
    let disposed = false
    let phase: PanelState = 'loading'
    let frameId: string | undefined
    let renewal: number | undefined
    const rate = freshRate()
    // What this open is for, checked by the gateway against the live grant on every
    // mint and renewal: a tab holding an older app list cannot open a host, or hand
    // over a whole URL, that the owner has since taken back.
    const request = { host: held.host, url: held.opener.fullUrl ? 'full' : 'stripped' }
    const move = (next: PanelState, why = '') => {
      phase = next
      setState(next)
      // eslint-disable-next-line no-console -- an app failing to open a link is otherwise silent
      if (why) console.error(`[link-open] ${key} could not open the link: ${why}`)
    }
    const fail = (why: string) => {
      if (phase === 'failed') return
      window.clearTimeout(readyTimer)
      invRef.current?.cancel('other')
      el.style.visibility = 'hidden'
      move('failed', why)
    }
    const readyTimer = window.setTimeout(() => { if (phase === 'loading') fail('timeout') }, READY_TIMEOUT_MS)
    const renew = async () => {
      try {
        const minted = await mintForFrames(appName, 'linkOpen', id, frameId, request)
        if (!disposed) postToFrame(el, { type: 'capability', capability: minted.capability })
      } catch {
        // The frame asks again on its next refusal.
      }
    }
    mintForFrames(appName, 'linkOpen', id, undefined, request).then(
      (minted) => {
        if (disposed) return
        frameId = minted.frameId
        loadMintedFrame(el, holder, minted, {
          theme: currentTheme,
          onNavigatedAway: () => {
            // eslint-disable-next-line no-console -- an app's frame navigating away is otherwise silent
            console.warn(`[link-open] ${key}: the frame navigated away and was closed`)
            el.remove()
            fail('navigated')
          },
        })
        renewal = window.setInterval(() => { void renew() }, renewIntervalMs(minted.ttl))
      },
      () => { if (!disposed) fail('mint') },
    )

    const onMessage = (e: MessageEvent) => {
      if (disposed || !e.source || e.source !== el.contentWindow) return
      // Bounded: past the burst allowance, or over the size cap, a message is dropped unread.
      if (!takeMessage(rate) || messageBytes(e.data) > MAX_HOST_MESSAGE_BYTES) return
      const msg = readInvocationMessage(e.data)
      if (!msg) return
      switch (msg.type) {
        case 'capabilityExpired': void renew(); return
        case 'failed': if (phase === 'loading') fail(msg.message || 'failed'); return
        case 'escape': closeRef.current(); return
        case 'ready': {
          if (phase !== 'loading') return
          window.clearTimeout(readyTimer)
          const inv = new HostInvocation(el, 'link.open', OPEN_DEADLINE_MS, {
            onOffer: (kind, value) => setOffers((o) => ({ ...o, [kind]: value })),
            onResize: () => {
              if (phase !== 'opening') return
              // The app drew the link: show it, and stop the deadline while the user reads.
              inv.settle()
              el.style.visibility = 'visible'
              move('open')
            },
            onClose: () => closeRef.current(),
            onError: (message) => fail(message || 'error'),
            onDeadline: () => fail('deadline'),
          })
          invRef.current = inv
          move('opening')
          inv.start({ url, theme: currentTheme() })
          return
        }
        default:
          invRef.current?.handle(msg)
      }
    }
    window.addEventListener('message', onMessage)
    return () => {
      disposed = true
      window.removeEventListener('message', onMessage)
      window.clearTimeout(readyTimer)
      if (renewal !== undefined) window.clearInterval(renewal)
      invRef.current?.dispose()
      invRef.current = null
      postToFrame(el, { type: 'dispose', reason: 'closed' })
      el.remove()
      frameRef.current = null
      setFrameRefs([])
    }
  }, [held, url, app])

  const close = useCallback(() => {
    invRef.current?.cancel('closed')
    onClose()
  }, [onClose])

  return (
    <Modal
      open
      onClose={close}
      maxWidth={960}
      height="80vh"
      ariaLabel={i18nT('components.linkOpen.panel_label', { app, host: held.host })}
      title={<span className="flex min-w-0 items-baseline gap-2"><span>{app}</span><span className="truncate text-[12px] font-normal text-muted">{held.host}</span></span>}
      headerActions={(
        // One trigger beside the close button: the row holds at most two controls.
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Btn
              className="!px-1.5"
              aria-label={i18nT('components.linkOpen.more_actions')}
              title={i18nT('components.linkOpen.more_actions')}
              data-testid="link-open-offers"
            >
              <MoreHorizontal size={14} />
            </Btn>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {/* The host's own open: runs inside this select, never an async popup. */}
            <DropdownMenuItem onSelect={() => openExternal(held.href)} data-testid="link-open-in-browser">
              {i18nT('components.linkOpen.open_in_browser')}
            </DropdownMenuItem>
            {offers.copy !== undefined ? (
              <DropdownMenuItem onSelect={() => { void copyOffer(offers.copy ?? '') }} data-testid="link-open-offer-copy">
                {i18nT('components.linkOpen.offer_copy')}
              </DropdownMenuItem>
            ) : null}
            {offers.open ? (
              <DropdownMenuItem onSelect={() => openExternal(offers.open ?? '')} title={offers.open} data-testid="link-open-offer-open">
                {i18nT('components.linkOpen.offer_open', { host: hostOf(offers.open) })}
              </DropdownMenuItem>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    >
      <div className="relative h-full min-h-[320px] w-full" data-testid="link-open-panel" data-state={state}>
        <div ref={holderRef} className="absolute inset-0" />
        {copyFailed ? (
          <div className="absolute inset-x-0 top-0 z-10 p-2" data-testid="link-open-copy-failed">
            {/* No hand-off: the chat composer's unsent draft sits behind this dialog, and the hand-off navigates away from it. */}
            <ErrorNotice
              message={i18nT('components.linkOpen.copy_failed')}
              onDismiss={() => setCopyFailed(false)}
            />
          </div>
        ) : null}
        {state === 'loading' || state === 'opening' ? (
          <div className="absolute inset-0 flex items-center justify-center text-[13px] text-muted">
            {i18nT('components.linkOpen.opening', { app })}
          </div>
        ) : null}
        {state === 'failed' ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6" data-testid="link-open-failed">
            {/* No hand-off: the chat composer's unsent draft sits behind this dialog, and the hand-off navigates away from it. */}
            <ErrorNotice variant="inline" message={i18nT('components.linkOpen.failed', { app })} />
            <Btn primary onClick={() => openExternal(held.href)}>{i18nT('components.linkOpen.open_in_browser')}</Btn>
          </div>
        ) : null}
      </div>
      {chip}
    </Modal>
  )
}
