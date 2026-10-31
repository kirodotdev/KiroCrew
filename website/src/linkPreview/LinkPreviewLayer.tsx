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
import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import { activeElementIsEditable, isEditableTarget } from '../utils/editableTarget'
import { useFrameLinkBridge } from '../hooks/useFrameLinkBridge'
import { i18nT } from '../i18n/t'
import { resolveLinkPreviews, providerFor, type ResolvedLinkPreview } from './linkPreviewRegistry'
import { currentTheme } from '../appFrame/sandboxFrame'
import { FrameHost, mintFrame, type FrameHostOptions } from './linkPreviewFrames'
import { resolveTextPreview, setLinkPreviewTextPatterns, setTextPatternsOff } from './textPatterns'

const HOVER_DELAY_MS = 300
/** A provider whose frame failed to load or activate is tried again on a hover this long after. */
export const RETRY_AFTER_MS = 30_000

/** The URL a provider is shown by default: no query and no fragment, which can
 *  carry signed tokens or other secrets. `fullUrl` in the manifest opts out. */
export function withoutQuery(href: string): string {
  try {
    const u = new URL(href)
    return `${u.origin}${u.pathname}`
  } catch {
    return ''
  }
}

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
  // key -> when its frame last failed
  const failedAt = useRef(new Map<string, number>())
  // The hover whose card could not be shown: the frame failed to load or the
  // module threw. Drawn below the hovered element until the pointer leaves it.
  const [notice, setNotice] = useState<{ key: string; left: number; top: number } | null>(null)
  // The live frames, for the link bridge: a card's links open through it.
  const [frameRefs, setFrameRefs] = useState<RefObject<HTMLIFrameElement | null>[]>([])
  const hostRef = useRef<FrameHost | null>(null)
  const hideAllRef = useRef<(reason: string) => void>(() => {})
  // The hover in progress and its provider: a frame's failure is shown only under
  // its own hover, and nothing is shown once the user has left it.
  const anchorRef = useRef<{ el: HTMLElement; key: string } | null>(null)

  const chip = useFrameLinkBridge(frameRefs)

  useEffect(() => {
    const host = new FrameHost({
      mint: (app, frameId, providerId) => mintForFrames(app, frameId, providerId),
      theme: currentTheme,
      onFailed: (frame, why) => {
        // eslint-disable-next-line no-console -- an app's link preview failing to load is otherwise silent
        console.error(`[link-preview] ${frame.key} failed to load: ${why}`)
        failedAt.current.set(frame.key, Date.now())
        const a = anchorRef.current
        if (a && a.key === frame.key && a.el.isConnected) {
          const r = a.el.getBoundingClientRect()
          setNotice({ key: frame.key, left: Math.max(8, Math.min(r.left, window.innerWidth - 320)), top: r.bottom + 6 })
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
      if (!hit || hit.a === current?.a) return
      const key = hit.p.key
      const lastFailed = failedAt.current.get(key)
      current = { a: hit.a, key }
      anchorRef.current = { el: hit.a, key }
      window.clearTimeout(timer)
      if (lastFailed !== undefined && Date.now() - lastFailed < RETRY_AFTER_MS) {
        // A failed frame is not final, but it is not retried on every hover either.
        timer = window.setTimeout(() => {
          if (current?.a !== hit.a || !hit.a.isConnected) return
          const r = hit.a.getBoundingClientRect()
          setNotice({ key, left: Math.max(8, Math.min(r.left, window.innerWidth - 320)), top: r.bottom + 6 })
        }, HOVER_DELAY_MS)
        return
      }
      // Create the frame during the delay, so the first card is not late by the load.
      const frame = host.hoverFrame(key, hit.p.appName)
      const a = hit.a
      const link = hit.url ? { url: hit.url } : { text: hit.text }
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
    const onOut = (e: PointerEvent) => {
      if (!current) return
      const to = e.relatedTarget instanceof Element ? e.relatedTarget.closest('a[href], [data-link-preview-text]') : null
      if (to === current.a) return
      if (e.composedPath().includes(current.a)) {
        setNotice(null)
        window.clearTimeout(timer)
        const key = current.key
        current = null
        anchorRef.current = null
        host.leave(key)
      }
    }
    const hideAll = (reason: string) => {
      setNotice(null)
      window.clearTimeout(timer)
      current = null
      anchorRef.current = null
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
    // card happens in its frame and never reaches this window.
    const onScroll = () => hideAll('scroll')
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
      // A card shown through these listeners must not outlive them.
      host.hideUnpinned('disabled')
      hideAllRef.current = () => {}
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
      if (!a || a === over) return
      over = a
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        if (over !== a || !a.isConnected) return
        const r = a.getBoundingClientRect()
        setNotice({ key: '', left: Math.max(8, Math.min(r.left, window.innerWidth - 320)), top: r.bottom + 6 })
        if (Date.now() - askedAt >= RETRY_AFTER_MS) { askedAt = Date.now(); void refetch() }
      }, HOVER_DELAY_MS)
    }
    const onOut = (e: PointerEvent) => {
      if (!over || !e.composedPath().includes(over)) return
      if (e.relatedTarget instanceof Node && over.contains(e.relatedTarget)) return
      over = null
      window.clearTimeout(timer)
      setNotice(null)
    }
    document.addEventListener('pointerover', onOver, true)
    document.addEventListener('pointerout', onOut, true)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('pointerover', onOver, true)
      document.removeEventListener('pointerout', onOut, true)
    }
  }, [isError, providers.length, refetch])

  return (
    <>
      {chip}
      {notice ? (
        <div className="fixed z-[2147482000] w-[312px]" style={{ left: notice.left, top: notice.top }} data-testid="link-preview-failed">
          {/* No hand-off: the chat composer's unsent draft sits beside this notice, and the hand-off navigates away from it. */}
          <ErrorNotice variant="inline" message={i18nT('components.linkPreview.load_failed')} onDismiss={() => setNotice(null)} />
        </div>
      ) : null}
    </>
  )
}
