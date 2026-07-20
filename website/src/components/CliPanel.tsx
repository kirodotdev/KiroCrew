import { useEffect, useCallback, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { useMutation } from '@tanstack/react-query'
import { ensureTerminalConnection, disposeTerminalConnection } from '../utils/terminalRegistry'

/* ── Per-session xterm instance cache ──
 * Keyed by PTY session id. Instances persist across tab switches / chat
 * switches (so scrollback + cursor survive), and are only torn down when the
 * owning terminal TAB is closed (disposeTerminalSession). */
const termCache = new Map<string, { term: Terminal; fit: FitAddon }>()

/* ── Terminal theme from CSS custom properties ── */
function getTermTheme() {
  const style = getComputedStyle(document.documentElement)
  return {
    background:          style.getPropertyValue('--bg').trim()            || '#1e1e2e',
    foreground:          style.getPropertyValue('--text').trim()          || '#cdd6f4',
    cursor:              style.getPropertyValue('--accent').trim()        || '#89b4fa',
    selectionBackground: style.getPropertyValue('--accent-subtle').trim() || '#313244',
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
  if (_themeRaf) return
  _themeRaf = requestAnimationFrame(() => { _themeRaf = 0; refreshTermThemes() })
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
    for (const r of records) {
      if (r.type === 'attributes') { scheduleTermThemeRefresh(); return }
      for (const n of r.addedNodes) {
        if (n instanceof HTMLStyleElement && n.id.startsWith('mc-custom-theme-')) {
          scheduleTermThemeRefresh()
          return
        }
      }
    }
  })
  _themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  _themeObserver.observe(document.head, { childList: true })
}

function getOrCreateTerm(id: string): { term: Terminal; fit: FitAddon } {
  let entry = termCache.get(id)
  if (!entry) {
    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
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
 * screen overflows the pane and the right edge is clipped (Mesh-2148, worst in
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
function TerminalView({ sessionId, cwd, visible }: { sessionId: string; cwd?: string; visible: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const entryRef = useRef<{ term: Terminal; fit: FitAddon } | null>(null)

  if (!entryRef.current) {
    entryRef.current = getOrCreateTerm(sessionId)
  }
  const { term, fit } = entryRef.current

  // Re-measure + refit, gated on pane visibility (see remeasureAndFit). Stable
  // per cached term/fit, so the effects below don't re-subscribe.
  const doRefit = useCallback(() => remeasureAndFit(term, fit), [term, fit])

  // Persistent per-session WS: created once and kept alive across tab/chat/
  // panel/route unmounts; torn down only on explicit tab close. Unmounting this
  // component no longer disconnects — the module-level manager owns the socket.
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
      fonts.load(`${px}px "JetBrains Mono"`).then(onReady, () => {})
    } catch { /* invalid spec on some engines — `ready` handler covers it */ }
    return () => { cancelled = true }
  }, [term, doRefit]) // stable — term/doRefit come from the per-session cache

  // xterm instances persist in termCache across tab switches — only destroyed
  // on explicit tab close via disposeTerminalSession.

  return (
    <div
      ref={containerRef}
      className="flex-1 min-h-0 h-full overflow-hidden"
      style={{ display: visible ? 'block' : 'none' }}
    />
  )
}

/**
 * One terminal tab in the activity bar: a single shell bound to `sessionId`,
 * spawned in `cwd` (the chat's working directory, if any). The tab chip owns
 * identity + close, so this is header-less — it just hosts the xterm view.
 * Sessions are chat-specific by virtue of living in usePanelTabs' per-slot
 * bucket; the module-level termCache keeps them warm across tab/chat switches.
 */
export default function CliPanel({ sessionId, cwd, visible = true }: {
  sessionId: string
  cwd?: string
  visible?: boolean
}) {
  useEffect(() => { ensureThemeObserver() }, [])
  return (
    <div className="flex flex-col w-full h-full overflow-hidden bg-bg px-3 pt-2">
      <TerminalView sessionId={sessionId} cwd={cwd} visible={visible} />
    </div>
  )
}
