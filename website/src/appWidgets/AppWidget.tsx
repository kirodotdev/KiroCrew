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
 * why and keeps the payload readable: the app is not installed, it is turned off,
 * it declares no such widget, the payload is not one JSON object within the cap,
 * the app list does not load, the mint is refused, or the frame does not say
 * `ready` in time. The first two link to the app's page, where the user can
 * install it or turn it on. The last three are load failures, so they render
 * through `ErrorNotice`.
 *
 * The frame opens a link only through the host, and refuses one the reply's own
 * text draws as a Blocked link chip, or any link while those records are pending,
 * as `WidgetFrame` does.
 *
 * @module appWidgets/AppWidget
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useInRouterContext, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import { useGuardedLeave } from '../components/NavigationLeaveGuard'
import { blockedLinkFor, type BlockedLink } from '../components/blockedLinkRecords'
import { withBase } from '../lib/basePath'
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
  appLabelFor,
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

export type FallbackReason =
  | 'bad-use' | 'not-installed' | 'app-off' | 'no-widget' | 'too-large' | 'not-object'
  | 'apps-unavailable' | 'failed'

const NO_BLOCKED: readonly BlockedLink[] = []

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

export default function AppWidget({ use, body, title, blockedLinks = NO_BLOCKED, linksPending = false }: {
  /** The tag's `use` attribute, as written. */
  use: string
  /** The tag's body, as written: the JSON payload. */
  body: string
  /** The tag's `title`, or the block's default title. */
  title?: string
  /** The reply's blocked-link records: the frame opens no link that matches one. */
  blockedLinks?: readonly BlockedLink[]
  /** The reply's records are not known yet, so the frame opens no web link. */
  linksPending?: boolean
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
  else if (!isPending && !resolved) reason = 'apps-unavailable'
  else if (failed) reason = 'failed'

  const heading = title && title !== 'Widget' ? title : (widget?.title || use)
  if (reason) {
    const appLabel = widget?.appLabel ?? (ref && apps ? appLabelFor(apps as never[], ref.appName) : undefined)
    return <AppWidgetFallback title={heading} use={use} reason={reason} body={body} appLabel={appLabel} />
  }
  if (!widget || !parsed.ok) {
    // The app list is still loading.
    return <section className="my-2 rounded-lg border border-border bg-bg-elevated/40 px-3 py-2 text-[12px] text-muted" aria-busy="true" data-testid="app-widget-loading">{heading}</section>
  }
  // Keyed by the tag: a frame renders its payload once, so a changed body is a new frame.
  return (
    <AppWidgetFrame
      key={`${use}\n${body}`}
      widget={widget}
      props={parsed.props}
      heading={heading}
      blockedLinks={blockedLinks}
      linksPending={linksPending}
      onFailed={() => setFailed(true)}
    />
  )
}

function AppWidgetFrame({ widget, props, heading, blockedLinks, linksPending, onFailed }: {
  widget: ResolvedAppWidget
  props: Record<string, unknown>
  heading: string
  blockedLinks: readonly BlockedLink[]
  linksPending: boolean
  onFailed: () => void
}) {
  const boxRef = useRef<HTMLElement | null>(null)
  const slotRef = useRef<HTMLDivElement | null>(null)
  const frameRef = useRef<HTMLIFrameElement | null>(null)
  const near = useNearViewport(boxRef)
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  const onFailedRef = useRef(onFailed)
  onFailedRef.current = onFailed
  // A link the reply's own text draws as a Blocked link chip opens nothing, and
  // no web link opens while the reply's records are pending.
  const refuseLink = useCallback(
    (url: string) => linksPending || blockedLinkFor(url, blockedLinks) !== null,
    [linksPending, blockedLinks],
  )
  const chip = useFrameLinkBridge([frameRef], undefined, undefined, undefined, refuseLink)

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
        <span className="shrink-0 text-[11px] text-muted">{i18nT('components.appWidget.from_app', { app: widget.appLabel })}</span>
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
    case 'not-installed': return i18nT('components.appWidget.reason_not_installed', { app })
    case 'app-off': return i18nT('components.appWidget.reason_app_off', { app })
    // `use` parsed as `<app>:<id>`, so the widget's own id follows the one colon.
    case 'no-widget': return i18nT('components.appWidget.reason_no_widget', { app, widget: use.slice(use.indexOf(':') + 1) })
    case 'too-large': return i18nT('components.appWidget.reason_too_large')
    case 'not-object': return i18nT('components.appWidget.reason_not_object')
    case 'apps-unavailable': return i18nT('components.appWidget.reason_apps_unavailable')
    case 'failed': return i18nT('components.appWidget.reason_failed', { app })
  }
}

function isPlainPrimaryClick(e: MouseEvent<HTMLAnchorElement>): boolean {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
}

const ACTION_CLASS = 'mt-1 inline-block text-[12px] text-accent hover:underline'

/** A link to the app's page, where the user installs it or turns it on. */
function AppPageLink({ appName, label }: { appName: string; label: string }) {
  const to = `/apps/detail/${encodeURIComponent(appName)}`
  // A reply drawn outside the dashboard's router (an app's own page) has no
  // in-place navigation, so the app's page opens in a new tab.
  if (!useInRouterContext()) {
    return <a href={withBase(to)} target="_blank" rel="noopener noreferrer" className={ACTION_CLASS} data-testid="app-widget-app-link">{label}</a>
  }
  return <GuardedAppPageLink to={to} label={label} />
}

function GuardedAppPageLink({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  return (
    <Link
      to={to}
      className={ACTION_CLASS}
      data-testid="app-widget-app-link"
      onClick={(e) => {
        // Leaving the chat unmounts it, so the page's leave check is asked first,
        // as SettingsLink does. A modified or middle click opens a new tab.
        if (!isPlainPrimaryClick(e)) return
        e.preventDefault()
        guardedLeave(() => navigate(to), to)
      }}
    >
      {label}
    </Link>
  )
}

export function AppWidgetFallback({ title, use, reason, body, appLabel }: {
  title: string
  use: string
  reason: FallbackReason
  body: string
  appLabel?: string
}) {
  const appName = use.split(':', 1)[0] || use
  const app = appLabel || appName
  const why = fallbackText(reason, app, use)
  // The user fixes a missing or turned-off app on its page. Every other reason is
  // a bad draw or a failed load, which the agent can redraw or explain.
  const appMissing = reason === 'not-installed' || reason === 'app-off'
  return (
    <section className="my-2 rounded-lg border border-border bg-bg-elevated/40 px-3 py-2" role="note" data-testid="app-widget-fallback" data-reason={reason}>
      <div className="text-[12px] font-semibold text-muted truncate">{title}</div>
      {appMissing
        ? <p className="mt-1 text-[12px] text-text-strong">{why}</p>
        // The rest of the chat works and the payload stays below, so the loss is partial.
        : <ErrorNotice impact="partial" className="mt-1" message={why} askAgent testId="app-widget-error" />}
      {/* The labels say the link opens the app's page; nothing is installed or turned on from here. */}
      {reason === 'not-installed' && <AppPageLink appName={appName} label={i18nT('components.appWidget.open_to_install', { app })} />}
      {reason === 'app-off' && <AppPageLink appName={appName} label={i18nT('components.appWidget.open_to_turn_on', { app })} />}
      <details className="mt-1 text-[12px]">
        <summary className="cursor-pointer text-muted">{i18nT('components.appWidget.show_data')}</summary>
        <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded bg-bg px-2 py-1 font-mono text-[11px] text-text-strong">{body.trim()}</pre>
      </details>
    </section>
  )
}
