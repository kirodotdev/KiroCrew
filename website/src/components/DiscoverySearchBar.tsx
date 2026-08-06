/**
 * DiscoverySearchBar — the shared search chrome of the provider-discovery
 * modals (SkillBrowserModal, McpBrowserModal): combobox input with clear
 * button, the providers/result-count meta line, and the three pre-result
 * placeholder states (short query, loading, empty).
 *
 * Extracted so the two modals share one implementation instead of a byte
 * clone (jscpd runs at a 0% duplication threshold in CI). Behavior lives in
 * the modals — this renders their state.
 */
import { forwardRef } from 'react'
import { Search, Loader2, X } from 'lucide-react'

import { i18nT } from '../i18n/t'
interface SearchBarProps {
  idPrefix: string
  /** Noun for a11y labels + placeholders, e.g. "skills" / "MCP servers". */
  subject: string
  query: string
  debouncedQuery: string
  providers: string[]
  resultCount: number
  isLoading: boolean
  hasResults: boolean
  activeDescendant: string | null
  onQueryChange: (value: string) => void
  onKeyDown: (e: React.KeyboardEvent) => void
  onClear: () => void
}

export const DiscoverySearchBar = forwardRef<HTMLInputElement, SearchBarProps>(
  function DiscoverySearchBar(
    { idPrefix, subject, query, debouncedQuery, providers, resultCount, isLoading, hasResults, activeDescendant, onQueryChange, onKeyDown, onClear },
    inputRef
  ) {
    return (
      <div className="mb-3 shrink-0">
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded={hasResults}
            aria-controls={`${idPrefix}-results-list`}
            aria-activedescendant={activeDescendant ? `${idPrefix}-opt-${activeDescendant}` : undefined}
            aria-label={i18nT('components.discoverySearchBar.search', { subject })}
            value={query}
            onChange={e => onQueryChange(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={i18nT('components.discoverySearchBar.search_across_providers', { subject })}
            className="w-full pl-9 pr-9 py-2 rounded-md border border-border bg-bg text-text text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            autoFocus
          />
          {query && (
            <button
              onClick={onClear}
              aria-label={i18nT('components.discoverySearchBar.clear_search')}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 rounded text-muted hover:text-text hover:bg-bg-hover"
            >
              <X size={14} aria-hidden="true" />
            </button>
          )}
        </div>
        <div className="mt-1.5 flex items-center justify-between text-xs text-muted">
          <span>{providers.length > 0 ? i18nT('components.discoverySearchBar.searching_2', { name: providers.join(', ') }) : '\u00A0'}</span>
          {debouncedQuery.length >= 2 && !isLoading && (
            <span>{i18nT('components.discoverySearchBar.result', { count: resultCount })}</span>
          )}
        </div>
      </div>
    )
  }
)

/** Centered placeholder for the pre-result modal states. */
export function DiscoveryPlaceholder({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center flex-1 text-muted text-sm">
      {children}
    </div>
  )
}

/** The three canonical pre-result states, keyed off query/loading/results. */
export function DiscoveryStates({ debouncedQuery, isLoading, resultCount, noun }: {
  debouncedQuery: string
  isLoading: boolean
  resultCount: number
  /** Plural noun for the empty state, e.g. "servers" / "skills". */
  noun: string
}) {
  if (debouncedQuery.length < 2) {
    return <DiscoveryPlaceholder>{i18nT('components.discoverySearchBar.type_at_least_2_characters_to_search')}</DiscoveryPlaceholder>
  }
  if (isLoading) {
    return (
      <DiscoveryPlaceholder>
        <Loader2 size={20} className="animate-spin mb-2" aria-hidden="true" />
        {i18nT('components.discoverySearchBar.searching')}
      </DiscoveryPlaceholder>
    )
  }
  if (resultCount === 0) {
    return <DiscoveryPlaceholder>{i18nT('components.discoverySearchBar.no')} {noun} {i18nT('components.discoverySearchBar.found_for_query', { query: debouncedQuery })}</DiscoveryPlaceholder>
  }
  return null
}
