import { useCallback, useEffect, useRef, useState } from 'react'
import type { Terminal } from '@xterm/xterm'
import type { ISearchOptions, SearchAddon } from '@xterm/addon-search'
import { parseCssColor } from '../lib/iconContrast'

/* ── Terminal scrollback search (#10176, #9906) ────────────────────────────
 * Drives xterm's SearchAddon for ONE terminal pane: the find bar's query lives
 * here, the addon does the buffer search, and `onDidChangeResults` reports the
 * live match count. The browser's own find-in-page only sees the rendered
 * viewport, never the scrollback above it, which is the whole reason this
 * exists — the addon searches the full buffer.
 *
 * Search uses the addon's defaults (case-insensitive substring); case / whole-
 * word / regex options are deliberately not surfaced — the linked issues ask
 * only to "search the scrollback", and a smaller surface is the honest one.
 *
 * The addon instance is owned by the per-session term cache (CliPanel), so a
 * tab switch keeps the shell AND its search state warm; this hook holds the
 * UI-facing query/results and the open flag. Because the cached addon outlives
 * this hook, the hook clears the addon's decorations when it closes, when its
 * pane goes hidden, AND on unmount — otherwise a remount (the dock's
 * `{open && …}`) would leave old highlights painted with no bar to clear them. */

/** The addon only reports a real match count when `decorations` are on (its
 *  count is the number of highlight decorations it drew), so highlight-all and
 *  the "n of m" readout are one feature, not two. */
export interface TerminalSearchState {
  open: boolean
  query: string
  /** 0-based index of the active match, or -1 when none / the count overflowed
   *  the highlight limit (the addon reports -1 for the index then). */
  resultIndex: number
  resultCount: number
}

export interface TerminalSearch extends TerminalSearchState {
  /** Open the find bar and focus its input (idempotent while already open). */
  openSearch: () => void
  /** Close the bar, clear highlights, and return focus to the shell. */
  closeSearch: () => void
  setQuery: (q: string) => void
  findNext: () => void
  findPrevious: () => void
  /** Ref for the find-bar text input, so Cmd+F re-focuses it when already open. */
  inputRef: React.RefObject<HTMLInputElement>
}

type Rgba = NonNullable<ReturnType<typeof parseCssColor>>

const toHex = (n: number) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0')
const rgbaToHex = (c: Rgba) => `#${toHex(c.r)}${toHex(c.g)}${toHex(c.b)}`

/** Composite `fg` (possibly translucent) over opaque `bg`, returning an opaque
 *  #RRGGBB. The decoration API takes only opaque hex, so a translucent token
 *  like `--accent-subtle` (an rgba tint) must be flattened over the terminal
 *  background — otherwise dropping its alpha turns a 12% tint into a
 *  full-strength fill indistinguishable from the opaque active-match colour. */
function flattenOver(fg: Rgba, bg: Rgba): string {
  const a = fg.a
  return rgbaToHex({
    r: fg.r * a + bg.r * (1 - a),
    g: fg.g * a + bg.g * (1 - a),
    b: fg.b * a + bg.b * (1 - a),
    a: 1,
  })
}

/** Read the themed match colours as opaque #RRGGBB. The active match is the
 *  opaque accent; inactive matches are `--accent-subtle` flattened over the
 *  terminal background (`--bg`) so they read as a distinct muted tint, never
 *  the same solid fill as the active one. Falls back to an accessible amber /
 *  slate pair that reads on both default themes when a token is missing. */
function matchDecorations(): ISearchOptions['decorations'] {
  const fallbackActive = '#f59e0b'
  const fallbackMatch = '#78716c'
  if (typeof document === 'undefined') {
    return {
      matchBackground: fallbackMatch,
      matchOverviewRuler: fallbackMatch,
      activeMatchBackground: fallbackActive,
      activeMatchColorOverviewRuler: fallbackActive,
    }
  }
  const style = getComputedStyle(document.documentElement)
  // Theme tokens are hex or rgba(); parseCssColor (the shared theme-colour
  // parser) returns null for anything else, which takes the fallback below.
  const accentRgba = parseCssColor(style.getPropertyValue('--accent'))
  const subtleRgba = parseCssColor(style.getPropertyValue('--accent-subtle'))
  const bgRgba = parseCssColor(style.getPropertyValue('--bg')) ?? { r: 30, g: 30, b: 46, a: 1 }
  const active = accentRgba ? rgbaToHex({ ...accentRgba, a: 1 }) : fallbackActive
  // Flatten the (usually translucent) subtle token over the terminal bg. If it
  // came back fully opaque AND equal to the active colour, fall back so the two
  // stay distinguishable.
  let inactive = subtleRgba ? flattenOver(subtleRgba, bgRgba) : fallbackMatch
  if (inactive === active) inactive = fallbackMatch
  return {
    matchBackground: inactive,
    matchOverviewRuler: inactive,
    activeMatchBackground: active,
    activeMatchColorOverviewRuler: active,
  }
}

const searchOptions = (): ISearchOptions => ({ decorations: matchDecorations() })

