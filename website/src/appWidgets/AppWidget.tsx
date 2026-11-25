/**
 * An app widget in a chat answer: `<mcwidget use="<app>:<id>">{json}</mcwidget>`.
 *
 * The named app's module runs in the shared sandboxed app frame
 * (`appFrame/sandboxFrame.ts`, point `widgets`). The frame is handed the parsed
 * payload of this one tag and the theme, nothing else. The point is offline
 * (`apps/widget_frame.py`): the mint gives no capability, so the module reaches no
 * backend, and none is ever renewed.
 *
 * Every way the widget cannot be drawn ends in the same fallback card, which says
 * why and keeps the payload readable: the app is not installed or not enabled, it
 * declares no such widget, the payload is not one JSON object within the cap, the
 * mint is refused, or the frame does not say `ready` in time.
 *
 * @module appWidgets/AppWidget
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useNearViewport } from '../hooks/useNearViewport'
import { useFrameLinkBridge } from '../hooks/useFrameLinkBridge'
import { finite, freshRate, isRelayMessage, messageBytes, readTagged, takeMessage } from '../appFrame/frameProtocol'
import {
  createSandboxedFrame,
  currentTheme,
  loadMintedFrame,
  mintAppFrame,
  postToFrame,
  type Minted,
} from '../appFrame/sandboxFrame'
import {
  parseWidgetProps,
  parseWidgetUse,
  resolveAppWidget,
  type ResolvedAppWidget,
} from './appWidgetRegistry'

/** The frame point these widgets are served under (`WIDGETS_POINT`). */
export const WIDGETS_POINT = 'widgets'
/** A frame that has not said `ready` by then failed. */
export const READY_TIMEOUT_MS = 10_000
/** The frame's height before it reports its own, and the bounds of what it may report. */
export const DEFAULT_HEIGHT = 120
export const MIN_HEIGHT = 40
export const MAX_HEIGHT = 800
/** Largest message the frame may send, as JSON text. */
const MAX_FRAME_MESSAGE_BYTES = 4096

export type FallbackReason = 'bad-use' | 'no-app' | 'no-widget' | 'too-large' | 'not-object' | 'failed'

/** Overridable in tests: how a widget frame is minted. */
type Mint = (appName: string, widgetId: string) => Promise<Minted>
const defaultMint: Mint = (appName, widgetId) => mintAppFrame(appName, WIDGETS_POINT, widgetId)
let mintForWidgets: Mint = defaultMint
export function setAppWidgetMintForTest(mint?: Mint): void {
  mintForWidgets = mint ?? defaultMint
}

function selectApps(d: unknown): unknown[] {
  const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
  return Array.isArray(apps) ? apps : []
}

export default function AppWidget({ use, body, title }: {
  /** The tag's `use` attribute, as written. */
  use: string
  /** The tag's body, as written: the JSON payload. */
  body: string
  /** The tag's `title`, or the block's default title. */
  title?: string
}) {
  const ref = useMemo(() => parseWidgetUse(use), [use])
  const parsed = useMemo(() => parseWidgetProps(body), [body])
  const { data: apps, isPending } = useQuery({
    queryKey: ['apps'],
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select: selectApps,
  })
  const resolved = useMemo(
    () => (ref && apps ? resolveAppWidget(apps as never[], ref) : null),
    [ref, apps],
  )
  const widget = resolved && typeof resolved === 'object' ? resolved : null
  const [failed, setFailed] = useState(false)

  let reason: FallbackReason | null = null
  if (!ref) reason = 'bad-use'
  else if (!parsed.ok) reason = parsed.reason
  else if (typeof resolved === 'string') reason = resolved
  // The app list failed to load, so the widget cannot be found.
  else if (!isPending && !resolved) reason = 'failed'
  else if (failed) reason = 'failed'

  const heading = title && title !== 'Widget' ? title : (widget?.title || use)
  if (reason) return <AppWidgetFallback title={heading} use={use} reason={reason} body={body} appLabel={widget?.appLabel} />
  if (!widget || !parsed.ok) {
    // The app list is still loading.
    return <section className="my-2 rounded-lg border border-border bg-bg-elevated/40 px-3 py-2 text-[12px] text-muted" aria-busy="true" data-testid="app-widget-loading">{heading}</section>
  }
  // Keyed by the tag: a frame renders its payload once, so a changed body is a new frame.
  return <AppWidgetFrame key={`${use}\n${body}`} widget={widget} props={parsed.props} heading={heading} onFailed={() => setFailed(true)} />
}

