import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search } from 'lucide-react'
import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { scoreSettingEntry } from '../../components/commandPalette/settingsSearchCore'
import { settingsRoute } from '../../components/commandPalette/settingsRoute'
import { settingsSubtitle } from '../../components/commandPalette/settingsTabLabel'
import type { SettingEntry } from '../../components/commandPalette/settingsTypes'
import { makeScoreThenNameComparator } from '../../utils/fuzzyMatch'
import { useListKeyboardNav } from '../../hooks/useListKeyboardNav'
import { i18nT } from '../../i18n/t'

/**
 * SettingsSearch — in-page search over SETTINGS_REGISTRY, rendered in the
 * Settings page header (SidePanelLayout's `headerRight`, both desktop and
 * narrow branches).
 *
 * Search and ranking are the command palette's, literally: both surfaces call
 * `scoreSettingEntry` in settingsSearchCore, so a query ranks identically here
 * and in Search Everywhere, and every keyword/scoring fix lands once for both.
 * The corpus carries the LOCALIZED label and tab name, so non-English users
 * can search in their own language while the English keyword overlay keeps
 * working.
 *
 * Activation stays inside the page: navigating to `settingsRoute(entry)`
 * REPLACES the query string with a fresh set of params (tab + entry.params +
 * highlight) — stale params from the previous tab (channel=, section=, …)
 * cannot ride along and stop the target panel from mounting. The highlight
 * param hands off to `useSettingHighlight`, which SettingsPage already mounts
 * to scroll to and flash the target row.
 */

/** Dropdown cap. Enough to show every plausible hit for a specific query
 *  without the menu growing past its max height into a second scroller. */
const MAX_RESULTS = 12

const LISTBOX_ID = 'settings-search-results'

interface Match {
  entry: SettingEntry
  /** Label as rendered in the active locale — what the row displays. */
  label: string
  score: number
}

const compareMatches = makeScoreThenNameComparator<Match>(
  m => m.score,
  m => m.label,
)

function searchSettings(query: string): Match[] {
  const out: Match[] = []
  for (const entry of SETTINGS_REGISTRY) {
    const s = scoreSettingEntry(query, entry)
    if (!s) continue
    out.push({ entry, label: s.localizedLabel, score: s.score })
  }
  out.sort(compareMatches)
  return out.slice(0, MAX_RESULTS)
}

export default function SettingsSearch() {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  // Escape/blur/outside-click dismiss the dropdown without clearing the text;
  // any edit re-opens it. Tracked separately from the query so a dismissed
  // dropdown stays closed while the input still shows what was typed.
  const [dismissed, setDismissed] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const q = query.trim()
  const results = useMemo(() => (q ? searchSettings(q) : []), [q])
  const open = q.length > 0 && !dismissed

  const activate = useCallback((m: Match) => {
    // settingsRoute builds the full deep link (tab + entry.params + highlight)
    // and navigation REPLACES the query string, so stale params from the
    // previous tab (channel=, section=, …) cannot ride along and stop the
    // target panel from mounting. useSettingHighlight (mounted by
    // SettingsPage) scrolls to the row, flashes it, then strips the param.
    navigate(settingsRoute(m.entry))
    setQuery('')
  }, [navigate])

  const close = useCallback(() => setDismissed(true), [])

  // Shared Arrow/Enter/Escape handling. Escape closes the dropdown only —
  // focus never leaves the input, so the user can keep typing.
  const { selected, setSelected, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    wrap: true,
    onChoose: i => { const m = results[i]; if (m) activate(m) },
    onClose: close,
  })

  // New query → selection back to the top, so Enter always takes the best match.
  useEffect(() => {
    if (open) setSelected(0)
  }, [q, open, setSelected])

  // Click-outside closes (same document-mousedown pattern as ProjectPicker).
  // Row clicks land inside rootRef, so they never trip this.
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open, close])

  return (
    <div ref={rootRef} className="relative shrink-0">
      <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted pointer-events-none" />
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-label={i18nT('pages.settingsPage.search.aria_label')}
        aria-expanded={open}
        aria-controls={LISTBOX_ID}
        aria-activedescendant={open && results.length > 0 ? `settings-search-option-${selected}` : undefined}
        placeholder={i18nT('pages.settingsPage.search.placeholder')}
        value={query}
        onChange={e => { setQuery(e.target.value); setDismissed(false) }}
        // Choosing a row never blurs: rows activate on mousedown and
        // preventDefault, so a genuine blur means focus left the widget.
        onBlur={close}
        className="w-44 sm:w-56 bg-bg-elevated border border-border rounded-lg pl-8 pr-3 py-1.5 text-[13px] text-text placeholder:text-muted focus:outline-none focus-visible:border-accent"
      />
      {open && (
        <div
          id={LISTBOX_ID}
          role="listbox"
          aria-label={i18nT('pages.settingsPage.search.aria_label')}
          className="absolute right-0 top-full mt-1 w-80 max-w-[calc(100vw-2rem)] max-h-80 overflow-y-auto bg-card border border-border rounded-lg shadow-lg z-50 py-1"
        >
          {results.length === 0 ? (
            <div className="px-3 py-3 text-[12px] text-muted">{i18nT('pages.settingsPage.search.no_results')}</div>
          ) : results.map((m, i) => (
            <div
              key={m.entry.id}
              id={`settings-search-option-${i}`}
              role="option"
              aria-selected={i === selected}
              tabIndex={-1}
              ref={el => { itemRefs.current[i] = el }}
              className={`px-3 py-2 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}
              onMouseEnter={() => setSelected(i)}
              onMouseDown={e => { e.preventDefault(); activate(m) }}
            >
              <div className="text-[13px] font-medium text-text-strong truncate">{m.label}</div>
              <div className="text-[11px] text-muted truncate">{settingsSubtitle(m.entry)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
