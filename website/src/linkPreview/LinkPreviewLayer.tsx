/**
 * LinkPreviewLayer — runs app-contributed link previews (`contributes.linkPreviews`).
 *
 * The host owns detection: which links (the provider's declared hosts, https
 * only), the hover delay, leave and dismissal. No app code runs until a matching
 * link is hovered. Then the provider's card frame is created: an iframe that is
 * `sandbox="allow-scripts"` with no `allow-same-origin`, loading the gateway's
 * frame document, which imports the app's ESM `entry` and calls its
 * `activate(ctx)`. The card never runs in this document: it gets the hovered URL
 * or text, the anchor's rect and the theme over `postMessage`, and nothing else.
 * The dashboard positions and sizes the frame (`linkPreviewFrames.ts`); the
 * messages are in `linkPreviewProtocol.ts` and the module contract in
 * docs/app-kit/manifest-reference.md.
 *
 * @module linkPreview/LinkPreviewLayer
 */
import { useEffect, useRef, useState, type RefObject } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RotateCw } from 'lucide-react'
import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import { activeElementIsEditable, isEditableTarget } from '../utils/editableTarget'
import { useFrameLinkBridge } from '../hooks/useFrameLinkBridge'
import { i18nT } from '../i18n/t'
import { resolveLinkPreviews, providerFor, type ResolvedLinkPreview } from './linkPreviewRegistry'
import { withoutQuery } from '../appFrame/hosts'
import { currentTheme } from '../appFrame/sandboxFrame'
import { FrameHost, mintFrame, type FailureCause, type FrameHostOptions } from './linkPreviewFrames'
import { resolveTextPreview, setLinkPreviewTextPatterns, setTextPatternsOff } from './textPatterns'

const HOVER_DELAY_MS = 300
/** A provider whose frame failed to load or activate is tried again on a hover this long after. */
export const RETRY_AFTER_MS = 30_000
/** The failure card's height, for placing it above the link when it does not fit below. */
const FAILED_CARD_HEIGHT = 96
/** The failure card sits a few pixels from its link: leaving one for the other
 *  crosses that gap, so the card closes only this long after the pointer left both. */
export const LEAVE_GRACE_MS = 300

/** What the failure card says, by cause. */
const FAILED_LINE: Record<FailureCause, string> = {
  timeout: 'components.linkPreview.failed_timeout',
  app: 'components.linkPreview.failed_app',
  unavailable: 'components.linkPreview.failed_unavailable',
}

interface FailedNotice {
  /** The provider key, or '' when the app list itself could not be read. */
  key: string
  cause: FailureCause
  left: number
  top: number
  width: number
  retrying: boolean
}

type Timer = { current: number | undefined }
function cancelGraceOf(t: Timer): void {
  window.clearTimeout(t.current)
  t.current = undefined
}
function closeAfterGraceOf(t: Timer, close: () => void): void {
  cancelGraceOf(t)
  t.current = window.setTimeout(() => { t.current = undefined; close() }, LEAVE_GRACE_MS)
}

/** Where the failure card goes for the element `el`: where the app's card would. */
function spotFor(el: HTMLElement): { left: number; top: number; width: number } {
  const r = el.getBoundingClientRect()
  return FrameHost.cardSpot({ left: r.left, top: r.top, width: r.width, height: r.height }, FAILED_CARD_HEIGHT)
}

// The URL a provider is shown by default (`fullUrl` opts out); shared with every
// point that hands an app a link.
export { withoutQuery } from '../appFrame/hosts'

function selectProviders(d: unknown): ResolvedLinkPreview[] {
  const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
  return resolveLinkPreviews((apps as never[]) || [])
}
const NONE: ResolvedLinkPreview[] = []

/** Overridable in tests: how a frame's URL and capability are minted. */
let mintForFrames: FrameHostOptions['mint'] = mintFrame
export function setLinkPreviewMintForTest(mint?: FrameHostOptions['mint']): void {
  mintForFrames = mint ?? mintFrame
}