export function useTerminalSearch(term: Terminal, search: SearchAddon, active: boolean): TerminalSearch {
  const [state, setState] = useState<TerminalSearchState>({
    open: false,
    query: '',
    resultIndex: -1,
    resultCount: 0,
  })
  const inputRef = useRef<HTMLInputElement>(null)
  // The latest query, read by the imperative find helpers so they do not need
  // to be re-created on every keystroke.
  const liveRef = useRef(state)
  liveRef.current = state
  // Pending requestAnimationFrame handles, cancelled on unmount so a deferred
  // search cannot repaint decorations after the hook (and its pane) is gone.
  const rafs = useRef<Set<number>>(new Set())
  const schedule = useCallback((fn: () => void) => {
    // A holder, not a `const id`, because a synchronous requestAnimationFrame
    // (test fakes, some environments) runs the callback BEFORE the handle is
    // returned, which would read an uninitialized `const` — a TDZ throw. The
    // holder is undefined then; the add below registers the real id, and a
    // later cancel on unmount still reaches it.
    const holder: { id: number | null } = { id: null }
    holder.id = requestAnimationFrame(() => {
      if (holder.id !== null) rafs.current.delete(holder.id)
      fn()
    })
    rafs.current.add(holder.id)
  }, [])
  // Drop every queued frame. Called on close and hide (and unmount) so a search
  // scheduled a moment before cannot repaint highlights after the bar is gone.
  const cancelPending = useCallback(() => {
    for (const id of rafs.current) cancelAnimationFrame(id)
    rafs.current.clear()
  }, [])

  // Live match count. onDidChangeResults fires on every find call (and on the
  // addon's own re-highlight when the buffer changes), so the readout tracks
  // output scrolling past under an open find bar.
  useEffect(() => {
    const disp = search.onDidChangeResults(({ resultIndex, resultCount }) => {
      setState(s => (s.resultIndex === resultIndex && s.resultCount === resultCount
        ? s
        : { ...s, resultIndex, resultCount }))
    })
    return () => disp.dispose()
  }, [search])

  const clear = useCallback(() => {
    search.clearDecorations()
    setState(s => (s.resultIndex === -1 && s.resultCount === 0 ? s : { ...s, resultIndex: -1, resultCount: 0 }))
  }, [search])

  // `incremental` keeps findNext anchored on the current selection while it
  // still matches, which is what a live find-as-you-type box wants: typing
  // f → fo → foo must stay on the FIRST match, not step forward one match per
  // keystroke. The addon applies it to findNext only (never findPrevious), so
  // it is meaningless for 'prev' and omitted there. Explicit Enter / Next
  // navigation passes incremental=false so it advances past the current match.
  const run = useCallback((dir: 'next' | 'prev', incremental = false) => {
    const q = liveRef.current.query
    if (!q) { clear(); return }
    // Defence in depth: substring search does not throw, but a decoration or a
    // future option could, and an uncaught error inside rAF would be silent —
    // clear and zero rather than leave stale highlights.
    try {
      if (dir === 'next') search.findNext(q, { ...searchOptions(), incremental })
      else search.findPrevious(q, searchOptions())
    } catch {
      clear()
    }
  }, [search, clear])

  const openSearch = useCallback(() => {
    setState(s => (s.open ? s : { ...s, open: true }))
    // Focus (and select) on the next frame so the input has mounted.
    schedule(() => {
      const el = inputRef.current
      if (el) { el.focus(); el.select() }
    })
    // Re-run against the current query so reopening restores highlights.
    // Incremental so the restored active match is the first one, not wherever
    // a prior selection left the cursor.
    schedule(() => { if (liveRef.current.query) run('next', true) })
  }, [run, schedule])

  const closeSearch = useCallback(() => {
    cancelPending()           // drop a search queued a frame before Esc
    search.clearDecorations()
    setState(s => (s.open ? { ...s, open: false, resultIndex: -1, resultCount: 0 } : s))
    // Hand focus back to the shell so typing resumes immediately.
    if (active) schedule(() => term.focus())
  }, [search, term, active, schedule, cancelPending])

  const setQuery = useCallback((q: string) => {
    setState(s => ({ ...s, query: q }))
    // Search as the user types; an empty query clears the highlights. Use the
    // just-typed `q` directly (not liveRef, which has not committed yet this
    // frame). Incremental so f → fo → foo stays on the first match instead of
    // stepping forward one match per keystroke (addon-search starts findNext
    // past the current selection unless incremental keeps it anchored).
    schedule(() => {
      if (!q) { clear(); return }
      try {
        search.findNext(q, { ...searchOptions(), incremental: true })
      } catch {
        clear()
      }
    })
  }, [search, clear, schedule])

  const findNext = useCallback(() => run('next'), [run])
  const findPrevious = useCallback(() => run('prev'), [run])

  // A pane going hidden (tab switch) drops its highlights so a background pane
  // is not left decorated; the query is kept. When it becomes active again with
  // the find bar still open, re-run the kept query so the highlights come back
  // — otherwise returning to a searched tab shows a populated bar over an
  // un-highlighted buffer until the next find action.
  useEffect(() => {
    if (!state.open) return
    if (!active) { cancelPending(); search.clearDecorations(); return }
    if (liveRef.current.query) schedule(() => run('next', true))
  }, [active, state.open, search, run, schedule, cancelPending])

  // Unmount cleanup. The addon is cached in termCache and outlives this hook
  // (the dock unmounts CliPanel on hide via `{open && …}`), so without this a
  // remount would find the old query's decorations still painted — and the
  // addon re-highlights them on every new shell line — with no find bar to
  // clear them. Cancel any pending frame first so it cannot repaint after.
  useEffect(() => {
    const pending = rafs.current
    return () => {
      for (const id of pending) cancelAnimationFrame(id)
      pending.clear()
      search.clearDecorations()
    }
  }, [search])

  return {
    ...state,
    openSearch,
    closeSearch,
    setQuery,
    findNext,
    findPrevious,
    inputRef,
  }
}