function AppWidgetFrame({ widget, props, heading, onFailed }: {
  widget: ResolvedAppWidget
  props: Record<string, unknown>
  heading: string
  onFailed: () => void
}) {
  const boxRef = useRef<HTMLElement | null>(null)
  const slotRef = useRef<HTMLDivElement | null>(null)
  const frameRef = useRef<HTMLIFrameElement | null>(null)
  const near = useNearViewport(boxRef)
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  const onFailedRef = useRef(onFailed)
  onFailedRef.current = onFailed
  const chip = useFrameLinkBridge([frameRef])

  useEffect(() => {
    const slot = slotRef.current
    if (!near || !slot) return
    const label = i18nT('components.appWidget.frame_label', { app: widget.appLabel, title: heading })
    const el = createSandboxedFrame(label)
    el.dataset.appWidgetFrame = `${widget.appName}:${widget.widgetId}`
    Object.assign(el.style, { width: '100%', height: '100%', border: '0', display: 'block', background: 'transparent', colorScheme: 'normal' })
    frameRef.current = el
    const rate = freshRate()
    let loading = true
    let disposed = false
    const fail = () => {
      if (disposed) return
      loading = false
      onFailedRef.current()
    }
    const readyTimer = window.setTimeout(() => { if (loading) fail() }, READY_TIMEOUT_MS)
    const onMessage = (e: MessageEvent) => {
      if (!e.source || e.source !== el.contentWindow) return
      if (isRelayMessage(e.data)) return
      // Bounded: an oversized message, or one past the burst allowance, is dropped unread.
      if (!takeMessage(rate)) return
      if (messageBytes(e.data) > MAX_FRAME_MESSAGE_BYTES) return
      const m = readTagged(e.data)
      if (!m) return
      switch (m.type) {
        case 'ready':
          if (!loading) return
          loading = false
          window.clearTimeout(readyTimer)
          postToFrame(el, { type: 'widget.render', props, theme: currentTheme(), widgetId: widget.widgetId })
          return
        case 'widget.resize': {
          const h = Math.round(finite(m.height))
          if (h > 0) setHeight(Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, h)))
          return
        }
        case 'failed': fail(); return
        // `capabilityExpired` is not answered: the point is offline.
        default: return
      }
    }
    window.addEventListener('message', onMessage)
    // The dashboard's theme is a class or data attribute on the root element.
    let theme = currentTheme()
    const themeWatch = new MutationObserver(() => {
      const next = currentTheme()
      if (next === theme) return
      theme = next
      postToFrame(el, { type: 'theme', theme: next })
    })
    themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] })
    mintForWidgets(widget.appName, widget.widgetId).then((minted) => {
      if (disposed) return
      // The point is offline; a capability, if one ever arrived, is never handed over.
      loadMintedFrame(el, slot, { ...minted, capability: null }, {
        theme: currentTheme,
        onNavigatedAway: () => {
          // eslint-disable-next-line no-console -- an app's frame navigating away is otherwise silent
          console.warn(`[app-widget] ${widget.appName}:${widget.widgetId}: the frame navigated away and was closed`)
          fail()
        },
      })
    }, () => {
      window.clearTimeout(readyTimer)
      fail()
    })
    return () => {
      disposed = true
      postToFrame(el, { type: 'dispose' })
      window.clearTimeout(readyTimer)
      themeWatch.disconnect()
      window.removeEventListener('message', onMessage)
      el.remove()
      frameRef.current = null
    }
    // One frame per mounted widget: a finished tag's payload does not change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [near])

  return (
    <section
      ref={boxRef}
      className="my-2 overflow-hidden rounded-lg border border-border bg-bg-elevated/40"
      aria-label={i18nT('components.appWidget.frame_label', { app: widget.appLabel, title: heading })}
      data-testid="app-widget"
    >
      <div className="flex items-center justify-between px-3 pt-2 text-[12px]">
        <span className="truncate font-semibold text-muted">{heading}</span>
        <span className="shrink-0 text-[11px] text-muted">{widget.appLabel}</span>
      </div>
      <div ref={slotRef} style={{ height }} data-testid="app-widget-slot" />
      {chip}
    </section>
  )
}

/** The fallback card's one-line reason. Static keys, so the catalog check sees each one. */
function fallbackText(reason: FallbackReason, app: string, use: string): string {
  switch (reason) {
    case 'bad-use': return i18nT('components.appWidget.reason_bad_use', { use })
    case 'no-app': return i18nT('components.appWidget.reason_no_app', { app })
    case 'no-widget': return i18nT('components.appWidget.reason_no_widget', { app, use })
    case 'too-large': return i18nT('components.appWidget.reason_too_large')
    case 'not-object': return i18nT('components.appWidget.reason_not_object')
    case 'failed': return i18nT('components.appWidget.reason_failed', { app })
  }
}

export function AppWidgetFallback({ title, use, reason, body, appLabel }: {
  title: string
  use: string
  reason: FallbackReason
  body: string
  appLabel?: string
}) {
  const app = appLabel || use.split(':', 1)[0] || use
  const why = fallbackText(reason, app, use)
  return (
    <section className="my-2 rounded-lg border border-border bg-bg-elevated/40 px-3 py-2" role="note" data-testid="app-widget-fallback" data-reason={reason}>
      <div className="text-[12px] font-semibold text-muted truncate">{title}</div>
      <p className="mt-1 text-[12px] text-fg">{why}</p>
      <details className="mt-1 text-[12px]">
        <summary className="cursor-pointer text-muted">{i18nT('components.appWidget.show_data')}</summary>
        <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded bg-bg px-2 py-1 font-mono text-[11px] text-fg">{body.trim()}</pre>
      </details>
    </section>
  )
}
