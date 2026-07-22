import { safeSetItem } from '../utils/safeStorage'
import { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { Maximize2, Minimize2, ExternalLink, Download, Star } from 'lucide-react'
import { IconButton, IconButtonGroup } from './ui'
import { useTheme } from '../hooks/useTheme'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import { effectiveWidgetSlug } from '../lib/widgetSlug'
import { api, ApiError } from '../api/client'
import { useQuery, useQueryClient } from '@tanstack/react-query'

const MIN_HEIGHT = 80

// Upper bound on the text a single widget action may pre-fill into the
// composer. A malicious LLM-emitted <script> can postMessage
// directly, so cap the dispatched text to keep it reviewable and prevent a
// widget from stuffing the composer with an oversized payload.
const MAX_WIDGET_ACTION_TEXT = 4000
// Height shrinks are deferred this long. A reload / Tailwind JIT reflow briefly
// reports a smaller height before the content settles; applying it immediately
// collapses-then-regrows the row, which accumulates into a growing gap at the
// bottom (see the height message handler).
const HEIGHT_SHRINK_DEBOUNCE_MS = 250

// A JUMP (↑ button / search / nav) lands several widgets near the viewport in
// the same commit. Building each widget's Tailwind iframe is a synchronous,
// frame-dropping burst, so a batch building together stacks their JITs into one
// long task. During a jump we therefore stagger each widget in the batch onto
// its own macrotask. Manual scroll has no jump signal, so widgets build
// immediately as they near the viewport (one at a time, amortized across
// frames) — building during the scroll means the content is ready by the time
// it stops, with no skeleton→iframe flash on settle.
const PROGRAMMATIC_BUILD_DELAY_MS = 450
let lastProgrammaticScrollAt = 0
if (typeof window !== 'undefined') {
  window.addEventListener('mc-chat-scroll-jump', () => { lastProgrammaticScrollAt = Date.now() })
}
const BUILD_STAGGER_MS = 120
let jumpBuildSlot = 0
let jumpBuildResetAt = 0

// Height cache is theme-independent: every entry in THEME_VAR_NAMES is a
// color var, never a size. If a length/size var is ever added to the list,
// include it in the cache key so heights don't get reused across themes.
// Persisted to localStorage so widgets don't jump on page reload.
const CACHE_KEY = 'mc-widget-heights'
const heightCache: Map<string, number> = (() => {
  try {
    const stored = localStorage.getItem(CACHE_KEY)
    return stored ? new Map(JSON.parse(stored)) : new Map()
  } catch { return new Map() }
})()

// Fallback height for a widget we've never measured. The first reveal of any
// widget must reserve SOME height before its iframe builds and reports the real
// one; if that reserve is wrong the row visibly corrects once (skeleton →
// iframe). Using the median of heights we've already cached (this session or a
// prior one, via localStorage) makes a brand-new widget reserve a typical
// height, so the one-time correction is small. A truly first-ever widget (empty
// cache) falls back to the fixed default. NOTE: this is why the correction only
// showed on a cache-cold browser (fresh Firefox/Safari) and went away after one
// view or a refresh — localStorage warms the cache.
const DEFAULT_WIDGET_HEIGHT = 200
function defaultWidgetHeight(): number {
  if (heightCache.size === 0) return DEFAULT_WIDGET_HEIGHT
  const vals = [...heightCache.values()].sort((a, b) => a - b)
  return vals[Math.floor(vals.length / 2)]
}

function persistHeightCache() {
  try {
    // Keep only last 200 entries to bound storage
    const entries = [...heightCache.entries()].slice(-200)
    safeSetItem(CACHE_KEY, JSON.stringify(entries))
  } catch (e) {
    // Best-effort persistence (quota / private-mode / serialize failures).
    // Surface it in dev so a persistent failure isn't completely invisible;
    // there's no recovery to attempt, the next update retries the write.
    // eslint-disable-next-line no-console -- intentional dev-only diagnostic
    if (import.meta.env.DEV) console.warn('widget height cache persist failed', e)
  }
}

// localStorage.setItem is synchronous and JSON.stringify'ing up to 200 entries
// is not free; writing it on every height update stalls the main thread. Batch
// writes so a burst of resizes (a widget settling, or several widgets mounting
// at once) persists at most once per window.
const HEIGHT_PERSIST_DEBOUNCE_MS = 1000
let persistTimer: ReturnType<typeof setTimeout> | null = null
function schedulePersistHeightCache() {
  if (persistTimer) return
  persistTimer = setTimeout(() => {
    persistTimer = null
    persistHeightCache()
  }, HEIGHT_PERSIST_DEBOUNCE_MS)
}

function contentHash(html: string): string {
  let h = 0
  for (let i = 0; i < html.length; i++) {
    h = ((h << 5) - h + html.charCodeAt(i)) | 0
  }
  return String(h)
}

function readThemeVars(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = sanitizeCssValue(computed.getPropertyValue(name))
    if (v) out[name] = v
  }
  return out
}