export default function LinkPreviewLayer() {
  const { data, isError, refetch } = useQuery({
    queryKey: ['apps'],
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select: selectProviders,
  })
  const providers = data ?? NONE
  const providersRef = useRef(providers)
  providersRef.current = providers
  // In an effect, not render: a change notifies every rendered message, and
  // notifying other components during this one's render is a React error.
  useEffect(() => { setLinkPreviewTextPatterns(providers) }, [providers])
  // key -> when its frame last failed, and why
  const failedAt = useRef(new Map<string, { at: number; cause: FailureCause }>())
  // The hover whose card could not be shown: the frame failed to load or the
  // module threw. Drawn as a card where the app's card would be, until the
  // pointer leaves the link and the card, or the card is closed.
  const [notice, setNotice] = useState<FailedNotice | null>(null)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const noticeRef = useRef(notice)
  noticeRef.current = notice
  // A pending close of the failure card: the pointer left its link or the card.
  const graceRef = useRef<number | undefined>(undefined)
  // Set by the hover effect: the pointer left the failure card for somewhere
  // other than its link, which ends the hover like leaving the link does.
  const leaveCardRef = useRef<(to: EventTarget | null) => void>(() => setNotice(null))
  // Set by the hover effect: the card's ✕ or a successful Retry ends the hover
  // the card belongs to, so a pending show is dropped and the app is told `leave`.
  const closeCardRef = useRef<() => void>(() => setNotice(null))
  // The live frames, for the link bridge: a card's links open through it.
  const [frameRefs, setFrameRefs] = useState<RefObject<HTMLIFrameElement | null>[]>([])
  const hostRef = useRef<FrameHost | null>(null)
  const hideAllRef = useRef<(reason: string) => void>(() => {})
  // The hover in progress and its provider: a frame's failure is shown only under
  // its own hover, and nothing is shown once the user has left it.
  const anchorRef = useRef<{ el: HTMLElement; key: string; appName: string; link: { url?: string; text?: string } } | null>(null)

  const chip = useFrameLinkBridge(frameRefs)

  useEffect(() => {
    const host = new FrameHost({
      mint: (app, frameId, providerId) => mintForFrames(app, frameId, providerId),
      theme: currentTheme,
      onFailed: (frame, why, cause) => {
        // eslint-disable-next-line no-console -- an app's link preview failing to load is otherwise silent
        console.error(`[link-preview] ${frame.key} failed to load: ${why}`)
        failedAt.current.set(frame.key, { at: Date.now(), cause })
        const a = anchorRef.current
        if (a && a.key === frame.key && a.el.isConnected) {
          setNotice({ key: frame.key, cause, ...spotFor(a.el), retrying: false })
        }
      },
      onTextPatternsOff: (app, ids) => setTextPatternsOff(app, ids),
      onEscape: () => hideAllRef.current('escape'),
      onFramesChanged: (els) => setFrameRefs(els.map((el) => ({ current: el }))),
    })
    hostRef.current = host
    const themeWatch = new MutationObserver(() => host.setTheme(currentTheme()))
    themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] })
    return () => {
      themeWatch.disconnect()
      host.disposeAll()
      hostRef.current = null
    }
  }, [])

  // Dispose the frames of providers whose app went away (disabled / uninstalled),
  // and of those whose app was updated: a frame keeps the module it loaded, so the
  // next hover must get a fresh one that loads the current entry.
  const revisionsRef = useRef(new Map<string, string>())
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const live = new Map(providers.map((p) => [p.key, p.revision]))
    const seen = revisionsRef.current
    for (const f of host.frames()) {
      const revision = live.get(f.key)
      if (revision === undefined || (seen.has(f.key) && seen.get(f.key) !== revision)) host.disposeKey(f.key)
    }
    revisionsRef.current = live
  }, [providers])

  useEffect(() => {
    if (!providers.length) return
    const host = hostRef.current
    if (!host) return
    const doc = document
    let current: { a: HTMLElement; key: string } | null = null
    let timer: number | undefined
    // The link the latest card belongs to and where it was when the hover began.
    // Kept after the pointer moves on into the card: a scroll only dismisses the
    // card when it moved this link.
    let watched: { el: HTMLElement; left: number; top: number } | null = null
    const watch = (el: HTMLElement) => {
      const r = el.getBoundingClientRect()
      watched = { el, left: r.left, top: r.top }
    }

    const linkOf = (e: Event) => {
      const path = e.composedPath()
      // Chat messages only: a link elsewhere in the dashboard (Connections, an
      // OAuth approval, Settings) never reaches an app.
      if (!path.some((n) => n instanceof HTMLElement && n.hasAttribute('data-link-preview-scope'))) return null
      // composedPath, so a link inside an open shadow root (a rendered widget)
      // is seen, not only its retargeted host.
      const el = path.find((n): n is HTMLAnchorElement => n instanceof HTMLAnchorElement && Boolean(n.href))
      if (el) {
        const p = providerFor(el.href, providersRef.current)
        return p ? { a: el as HTMLElement, url: (p.fullUrl ? el.href : withoutQuery(el.href)) as string | undefined, text: undefined as string | undefined, p } : null
      }
      // A plain-text match: the owner is re-derived from the span's own text, and
      // the provider gets that text and nothing else.
      const sp = path.find((n): n is HTMLElement => n instanceof HTMLElement && n.hasAttribute('data-link-preview-text'))
      if (!sp) return null
      const text = sp.textContent ?? ''
      const hit = resolveTextPreview(text)
      const p = hit ? providersRef.current.find((x) => x.key === hit.owner) : undefined
      return p ? { a: sp, url: undefined as string | undefined, text: text as string | undefined, p } : null
    }

    const onOver = (e: PointerEvent) => {
      const hit = linkOf(e)
      if (!hit) return
      // Back on a link (its own or another): a close pending from leaving goes.
      cancelGraceOf(graceRef)
      if (hit.a === current?.a) return
      const key = hit.p.key
      const lastFailed = failedAt.current.get(key)
      const link = hit.url ? { url: hit.url } : { text: hit.text }
      // A different link ends the earlier hover here, whether or not its own
      // pointerout ended it (the failure card's leave grace defers that): its
      // pending show, a Retry's included, is dropped and its app told `leave`.
      if (current) host.leave(current.key)
      current = { a: hit.a, key }
      anchorRef.current = { el: hit.a, key, appName: hit.p.appName, link }
      watch(hit.a)
      window.clearTimeout(timer)
      // A different link: a failure card for an earlier one goes.
      setNotice(null)
      if (lastFailed !== undefined && Date.now() - lastFailed.at < RETRY_AFTER_MS) {
        // A failed frame is not final, but it is not retried on every hover
        // either: the card says why it failed, and its Retry loads it now.
        timer = window.setTimeout(() => {
          if (current?.a !== hit.a || !hit.a.isConnected) return
          setNotice({ key, cause: lastFailed.cause, ...spotFor(hit.a), retrying: false })
        }, HOVER_DELAY_MS)
        return
      }
      // Create the frame during the delay, so the first card is not late by the load.
      const frame = host.hoverFrame(key, hit.p.appName)
      const a = hit.a
      const modifiers = { alt: e.altKey, shift: e.shiftKey, ctrl: e.ctrlKey, meta: e.metaKey }
      timer = window.setTimeout(() => {
        if (current?.a !== a || !a.isConnected) return
        host.hideUnpinned('other-provider', key)
        const r = a.getBoundingClientRect()
        void host.show(frame, link, { left: r.left, top: r.top, width: r.width, height: r.height }, modifiers).then((ok) => {
          if (ok) failedAt.current.delete(key)
        })
      }, HOVER_DELAY_MS)
    }
    const endHover = () => {
      setNotice(null)
      window.clearTimeout(timer)
      if (!current) return
      const key = current.key
      current = null
      anchorRef.current = null
      host.leave(key)
    }
    const onOut = (e: PointerEvent) => {
      if (!current) return
      const to = e.relatedTarget instanceof Element ? e.relatedTarget.closest('a[href], [data-link-preview-text]') : null
      if (to === current.a) return
      // Into the failure card: the hover goes on, so its Retry can be pressed.
      if (e.relatedTarget instanceof Node && cardRef.current?.contains(e.relatedTarget)) return
      if (!e.composedPath().includes(current.a)) return
      // A failure card is showing: the pointer may be crossing the gap to it.
      if (noticeRef.current) closeAfterGraceOf(graceRef, endHover)
      else endHover()
    }
    leaveCardRef.current = (to) => {
      if (current && to instanceof Node && current.a.contains(to)) return
      closeAfterGraceOf(graceRef, endHover)
    }
    closeCardRef.current = () => {
      cancelGraceOf(graceRef)
      endHover()
    }
    const hideAll = (reason: string) => {
      setNotice(null)
      window.clearTimeout(timer)
      current = null
      anchorRef.current = null
      watched = null
      host.hideUnpinned(reason)
    }
    hideAllRef.current = hideAll
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { hideAll('escape'); return }
      // A card's own shortcuts (pin, copy): only a plain key, only while the
      // dashboard is not taking text, and only to a card that is showing.
      if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing || e.key.length !== 1) return
      // Shadow-aware: the code editor's editable node sits inside a shadow root,
      // where `document.activeElement` only reports its host.
      if (isEditableTarget(e) || activeElementIsEditable()) return
      host.forwardKey(e)
    }
    // Scrolling the chat moves the link away from its card. Scrolling inside a
    // card happens in its frame and never reaches this window. Any other scroll
    // (the sidebar, a transcript settling after load, a scroll that ends where it
    // began) leaves the link where it was, and so leaves its card. Only the
    // latest hover's link is known, so a card from an earlier hover (pinned,
    // then unpinned) still goes on any scroll.
    const onScroll = () => {
      if (watched && watched.el.isConnected && !host.hasUnpinnedDetached()) {
        const r = watched.el.getBoundingClientRect()
        if (Math.abs(r.left - watched.left) < 1 && Math.abs(r.top - watched.top) < 1) return
      }
      hideAll('scroll')
    }
    // Focus moving into a card's frame blurs this window too; that is the user
    // using the card, not leaving the page.
    const onBlur = () => {
      window.setTimeout(() => { if (!host.isFrame(document.activeElement)) hideAll('blur') }, 0)
    }

    doc.addEventListener('pointerover', onOver, true)
    doc.addEventListener('pointerout', onOut, true)
    doc.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('blur', onBlur)
    return () => {
      window.clearTimeout(timer)
      cancelGraceOf(graceRef)
      // A card shown through these listeners must not outlive them. Nor may a
      // failure card or its Retry: the hover's link payload was built from the
      // provider list being replaced, so a Retry must not send it again.
      host.hideUnpinned('disabled')
      anchorRef.current = null
      setNotice(null)
      hideAllRef.current = () => {}
      leaveCardRef.current = () => setNotice(null)
      closeCardRef.current = () => setNotice(null)
      doc.removeEventListener('pointerover', onOver, true)
      doc.removeEventListener('pointerout', onOut, true)
      doc.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('blur', onBlur)
    }
  }, [providers])

  // The app list could not be read, so no provider is known. A hover on a chat
  // link says so, and asks for the list again (at most every RETRY_AFTER_MS).
  useEffect(() => {
    if (!isError || providers.length) return
    let timer: number | undefined
    let over: Element | null = null
    let askedAt = 0
    const onOver = (e: PointerEvent) => {
      const path = e.composedPath()
      if (!path.some((n) => n instanceof HTMLElement && n.hasAttribute('data-link-preview-scope'))) return
      const a = path.find((n): n is HTMLAnchorElement => n instanceof HTMLAnchorElement && n.protocol === 'https:')
      if (!a) return
      cancelGraceOf(graceRef)
      if (a === over) return
      over = a
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        if (over !== a || !a.isConnected) return
        setNotice({ key: '', cause: 'unavailable', ...spotFor(a), retrying: false })
        if (Date.now() - askedAt >= RETRY_AFTER_MS) { askedAt = Date.now(); void refetch() }
      }, HOVER_DELAY_MS)
    }
    const leave = () => {
      over = null
      window.clearTimeout(timer)
      setNotice(null)
    }
    const onOut = (e: PointerEvent) => {
      if (!over || !e.composedPath().includes(over)) return
      if (e.relatedTarget instanceof Node && over.contains(e.relatedTarget)) return
      if (e.relatedTarget instanceof Node && cardRef.current?.contains(e.relatedTarget)) return
      if (noticeRef.current) closeAfterGraceOf(graceRef, leave)
      else leave()
    }
    leaveCardRef.current = (to) => {
      if (over && to instanceof Node && over.contains(to)) return
      closeAfterGraceOf(graceRef, leave)
    }
    closeCardRef.current = () => {
      cancelGraceOf(graceRef)
      leave()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      over = null
      window.clearTimeout(timer)
      setNotice(null)
    }
    document.addEventListener('pointerover', onOver, true)
    document.addEventListener('pointerout', onOut, true)
    document.addEventListener('keydown', onKey)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('pointerover', onOver, true)
      document.removeEventListener('pointerout', onOut, true)
      document.removeEventListener('keydown', onKey)
      cancelGraceOf(graceRef)
      leaveCardRef.current = () => setNotice(null)
      closeCardRef.current = () => setNotice(null)
    }
  }, [isError, providers.length, refetch])

  // Retry: dispose the provider's failed frame and load it again for this link.
  // The card stays (its button busy) until the new frame shows a card or fails.
  const retry = () => {
    const n = notice
    if (!n || n.retrying) return
    if (n.key === '') {
      setNotice({ ...n, retrying: true })
      void refetch().then((r) => {
        setNotice((cur) => (cur && cur.key === '' ? (r.isError ? { ...cur, retrying: false } : null) : cur))
      })
      return
    }
    const host = hostRef.current
    const a = anchorRef.current
    if (!host || !a || a.key !== n.key || !a.el.isConnected) { setNotice(null); return }
    failedAt.current.delete(n.key)
    setNotice({ ...n, retrying: true })
    host.hideUnpinned('other-provider', n.key)
    const frame = host.retryFrame(n.key, a.appName)
    const r = a.el.getBoundingClientRect()
    void host.show(frame, a.link, { left: r.left, top: r.top, width: r.width, height: r.height }).then((ok) => {
      if (!ok) {
        // Dropped (a newer hover, a provider refresh): Retry can be pressed again.
        setNotice((cur) => (cur?.key === n.key && cur.retrying ? { ...cur, retrying: false } : cur))
        return
      }
      failedAt.current.delete(n.key)
      // The app's card now stands where this one was: end the hover as if the
      // pointer had moved from the link into it, so the app is told `leave`.
      // Only this Retry's own hover: a newer one (even on the same provider) is left alone.
      if (anchorRef.current === a) closeCardRef.current()
      else setNotice((cur) => (cur?.key === n.key ? null : cur))
    })
  }

  return (
    <>
      {chip}
      {notice ? (
        <div
          ref={cardRef}
          role="group"
          aria-label={i18nT('components.linkPreview.load_failed')}
          data-testid="link-preview-failed"
          data-cause={notice.cause}
          className="fixed z-[2147482000] flex flex-col gap-2 rounded-lg border border-border bg-bg-elevated p-3 text-text shadow-lg"
          style={{ left: notice.left, top: notice.top, width: notice.width }}
          onPointerEnter={() => cancelGraceOf(graceRef)}
          onPointerLeave={(e) => leaveCardRef.current(e.relatedTarget)}
        >
          {/* No hand-off: the chat composer's unsent draft sits beside this card, and the hand-off navigates away from it. */}
          <ErrorNotice variant="inline" className="w-full" messageClassName="flex-1" message={i18nT(FAILED_LINE[notice.cause])} onDismiss={() => closeCardRef.current()} />
          <div className="flex justify-end">
            <Btn onClick={retry} disabled={notice.retrying} aria-busy={notice.retrying} data-testid="link-preview-retry">
              <RotateCw className="lucide-inline" aria-hidden="true" />
              {i18nT('components.linkPreview.retry')}
            </Btn>
          </div>
        </div>
      ) : null}
    </>
  )
}
