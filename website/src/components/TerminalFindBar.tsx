import { useTranslation } from 'react-i18next'
import { ArrowUp, ArrowDown, X, Search } from 'lucide-react'
import { useImeGuard } from '../hooks/useImeGuard'
import type { TerminalSearch } from '../hooks/useTerminalSearch'

/* ── Terminal scrollback find bar (#10176, #9906) ──────────────────────────
 * A compact search bar pinned top-right inside the terminal pane, over the
 * grid. It opens on Cmd/Ctrl+F while the terminal is focused (the chord is
 * intercepted in TerminalCompletion, which owns xterm's one custom-key slot).
 * Enter / Shift+Enter walk the matches, a live "n of m" count reads out, and
 * Escape closes it and returns focus to the shell. Search uses the addon's
 * defaults (case-insensitive substring); no option toggles — the issues ask
 * only to search the scrollback. */

export default function TerminalFindBar({ search }: { search: TerminalSearch }) {
  const { t } = useTranslation()
  const ime = useImeGuard()
  if (!search.open) return null

  const { query, resultCount, resultIndex } = search
  // "n of m" when matches exist (a find is run on every query change, so the
  // active index is set whenever the count is non-zero), "No results" when a
  // non-empty query finds nothing. Empty query shows nothing. A count that
  // overflows the addon's highlight limit reports index -1; fall back to the
  // bare total then rather than an impossible "0 of m".
  const countLabel = !query
    ? ''
    : resultCount === 0
      ? t('components.terminalFindBar.no_results')
      : resultIndex >= 0
        ? t('components.terminalFindBar.match_count', { index: resultIndex + 1, total: resultCount })
        : t('components.terminalFindBar.match_overflow', { total: resultCount })

  return (
    <div
      data-testid="terminal-find-bar"
      role="search"
      // Narrow-safe: bounded on both edges (left-2 right-2) and WRAPPING, so in
      // a pane down to the 200px dock minimum — a right-docked panel, a chat
      // side-panel terminal, a ~320px viewport — the control groups flow onto
      // further rows instead of clipping past the pane's overflow-hidden edge.
      // The input+count is one group that keeps a full row to itself when it
      // wraps; the toggles and the nav/close buttons are two more groups.
      className="absolute left-2 right-2 top-2 z-30 ml-auto flex w-fit max-w-full flex-wrap items-center justify-end gap-1 rounded-md border border-border bg-bg-elevated px-1.5 py-1 shadow-lg"
    >
      <div className="flex min-w-0 flex-1 basis-40 items-center gap-1">
        <Search size={13} className="shrink-0 text-muted" aria-hidden="true" />
        <input
          ref={search.inputRef}
          type="text"
          aria-label={t('components.terminalFindBar.search_scrollback')}
          placeholder={t('components.terminalFindBar.find')}
          value={query}
          spellCheck={false}
          autoComplete="off"
          className="min-w-0 flex-1 rounded bg-transparent px-1 py-0.5 text-[12.5px] text-text outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent focus-visible:-outline-offset-2 placeholder:text-muted"
          onChange={e => search.setQuery(e.target.value)}
          {...ime.bindComposition()}
          onKeyDown={e => {
            // Keystrokes belong to the find bar, never to the shell underneath or
            // the strip shortcuts; stop them here.
            if (e.key === 'Enter') {
              if (!ime.claimEnter(e)) return
              e.preventDefault()
              if (e.shiftKey) search.findPrevious()
              else search.findNext()
            } else if (e.key === 'Escape') {
              if (!ime.claimKey(e)) return
              e.preventDefault()
              search.closeSearch()
            }
            e.stopPropagation()
          }}
        />
        <span
          data-testid="terminal-find-count"
          aria-live="polite"
          className="min-w-[48px] shrink-0 text-right text-[11px] tabular-nums text-muted"
        >
          {countLabel}
        </span>
      </div>
      <div className="flex items-center gap-0.5">
        <button
          type="button"
          aria-label={t('components.terminalFindBar.previous_match')}
          title={t('components.terminalFindBar.previous_match')}
          onClick={search.findPrevious}
          onMouseDown={e => e.preventDefault()}
          className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
        >
          <ArrowUp size={14} />
        </button>
        <button
          type="button"
          aria-label={t('components.terminalFindBar.next_match')}
          title={t('components.terminalFindBar.next_match')}
          onClick={search.findNext}
          onMouseDown={e => e.preventDefault()}
          className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
        >
          <ArrowDown size={14} />
        </button>
        <button
          type="button"
          aria-label={t('components.terminalFindBar.close_search')}
          title={t('components.terminalFindBar.close_search')}
          onClick={search.closeSearch}
          onMouseDown={e => e.preventDefault()}
          className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
        >
          <X size={14} />
        </button>
      </div>
    </div>
  )
}