interface WidgetFrameProps {
  html: string
  title?: string
  /** Explicit slug attribute on `<mcwidget slug="...">`. When the agent
   * re-emits a previously-saved artifact it MUST include this attribute so
   * the impression binds to the same artifact. For brand-new emissions the
   * agent may omit it and we derive a stable slug from `messageTs +
   * widgetIndex` instead. */
  slug?: string
  /** Parent message timestamp. Threaded through from AssistantMessage so
   * widgets without an explicit slug get a stable, location-anchored
   * identity that survives refreshes and prevents save-then-refresh
   * duplicate creation. */
  messageTs?: string
  /** 0-based ordinal of this widget within the parent message. Two
   * `<mcwidget>` tags in the same message disambiguate by this index. */
  widgetIndex?: number
}

export default function WidgetFrame({ html, title = 'Widget', slug, messageTs, widgetIndex }: WidgetFrameProps) {
  // Re-read theme CSS vars whenever the resolved theme, active color theme,
  // or themeVersion counter changes. themeVersion is the trigger for
  // in-place custom-theme edits via the theme editor: the slug stays the
  // same but the injected CSS values change, so theme/colorTheme alone
  // wouldn't fire the memo. useTheme bumps themeVersion on every
  // loadCustomThemes completion and on every applyTheme.
  const { theme, colorTheme, themeVersion } = useTheme()
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)
  // Two-stage reveal. The IntersectionObserver marks the widget "near" the
  // viewport; the expensive iframe build (Tailwind CDN runtime + JIT, on the
  // parent's main thread) is normally done as soon as it's near — cheap for a
  // single widget during a manual scroll. But right after a chat JUMP we delay
  // it (see PROGRAMMATIC_BUILD_DELAY_MS) so a span full of widgets doesn't all
  // build in the same frame. `visible` is one-way false→true; the chat
  // virtualizer unmounts the whole row to actually free the iframe.
  const [visible, setVisible] = useState(false)
  const [near, setNear] = useState(false)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    // SSR / environments without IO: render eagerly.
    if (typeof IntersectionObserver === 'undefined') { setNear(true); return }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setNear(true)
          io.disconnect()
        }
      },
      // Mark as near a bit before it scrolls into view so a scroll pause has a
      // head start on building.
      { rootMargin: '400px 0px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [])

  useEffect(() => {
    if (visible || !near) return
    const now = Date.now()
    // Manual scroll (and tests): build immediately as the widget nears the
    // viewport, so it's ready by the time scrolling stops — no skeleton→iframe
    // flash on settle.
    const baseWait = lastProgrammaticScrollAt + PROGRAMMATIC_BUILD_DELAY_MS - now
    if (baseWait <= 0) {
      setVisible(true)
      return
    }
    // Jump path: stagger this batch so the widgets don't all JIT in one task.
    // A fresh batch (no jump within the last delay window) resets the counter.
    if (now > jumpBuildResetAt) jumpBuildSlot = 0
    const slot = jumpBuildSlot++
    jumpBuildResetAt = now + PROGRAMMATIC_BUILD_DELAY_MS
    const wait = baseWait + slot * BUILD_STAGGER_MS
    const id = setTimeout(() => setVisible(true), wait)
    return () => clearTimeout(id)
  }, [near, visible])
  const key = useMemo(() => contentHash(html), [html])
  const [height, setHeight] = useState(() => heightCache.get(key) ?? defaultWidgetHeight())
  // Mirror of `height` so the message handler (wired once per `key`) can
  // compare against the live value, plus a timer used to defer shrinks.
  const heightRef = useRef(height)
  heightRef.current = height
  const shrinkTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const srcdoc = useMemo(
    () => visible ? buildSrcdoc({ html, themeVars, mode: theme, includeHeightReporter: true }) : '',
    [html, themeVars, theme, visible],
  )

  // Blob URL — only created when visible
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  // Fade the iframe in once its document loads, so the reveal is a soft fade
  // instead of an abrupt blink-then-appear. Reset to false whenever a new blob
  // is built (first reveal, theme change, content rebuild) so each fresh render
  // fades in too.
  const [iframeLoaded, setIframeLoaded] = useState(false)

  useEffect(() => {
    if (!visible || !srcdoc) return
    setIframeLoaded(false)
    const blob = new Blob([srcdoc], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    setBlobUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [srcdoc, visible])

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      if (e.data?.type === 'mc-widget-height' && typeof e.data.height === 'number') {
        const h = Math.max(e.data.height, MIN_HEIGHT)
        // No-op when the clamped height is unchanged. An animated widget can
        // post the same height every frame; without this guard each report
        // re-ran applyHeight → persistHeightCache (synchronous localStorage
        // write), a per-frame main-thread stall that showed up as scroll jank.
        if (h === heightRef.current) {
          if (shrinkTimerRef.current) { clearTimeout(shrinkTimerRef.current); shrinkTimerRef.current = null }
          return
        }
        const applyHeight = (next: number) => {
          heightRef.current = next
          setHeight(next)
          heightCache.set(key, next)
          schedulePersistHeightCache()
        }
        // A pending shrink is always superseded by the newest reading.
        if (shrinkTimerRef.current) { clearTimeout(shrinkTimerRef.current); shrinkTimerRef.current = null }
        if (h > heightRef.current) {
          // Growth applies immediately.
          applyHeight(h)
        } else {
          // Defer shrinks. A reload / Tailwind JIT reflow briefly reports a
          // smaller height before the content settles; applying it at once
          // collapses-then-regrows the row, and at the bottom (where the
          // follow-pin and overflow-anchor both write scrollTop) that leaves a
          // residual gap which accumulates over repeated reloads. Only shrink
          // once the smaller height holds.
          shrinkTimerRef.current = setTimeout(() => {
            shrinkTimerRef.current = null
            applyHeight(h)
          }, HEIGHT_SHRINK_DEBOUNCE_MS)
        }
      }
      if (e.data?.type === 'mc-widget-action') {
 // SECURITY: a widget action can ONLY pre-fill the composer
        // (see the mc-widget-send handler in ChatPage) — it can never submit a
        // user-role turn on its own. We still validate/sanitize the shape here
        // because LLM-emitted <script> can postMessage directly (bypassing the
        // in-iframe isTrusted click guard), so an action must not be able to
        // inject a malformed or oversized payload into the composer.
        const action = typeof e.data.action === 'string' ? e.data.action.slice(0, 64) : ''
        if (!action) return
        const payload = e.data.payload && typeof e.data.payload === 'object' && !Array.isArray(e.data.payload)
          ? (e.data.payload as Record<string, unknown>)
          : {}
        let text = Object.keys(payload).length > 0
          ? `[UI] ${action}: ${JSON.stringify(payload)}`
          : `[UI] ${action}`
        if (text.length > MAX_WIDGET_ACTION_TEXT) text = text.slice(0, MAX_WIDGET_ACTION_TEXT) + '…'
        window.dispatchEvent(new CustomEvent('mc-widget-send', { detail: { text, action } }))
      }
    }
    window.addEventListener('message', handler)
    return () => {
      window.removeEventListener('message', handler)
      // The virtualizer can unmount this widget row (it leaves the window)
      // while a deferred shrink is still pending; clear it so it can't fire
      // applyHeight → setHeight / heightCache.set / persist after unmount.
      if (shrinkTimerRef.current) { clearTimeout(shrinkTimerRef.current); shrinkTimerRef.current = null }
    }
  }, [key])

  const openInNewTab = useCallback(() => {
    // Build the wrapper via DOM API instead of template literals: the browser
    // handles attribute escaping and HTML serialization, so LLM-generated
    // srcdoc/title can't break out of the document. The blob page sandboxes
    // the inner iframe so its origin doesn't grant LLM content access to the
    // parent app's cookies/storage.
    const doc = document.implementation.createHTMLDocument(title)
    const charsetMeta = doc.createElement('meta')
    charsetMeta.setAttribute('charset', 'utf-8')
    doc.head.insertBefore(charsetMeta, doc.head.firstChild)
    doc.body.style.margin = '0'
    doc.body.style.height = '100vh'
    const iframe = doc.createElement('iframe')
    iframe.setAttribute('sandbox', 'allow-scripts allow-popups allow-popups-to-escape-sandbox')
    iframe.setAttribute('srcdoc', srcdoc)
    iframe.style.width = '100%'
    iframe.style.height = '100%'
    iframe.style.border = 'none'
    doc.body.appendChild(iframe)

    const html = `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`
    const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    window.open(url, '_blank')
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }, [srcdoc, title])

  const downloadAsHtml = useCallback(() => {
    // Note: downloaded HTML runs with file:// origin when opened locally.
    // This is expected for an explicit download action — the user chose
    // to save the file. The content is LLM-generated, same as any code
    // the agent writes to disk.
    const blob = new Blob([srcdoc], { type: 'text/html' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${title.replace(/[^a-zA-Z0-9-_ ]/g, '')}.html`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(a.href), 60_000)
  }, [srcdoc, title])

  // Save the widget body as a persistent artifact. We send the *inner* HTML
  // (the user-visible widget body, not the wrapped srcdoc with theme blocks),
  // so the artifact stays portable across themes and renders identically when
  // opened on /artifacts/<slug>.

  // Determine the effective slug for this impression. Priority:
  //  1. Explicit `slug` attribute from the agent (used when re-emitting a
  //     known saved artifact — see artifacts skill).
  //  2. Derived from `messageTs + widgetIndex` — stable across refreshes,
  //     so saving once and refreshing doesn't create a duplicate.
  // Returns null only when neither is available (streaming/detached
  // widgets, or test fixtures); in that case bookmark is disabled.
  const effectiveSlug = useMemo(
    () => effectiveWidgetSlug({
      explicitSlug: slug,
      messageTs,
      widgetIndex,
    }),
    [slug, messageTs, widgetIndex],
  )
  // Probe whether this widget's artifact exists on the server. Cached via
  // React Query with a 5-min staleTime so repeated impressions / tab
  // refocuses don't each fire a network request. 404s are cached as
  // `false` (not retried) to avoid a 404 storm for unsaved widgets.
  const queryClient = useQueryClient()
  const savedProbe = useQuery({
    queryKey: ['artifact-saved', effectiveSlug],
    queryFn: async () => {
      try {
        // "Saved" means STARRED (pinned) — not merely existing. A widget
        // artifact can exist unpinned (e.g. created then unstarred), which
        // must render as not-in-library.
        const a = await api.artifact(effectiveSlug!)
        return !!(a && (a as { pinned?: boolean }).pinned)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return false
        throw e
      }
    },
    enabled: !!effectiveSlug,
    retry: false,
    staleTime: 5 * 60 * 1000,
    // Optimistic fill: explicit slug attr means agent re-emitted a saved
    // artifact — show bookmark filled before the probe resolves.
    placeholderData: slug ? true : undefined,
  })

  // Derive savedSlug from probe result
  const savedSlug = savedProbe.data === true
    ? effectiveSlug
    : savedProbe.data === false
      ? null
      : (slug ?? null)

  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Track mount status so async save/remove callbacks can skip side-effects
  // when the component has been unmounted mid-flight (e.g. user navigated
  // away, or the chat scrolled the widget out of view between the bookmark
  // click and the API response).
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const saveAsArtifact = useCallback(async () => {
    if (saving || savedSlug || !effectiveSlug) return
    // Atomic one-click save with a deterministic slug — POST goes to the
    // exact slug for this widget impression. If the artifact already
    // exists at that slug (e.g. a rapid double-click race, or the user
    // saved in another tab and the verify-on-mount hasn't reconciled
    // yet), the server returns 409 and we treat that as "already saved":
    // sync local state to match server truth, no error shown.
    const name = title && title !== 'Widget' ? title : 'Widget'
    setSaving(true)
    setSaveError(null)
    try {
      // Create the artifact if it doesn't exist yet (409 = already there),
      // then PIN it so it shows in the Starred library — "star" = create + pin.
      try {
        await api.createArtifact({
          name,
          content: html,
          kind: 'widget',
          source: 'chat',
          slug: effectiveSlug,
        })
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e
      }
      await api.setArtifactPinned(effectiveSlug, true)
      queryClient.setQueryData(['artifact-saved', effectiveSlug], true)
    } catch (e) {
      if (mountedRef.current) {
        setSaveError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }, [html, title, saving, savedSlug, effectiveSlug, queryClient])

  const removeArtifact = useCallback(async () => {
    if (saving || !savedSlug) return
    // Un-star = unpin (metadata-only), NOT delete — preserves the artifact and
    // its version history. The Artifacts library page handles permanent delete.
    setSaving(true)
    setSaveError(null)
    try {
      await api.setArtifactPinned(savedSlug, false)
      queryClient.setQueryData(['artifact-saved', effectiveSlug], false)
    } catch (e) {
      // 404 → already gone; reconcile to the empty state silently.
      if (e instanceof ApiError && e.status === 404) {
        queryClient.setQueryData(['artifact-saved', effectiveSlug], false)
      } else if (mountedRef.current) {
        setSaveError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }, [savedSlug, saving, effectiveSlug, queryClient])

  const toggleArtifact = savedSlug ? removeArtifact : saveAsArtifact

  return (
    <div
      ref={containerRef}
      className={`group my-2 transition-colors ${expanded ? 'fixed inset-4 z-[100] rounded-xl border border-border bg-card overflow-hidden shadow-2xl' : ''}`}
    >
      {!visible ? (
        /* Skeleton — mirrors the visible layout (same header bar + a reserved
           body of the cached iframe height) so the row keeps EXACTLY the same
           height when the iframe later mounts. Reserving only the iframe height
           (without the header) made the row grow by the header's height on
           becoming visible, which showed as a scroll-up "jump" as each widget
           entered from the top. */
        <>
          <div className="flex items-center justify-between px-3 py-2">
            <span className="text-[13px] font-medium text-muted truncate">{title}</span>
          </div>
          <div aria-hidden style={{ height }} />
        </>
      ) : (<>
      <div className={`flex items-center justify-between px-3 py-2 ${expanded ? 'border-b border-border bg-bg-elevated' : ''}`}>
        <span className="text-[13px] font-medium text-text truncate">
          {savedSlug ? (
            <a
              href={`/artifacts/${savedSlug}`}
              className="text-text hover:text-accent hover:underline"
              title={`Open artifact "${savedSlug}"`}
            >{title}</a>
          ) : (
            title
          )}
          {saveError && <span className="ml-2 text-[12px] text-danger" title={saveError}>save failed</span>}
        </span>
        <IconButtonGroup reveal={!expanded}>
          <IconButton
            variant={savedSlug ? 'active' : 'default'}
            onClick={toggleArtifact}
            disabled={saving || !effectiveSlug}
            className={saving ? 'cursor-wait' : ''}
            title={
              !effectiveSlug
                ? 'Cannot star: widget has no slug or message context'
                : savedSlug
                  ? `Starred as "${savedSlug}" — click to remove`
                  : 'Star as artifact'
            }
            aria-label={savedSlug ? `Remove artifact ${savedSlug} from library` : 'Star as artifact'}
          >
            <Star size={12} fill={savedSlug ? 'currentColor' : 'none'} />
          </IconButton>
          <IconButton onClick={downloadAsHtml} title="Download as HTML" aria-label="Download as HTML">
            <Download size={12} />
          </IconButton>
          <IconButton onClick={openInNewTab} title="Open in new tab" aria-label="Open in new tab">
            <ExternalLink size={12} />
          </IconButton>
          <IconButton onClick={() => setExpanded(!expanded)} title={expanded ? 'Minimize' : 'Expand'} aria-label={expanded ? 'Minimize' : 'Expand'}>
            {expanded ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
          </IconButton>
        </IconButtonGroup>
      </div>

      {/* onLoad is a frame-load lifecycle handler (fade the iframe in), not a */}
      {/* user interaction; the rule flags onLoad regardless. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      {blobUrl && <iframe
        ref={iframeRef}
        src={blobUrl}
        onLoad={() => setIframeLoaded(true)}
        sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
        className="w-full border-none bg-card transition-opacity duration-200 ease-out motion-reduce:transition-none"
        style={{ height: expanded ? 'calc(100% - 36px)' : height, opacity: iframeLoaded ? 1 : 0 }}
        title={title}
      />}

      {expanded && (
        // Click-outside backdrop to collapse; keyboard users collapse via the
        // Minimize toolbar button above, which sets the same state.
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
        <div className="fixed inset-0 bg-black/40 -z-10" onClick={() => setExpanded(false)} />
      )}
      </>)}
    </div>
  )
}
