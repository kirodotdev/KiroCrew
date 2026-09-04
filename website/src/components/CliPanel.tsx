import { useEffect, useLayoutEffect, useCallback, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { useMutation } from '@tanstack/react-query'
import { MessageSquarePlus, Copy, Check, PlugZap } from 'lucide-react'
import { ensureTerminalConnection, disposeTerminalConnection, getTerminalCwd, useTerminalConnStatus, useTerminalManualRetry, retryTerminalConnection } from '../utils/terminalRegistry'
import { getTerminalFont, resolveTerminalFontFamily, subscribeTerminalFont } from '../hooks/useTerminalFont'
import { ansiPaletteFromVars } from '../utils/terminalPalette'
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'
import { useTerminalTouchSelection, type TouchSelectStatus } from '../hooks/useTerminalTouchSelection'
import TerminalCompletion from './TerminalCompletion'
import TerminalKeyBar from './TerminalKeyBar'

import { i18nT } from '../i18n/t'
/* ── Per-session xterm instance cache ──
 * Keyed by PTY session id. Instances persist across tab switches / chat
 * switches (so scrollback + cursor survive), and are only torn down when the
 * owning terminal TAB is closed (disposeTerminalSession). */
const termCache = new Map<string, { term: Terminal; fit: FitAddon }>()

/* ── Terminal theme from CSS custom properties ── */
function getTermTheme() {
  const style = getComputedStyle(document.documentElement)
  const read = (name: string) => style.getPropertyValue(name)
  return {
    background:          style.getPropertyValue('--bg').trim()            || '#1e1e2e',
    foreground:          style.getPropertyValue('--text').trim()          || '#cdd6f4',
    cursor:              style.getPropertyValue('--accent').trim()        || '#89b4fa',
    selectionBackground: style.getPropertyValue('--accent-subtle').trim() || '#313244',
    // The 16 ANSI entries: without them xterm renders ANSI-coloured output with
    // its own palette, which ignores the theme entirely.
    ...ansiPaletteFromVars(read),
  }
}

/** Refresh theme on all cached terminals (called on theme change). */
function refreshTermThemes() {
  const theme = getTermTheme()
  for (const { term } of termCache.values()) {
    term.options.theme = theme
  }
}

/* ── Theme observer: a single module-level observer keeps every cached
 * terminal's colours in sync with the app theme. Initialised once on the
 * first terminal mount (multiple terminal tabs must not each spawn one). */
let _themeObserver: MutationObserver | null = null
let _themeRaf = 0
/** Coalesce multiple theme signals into one refresh, after the CSSOM settles. */
function scheduleTermThemeRefresh() {
  // Replace the pending frame rather than deferring to it. A plain
  // `if (_themeRaf) return` latch assumes every handle it stores is eventually
  // cleared by its own callback, and requestAnimationFrame does not promise
  // that: an implementation may hand back a live handle whose callback never
  // runs (happy-dom returns a truthy `{}` when the window is closed or a
  // timer-loop limit trips, and a browser drops queued frames for a page in the
  // back/forward cache). One such handle latches the guard truthy forever and
  // every later theme signal is dropped, so the terminal silently stops
  // tracking the app theme for the life of the page.
  //
  // Cancel-and-reschedule coalesces exactly as well -- a burst of signals still
  // yields one refresh, because each cancels the last -- while making a
  // non-firing handle merely stale instead of permanently blocking.
  if (_themeRaf) cancelAnimationFrame(_themeRaf)
  _themeRaf = requestAnimationFrame(() => { _themeRaf = 0; refreshTermThemes() })
}
/** Whether a batch of mutation records is one of the two theme signals.
 *
 * Split out of the observer callback so the CLASSIFICATION can be asserted directly.
 * Driving it through a live MutationObserver in a test means asserting on happy-dom's
 * record DELIVERY, which is bistable in this suite -- the same file passes under
 * `--coverage` and fails without it -- so a test written that way reports on the
 * environment rather than on this rule.
 */
export function isThemeSignal(records: MutationRecord[]): boolean {
  for (const r of records) {
    // (1) a built-in theme / mode swap flips <html data-theme>.
    if (r.type === 'attributes') return true
    // (2) a CUSTOM theme's vars only resolve once useTheme injects its <style>.
    for (const n of r.addedNodes) {
      if (n instanceof HTMLStyleElement && n.id.startsWith('mc-custom-theme-')) return true
    }
  }
  return false
}

function ensureThemeObserver() {
  if (_themeObserver || typeof document === 'undefined') return
  // A terminal's xterm colours are a construction-time snapshot (canvas, not
  // CSS), so they must be re-read on TWO distinct theme signals:
  //  (1) built-in themes / mode swaps flip <html data-theme> — an attribute change;
  //  (2) CUSTOM themes only resolve their vars once useTheme injects a
  //      <style id="mc-custom-theme-*"> into <head> (async, after the theme query
  //      loads). data-theme's VALUE doesn't change then, so an attribute-only
  //      observer misses it and the terminal stays on the boot-default palette.
  // The attribute filter catches (1); watching <head> childList catches (2).
  _themeObserver = new MutationObserver((records) => {
    if (isThemeSignal(records)) scheduleTermThemeRefresh()
  })
  _themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  _themeObserver.observe(document.head, { childList: true })
}

/** Test-only: release the document-scoped observer and any queued frame. */
export function __resetTerminalThemeSyncForTests(): void {
  if (_themeRaf) cancelAnimationFrame(_themeRaf)
  _themeRaf = 0
  _themeObserver?.disconnect()
  _themeObserver = null
}

/* ── Terminal font sync ──
 * Push the app-wide terminal font preference (useTerminalFont) onto every
 * cached xterm instance when it changes. Font family and size are canvas cell
 * metrics, so after updating the options each terminal must re-measure and
 * refit — remeasureAndFit toggles fontFamily to force xterm's CharSizeService
 * re-measure (see its doc) and no-ops on hidden panes. rAF-coalesced so rapid
 * font-size stepper clicks refit once. */
function applyTerminalFontToAll(): void {
  const font = getTerminalFont()
  const fontFamily = resolveTerminalFontFamily(font.fontFamily)
  for (const { term, fit } of termCache.values()) {
    term.options.fontFamily = fontFamily
    term.options.fontSize = font.fontSize
    remeasureAndFit(term, fit)
  }
}

let _fontRaf = 0
function scheduleTerminalFontApply(): void {
  // Same latch hazard as scheduleTermThemeRefresh -- see the comment there for
  // why a returned frame handle cannot be trusted to fire. Fixed identically so
  // the two schedulers cannot drift apart.
  if (_fontRaf) cancelAnimationFrame(_fontRaf)
  _fontRaf = requestAnimationFrame(() => { _fontRaf = 0; applyTerminalFontToAll() })
}

let _fontSubscribed = false
/** Subscribe once (module-level) so a preference change repaints every terminal. */
function ensureTerminalFontSync(): void {
  if (_fontSubscribed || typeof window === 'undefined') return
  _fontSubscribed = true
  subscribeTerminalFont(scheduleTerminalFontApply)
}

function getOrCreateTerm(id: string): { term: Terminal; fit: FitAddon } {
  let entry = termCache.get(id)
  if (!entry) {
    const font = getTerminalFont()
    const term = new Terminal({
      cursorBlink: true,
      fontSize: font.fontSize,
      fontFamily: resolveTerminalFontFamily(font.fontFamily),
      theme: getTermTheme(),
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    entry = { term, fit }
    termCache.set(id, entry)
  }
  return entry
}

function destroyTerm(id: string) {
  const entry = termCache.get(id)
  if (entry) {
    entry.term.dispose()
    termCache.delete(id)
  }
}

/**
 * Tear down a terminal tab's LOCAL state: close its persistent WS and dispose
 * the cached xterm instance. Killing the backend PTY is a separate server call
 * routed through useDeleteTerminalSession() (per the use-react-query guideline);
 * SidePanel.handleCloseTab fires both.
 */
export function disposeTerminalSession(sessionId: string): void {
  disposeTerminalConnection(sessionId)
  destroyTerm(sessionId)
}

/**
 * React Query mutation that kills a terminal's backend PTY
 * (DELETE /api/terminal/sessions/:id). Best-effort — a failed delete is
 * backstopped by the server-side orphan reaper — but routing it through a
 * mutation gives it the standard write lifecycle instead of a bare fetch.
 * Local teardown stays synchronous in disposeTerminalSession().
 */
export function useDeleteTerminalSession() {
  return useMutation({
    mutationFn: async (sessionId: string) => {
      const res = await fetch(`/api/terminal/sessions/${sessionId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(`Failed to delete terminal session (${res.status})`)
    },
  })
}

/**
 * Force xterm to re-measure the character cell, then refit. xterm measures the
 * cell at open()/fit() time with whatever font is resolvable then; the terminal
 * font ('JetBrains Mono') is a Google web font (display=swap) that can swap in
 * *after* the first measure, widening the cell while `cols` stays stale, so the
 * screen overflows the pane and the right edge is clipped (worst in
 * the narrow right sidebar). xterm exposes no public "re-measure now" API, so we
 * force CharSizeService.measure() via a transient fontFamily toggle (both sets
 * run synchronously, so nothing paints between them). Verified against xterm
 * 5.5.x, where CharSizeService re-measures on its
 * `onMultipleOptionChange(['fontFamily','fontSize'])` subscription;
 * CliPanel.fontRefit.test.ts pins that version so a future bump fails loudly.
 *
 * Skips detached / display:none panes (offsetParent === null), matching the
 * sibling refit effects (the ResizeObserver gates on offsetHeight, the focus
 * effect on `visible`): measuring a zero-size pane would cache a 0-width cell.
 * Such a pane is re-measured by the becoming-visible refit when next shown.
 */
export function remeasureAndFit(term: Terminal, fit: FitAddon): void {
  const el = term.element
  if (!el || !el.offsetParent) return // offsetParent === null when display:none / detached
  const ff = term.options.fontFamily
  term.options.fontFamily = 'monospace' // transient — forces CharSizeService.measure()
  term.options.fontFamily = ff          // restore — re-measures with the now-loaded font
  fit.fit()
}

/* ── Terminal view for one session ── */
/** Literal-key lookup for the touch range-selection hint (#6834) — one compact
 *  string per gesture stage, serving both the visible hint chip and the sr-only
 *  live region. Literal keys (no runtime-assembled catalog keys) keep the
 *  dead-key scan meaningful, same discipline as the key bar's label lookups. */
function touchSelectHint(status: TouchSelectStatus): string {
  switch (status) {
    case 'range_anchor': return i18nT('components.cliPanel.touch_select_anchor')
    case 'range_selected': return i18nT('components.cliPanel.touch_select_done')
    default: return ''
  }
}

function TerminalView({ sessionId, cwd, visible, onSendToChat }: { sessionId: string; cwd?: string; visible: boolean; onSendToChat?: (text: string) => void }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const entryRef = useRef<{ term: Terminal; fit: FitAddon } | null>(null)
  // Floating selection toolbar: anchored to where the drag ended (wrapper-
  // relative px), carrying the captured text so the actions stay valid even
  // after xterm clears its own selection.
  const [sel, setSel] = useState<{ x: number; y: number; text: string } | null>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const [copied, setCopied] = useState<'idle' | 'done' | 'failed'>('idle')
  const [sending, setSending] = useState<'idle' | 'busy' | 'failed'>('idle')
  const toolbarRef = useRef<HTMLDivElement>(null)
  // The soft keys exist for one reason — no physical keyboard.
  const touchDevice = useIsTouchDevice()

  // Live socket state, so the view can surface a disconnected banner. The
  // banner shows in two cases: the terminal 'disconnected' state (backoff
  // ceiling reached), and while a USER-initiated reconnect is in flight — the
  // latter as a "Reconnecting…" presentation so a failing manual retry is not
  // silent. Ordinary automatic 'reconnecting' blips (tab hide/show) stay quiet
  // to avoid flicker; only a manual retry sets manualRetry.
  const connStatus = useTerminalConnStatus(sessionId)
  const manualRetry = useTerminalManualRetry(sessionId)
  // The manual "Reconnecting…" state: the user clicked Reconnect and the dial
  // has not yet resolved. On failure the status flips back to 'disconnected'
  // (manualRetry clears with it), returning the banner to its disconnected
  // presentation; on success it flips to 'connected' and the banner is gone.
  const reconnecting = manualRetry && connStatus === 'reconnecting'
  const showBanner = connStatus === 'disconnected' || reconnecting

  if (!entryRef.current) {
    entryRef.current = getOrCreateTerm(sessionId)
  }
  const { term, fit } = entryRef.current

  // Touch range-selection (#6834): long-press a line to set the first endpoint,
  // then tap/long-press another to select the inclusive span. xterm's own
  // drag-selection is mouse-only and never fires on touch, so on a phone this
  // is the only way to grab an ARBITRARY range out of the middle of scrollback
  // (the staged Select key only reaches the last line or the whole buffer).
  // Inert on mouse devices, where xterm's drag-selection already works.
  const touchSelect = useTerminalTouchSelection(term, touchDevice)

  // Re-measure + refit, gated on pane visibility (see remeasureAndFit). Stable
  // per cached term/fit, so the effects below don't re-subscribe.
  const doRefit = useCallback(() => remeasureAndFit(term, fit), [term, fit])

  // Persistent per-session WS: created once and kept alive across tab/chat/
  // panel/route unmounts; torn down only on explicit tab close. Unmounting this
  // component does not disconnect — the module-level manager owns the socket.
  useEffect(() => {
    ensureTerminalConnection(sessionId, term, fit, cwd)
  }, [sessionId, term, fit, cwd])

  // Attach terminal to DOM
  useEffect(() => {
    if (!containerRef.current) return
    const el = term.element
    if (el) {
      containerRef.current.appendChild(el)
    } else {
      term.open(containerRef.current)
    }
    fit.fit()
  }, [term, fit])

  // Focus + refit when becoming visible. Re-measure here too so a pane that
  // gained the web font while it was hidden (display:none) picks up the correct
  // cell metrics the moment it is shown.
  useEffect(() => {
    if (!visible) return
    const raf = requestAnimationFrame(doRefit)
    term.focus()
    return () => cancelAnimationFrame(raf)
  }, [visible, term, doRefit])

  // Refit on container resize — always observe, not just when visible
  useEffect(() => {
    if (!containerRef.current) return
    const ro = new ResizeObserver(() => {
      if (containerRef.current?.offsetHeight) fit.fit()
    })
    ro.observe(containerRef.current)
    return () => ro.disconnect()
  }, [fit]) // stable — only depends on fit ref from cache

  // Refit after web fonts finish loading (see remeasureAndFit). `ready` resolves
  // once all pending @font-face loads settle; we also explicitly request the
  // terminal font in case it has not started loading when `ready` first fires.
  // doRefit no-ops on hidden panes — those are caught by the becoming-visible
  // refit above.
  useEffect(() => {
    const fonts = document.fonts
    if (!fonts) return
    let cancelled = false
    const onReady = () => { if (!cancelled) doRefit() }
    // `ready` resolves immediately (harmless no-op) if the font loaded before mount.
    fonts.ready.then(onReady)
    try {
      const px = term.options.fontSize ?? 13
      // Preload the family the terminal is actually configured to render. The
      // previous version hardcoded 'JetBrains Mono' — fine for the default
      // stack but silently miss for anyone who picked a different Terminal
      // Font Family (e.g. the bundled OpenDyslexicMono, or a custom Nerd Font):
      // @font-face fonts fetch lazily on first DOM use, so xterm's canvas
      // renderer would measure and draw against the generic monospace fallback
      // until some other var(--mono) surface pulled the resolved family in.
      // Using `term.options.fontFamily` (already the resolved stack from
      // resolveTerminalFontFamily — comma-separated with a generic fallback)
      // makes the load target follow the picker.
      const configuredFontFamily = term.options.fontFamily ?? 'monospace'
      fonts.load(`${px}px ${configuredFontFamily}`).then(onReady, () => {})
    } catch { /* invalid spec on some engines — `ready` handler covers it */ }
    return () => { cancelled = true }
  }, [term, doRefit]) // stable — term/doRefit come from the per-session cache

  // xterm instances persist in termCache across tab switches — only destroyed
  // on explicit tab close via disposeTerminalSession.

  // Selection toolbar lifecycle. We show the toolbar on mouseup (the drag is
  // finished and the selection is stable), anchored to the pointer, and hide it
  // whenever the selection is cleared or the viewport scrolls (which would leave
  // the toolbar floating over unrelated text). Text is snapshotted into state so
  // the button handlers survive xterm clearing its own selection.
  useEffect(() => {
    const wrap = wrapperRef.current
    if (!wrap) return
    const onMouseUp = (e: MouseEvent) => {
      // Defer one frame: xterm finalises the selection on its own mouseup
      // handler, which may run after ours depending on listener order.
      requestAnimationFrame(() => {
        const text = term.hasSelection() ? term.getSelection() : ''
        if (!text.trim()) { setSel(null); return }
        const rect = wrap.getBoundingClientRect()
        setCopied('idle')
        setSending('idle')
        setSel({ x: e.clientX - rect.left, y: e.clientY - rect.top, text })
      })
    }
    wrap.addEventListener('mouseup', onMouseUp)
    const selDisp = term.onSelectionChange(() => { if (!term.hasSelection()) setSel(null) })
    const scrollDisp = term.onScroll(() => setSel(null))
    return () => {
      wrap.removeEventListener('mouseup', onMouseUp)
      selDisp.dispose()
      scrollDisp.dispose()
    }
  }, [term])

  const dismissSelection = useCallback(() => {
    term.clearSelection()
    setSel(null)
  }, [term])

  // Clamp the toolbar fully inside the terminal pane. Ancestor containers use
  // overflow-hidden, so any part that spills past an edge is clipped (the
  // "cut off" bug). We measure the rendered toolbar, center it on the pointer
  // horizontally but clamp both edges inside the pane, prefer sitting above the
  // selection, and flip below when there isn't room above. Runs before paint so
  // the toolbar never flashes at an unclamped position.
  useLayoutEffect(() => {
    if (!sel) { setPos(null); return }
    const wrap = wrapperRef.current
    const bar = toolbarRef.current
    if (!wrap || !bar) return
    const cw = wrap.clientWidth
    const ch = wrap.clientHeight
    const w = bar.offsetWidth
    const h = bar.offsetHeight
    const M = 8 // margin from the pane edges
    const left = Math.max(M, Math.min(sel.x - w / 2, cw - w - M))
    let top = sel.y - h - M            // preferred: above the pointer
    if (top < M) top = sel.y + 16      // no room above → drop below the line
    top = Math.max(M, Math.min(top, ch - h - M))
    setPos({ left, top })
  }, [sel])

  const handleSendToChat = useCallback(async () => {
    if (!sel?.text || !onSendToChat) return
    setSending('busy')
    // Server-side redaction of the COMPLETE selection before insertion.
    // Streamed PTY output is redacted per read chunk, so a secret straddling
    // a chunk boundary can evade both scans; the contiguous re-scan closes
    // that gap. Fail closed: nothing is inserted unless redaction succeeds.
    let redacted: string
    try {
      const res = await fetch('/api/terminal/redact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: sel.text }),
      })
      if (!res.ok) throw new Error(`redact ${res.status}`)
      redacted = (await res.json()).text
    } catch {
      setSending('failed')
      return // keep the selection + toolbar so the user can retry
    }
    setSending('idle')
    // Annotate the handoff so the agent (and the reader) knows this is
    // literal terminal output and where it came from. The fence keeps the
    // output verbatim; backtick-runs inside are escaped by widening the
    // fence beyond the longest run.
    const runs = redacted.match(/`+/g)
    const fence = '`'.repeat(Math.max(3, ...(runs?.map(r => r.length + 1) ?? [0])))
    // Prefer the live cwd (backend polls the shell ~1/s) so the path is
    // correct after the user cd's around; fall back to the spawn dir.
    const path = getTerminalCwd(sessionId) ?? cwd
    const header = path ? `Terminal output (\`${path}\`):` : 'Terminal output:'
    onSendToChat(`${header}\n${fence}\n${redacted}\n${fence}`)
    dismissSelection()
  }, [sel, sessionId, cwd, onSendToChat, dismissSelection])

  const handleCopy = useCallback(() => {
    if (!sel?.text) return
    // Confirm only after the write actually lands; a denied/unavailable
    // clipboard shows "Copy failed" and keeps the selection so the user can
    // fall back to the native copy shortcut.
    const write = navigator.clipboard?.writeText(sel.text)
    if (!write) { setCopied('failed'); return }
    write.then(
      () => {
        setCopied('done')
        // Keep the toolbar up briefly so the confirmation is visible.
        setTimeout(dismissSelection, 900)
      },
      () => setCopied('failed'),
    )
  }, [sel, dismissSelection])

  return (
    <div
      ref={wrapperRef}
      className="relative flex flex-col flex-1 min-h-0 h-full overflow-hidden"
      style={{ display: visible ? 'flex' : 'none' }}
    >
      {/* Positioning context for the completion menu, sized to the TERMINAL
          only. The menu clamps itself inside its `offsetParent`; anchoring it to
          the outer wrapper would let it count the key bar's height as free space
          and drop over the soft keys — covering Tab on exactly the devices the
          bar exists for.

          The touch handlers (#6834) live on THIS div, not the outer wrapper, so
          a touch maps to a terminal row using the terminal's own geometry. They
          no-op on mouse devices (the hook's `enabled` gate), where xterm's own
          drag-selection handles selection. Passive: they never preventDefault,
          so ordinary scroll/tap still reach xterm — a long-press is recognised
          by a still-finger timer, not by swallowing the gesture. */}
      <div
        className="relative w-full flex-1 min-h-0 overflow-hidden"
        onTouchStart={touchSelect.onTouchStart}
        onTouchMove={touchSelect.onTouchMove}
        onTouchEnd={touchSelect.onTouchEnd}
        onTouchCancel={touchSelect.onTouchEnd}
      >
        <div ref={containerRef} className="w-full h-full overflow-hidden" />
        {/* Owns xterm's SINGLE `attachCustomKeyEventHandler` slot for this term
            (it reserves Tab/Enter/arrows/Escape while its menu is open). A later
            feature that attaches its own handler here would silently replace it —
            extend the handler inside TerminalCompletion instead. */}
        <TerminalCompletion term={term} sessionId={sessionId} active={visible} />
        {/* Touch range-selection discoverability (#6834). The gesture is
            invisible on a canvas — nothing tells the user an endpoint is set —
            so surface it two ways: a small transient hint chip anchored top-left
            (visual), and an sr-only live region (screen reader), the same
            announce-through-a-status-region pattern the key bar's Copy/Select
            use. Only rendered on touch, and only while a gesture is mid-flight
            or just completed. */}
        {touchDevice && touchSelect.status && (
          <div
            // Sits top-left over the grid. When the disconnect banner is up it
            // occupies the top strip (z-30) and would fully cover a top-2 chip —
            // and copying the last output of a dead session is exactly when this
            // gesture is used — so drop the chip below the banner then (UX
            // review #8070).
            className={`pointer-events-none absolute left-2 z-20 rounded-md border border-border bg-bg-elevated/95 px-2 py-0.5 text-[11px] text-text shadow-sm backdrop-blur ${showBanner ? 'top-10' : 'top-2'}`}
            aria-hidden="true"
          >
            {touchSelectHint(touchSelect.status)}
          </div>
        )}
        {touchDevice && (
          <span role="status" aria-live="polite" className="sr-only">
            {touchSelect.status ? touchSelectHint(touchSelect.status) : ''}
          </span>
        )}
        {showBanner && (
          <div
            className="absolute inset-x-0 top-0 z-30 flex items-center gap-2 border-b border-border bg-bg-elevated/95 px-3 py-1.5 text-[12px] text-text shadow-sm backdrop-blur"
            role="status"
            aria-live="polite"
          >
            <PlugZap className={`h-3.5 w-3.5 shrink-0 ${reconnecting ? 'text-text-muted' : 'text-danger'}`} aria-hidden="true" />
            <span className="min-w-0 flex-1 truncate">
              {reconnecting
                ? i18nT('components.cliPanel.reconnecting')
                : i18nT('components.cliPanel.disconnected_message')}
            </span>
            <button
              type="button"
              onClick={() => retryTerminalConnection(sessionId)}
              disabled={reconnecting}
              className="shrink-0 rounded-md border border-border px-2 py-0.5 text-[12px] text-text hover:bg-bg-hover transition-colors disabled:cursor-default disabled:opacity-60 disabled:hover:bg-transparent"
            >
              {i18nT('components.cliPanel.reconnect')}
            </button>
          </div>
        )}
      </div>
      {/* Below the terminal in flow, never over it: the shell prompt occupies the
          bottom row, so an overlay would hide the line being typed. The terminal
          shrinks by the bar's height and the ResizeObserver above refits it. */}
      {touchDevice && <TerminalKeyBar term={term} />}
      {sel && (
        // Positioning-only container; the interactive affordances are the
        // native <button>s inside. The mouse handlers merely guard event
        // bubbling to the wrapper, so this element carries no interactive role.
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions
        <div
          ref={toolbarRef}
          className="absolute z-20 flex items-center gap-0.5 rounded-lg border border-border bg-bg-elevated p-0.5 shadow-lg transition-opacity"
          style={{
            left: pos?.left ?? 0,
            top: pos?.top ?? 0,
            // Hidden until measured/clamped so it never paints at the raw
            // (potentially clipped) pointer position for a frame.
            opacity: pos ? 1 : 0,
            pointerEvents: pos ? 'auto' : 'none',
          }}
          // Keep the pointer interaction from bubbling back to the wrapper
          // mouseup handler (which would re-evaluate and flicker the toolbar).
          onMouseDown={e => e.preventDefault()}
          onMouseUp={e => e.stopPropagation()}
        >
          {onSendToChat && (
            <button
              type="button"
              onClick={handleSendToChat}
              disabled={sending === 'busy'}
              className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-[12px] text-text hover:bg-bg-hover transition-colors ${sending === 'busy' ? 'opacity-60' : ''}`}
              title={sending === 'failed' ? i18nT('components.cliPanel.redaction_failed_retry') : i18nT('components.cliPanel.send_selection_to_chat')}
            >
              <MessageSquarePlus className={`h-3.5 w-3.5 ${sending === 'failed' ? 'text-red-500' : ''}`} />
              {sending === 'busy' ? i18nT('components.cliPanel.sending') : sending === 'failed' ? i18nT('components.cliPanel.failed_retry') : i18nT('components.cliPanel.send_to_chat')}
            </button>
          )}
          <button
            type="button"
            onClick={handleCopy}
            className="flex items-center gap-1.5 rounded-md px-2 py-1 text-[12px] text-text hover:bg-bg-hover transition-colors"
            title={i18nT('components.cliPanel.copy_selection')}
          >
            {copied === 'done' ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className={`h-3.5 w-3.5 ${copied === 'failed' ? 'text-red-500' : ''}`} />}
            {copied === 'done' ? i18nT('components.cliPanel.copied') : copied === 'failed' ? i18nT('components.cliPanel.copy_failed') : i18nT('components.cliPanel.copy')}
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * One terminal tab in the activity bar: a single shell bound to `sessionId`,
 * spawned in `cwd` (the chat's working directory, if any). The tab chip owns
 * identity + close, so this is header-less — it just hosts the xterm view.
 * Sessions are chat-specific by virtue of living in usePanelTabs' per-slot
 * bucket; the module-level termCache keeps them warm across tab/chat switches.
 */
export default function CliPanel({ sessionId, cwd, visible = true, onSendToChat }: {
  sessionId: string
  cwd?: string
  visible?: boolean
  onSendToChat?: (text: string) => void
}) {
  useEffect(() => { ensureThemeObserver(); ensureTerminalFontSync() }, [])
  return (
    <div className="flex flex-col w-full h-full overflow-hidden bg-bg px-3 pt-2">
      <TerminalView sessionId={sessionId} cwd={cwd} visible={visible} onSendToChat={onSendToChat} />
    </div>
  )
}
