import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { CSSProperties } from 'react'
import { api } from '../../api/client'
import { retryPolicy } from '../../api/queryClient'
import { useLanguage } from '../../i18n/LanguageProvider'
import { useChatPageColors } from '../../hooks/usePreviewFlag'
import { dashboardQueryKey } from './dashboardQueryKey'
import { chatThemeVars } from './pageChatTheme'

/**
 * The CHAT column's colour overrides for the open crewmate, from that
 * crewmate's dashboard PAGE.
 *
 * Returns a style object of CSS-variable overrides to spread onto the thread
 * column, or `{}` when nothing applies. Empty is the default-OFF, byte-for-byte
 * state: the column spreads `{}`, which sets no variable, so the chat is the
 * one on `main`.
 *
 * Gated three ways, each of which yields `{}`:
 *   - the crewmate-chat-page-colours preview is off (the query does not even
 *     run — `enabled` is false — so a resting dashboard fetches nothing extra);
 *   - the page declares none of the colour tokens the chat follows;
 *   - a declared background whose derived text cannot be made readable.
 *
 * SHARES THE DASHBOARD TAB'S EXACT QUERY: the same key and the same
 * locale-carrying `queryFn` the tab uses (`[…dashboardQueryKey, locale,
 * 'live']`). This matters beyond saving a fetch — the gateway ARMS a live page
 * per read (`member_dashboard._serve_package`), keyed by `(slug, package)`, and
 * a second read under a different key (or with no locale) would disarm the tab's
 * page and re-arm one with the default locale, so a non-English reader's pushes
 * would arrive in English. Reading through the tab's own cache entry means one
 * armed page, the reader's locale, and one refresh that moves both the tab and
 * the chat.
 */
export function useChatPageTheme(slug: string, member: string): CSSProperties {
  const on = useChatPageColors()
  const { resolved: locale } = useLanguage()
  const enabled = on && Boolean(slug) && Boolean(member)
  const { data } = useQuery({
    // The tab's live key, verbatim: `[prefix, slug, member, locale, 'live']`.
    queryKey: [...dashboardQueryKey({ kind: 'member', slug, member }), locale, 'live'],
    queryFn: () => api.memberDashboard(slug, member, locale),
    enabled,
    retry: retryPolicy,
    // The tab owns refresh (its WS-driven refetch and its long fallback poll).
    // Here a stale read tinting the chat briefly is harmless, and this must not
    // start a refetch cadence of its own on a key the tab also holds: every read
    // re-arms the server's live page, so no re-read on mount or focus.
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  })
  return useMemo(() => {
    const vars = enabled ? chatThemeVars(data?.page_theme) : {}
    if (!Object.keys(vars).length) return {}
    // Setting `--text` alone does not recolour text that INHERITS `color`: the
    // inherited value was already computed from the app's `--text` higher up.
    // So the column re-reads it here, and chrome with no text class of its own
    // (the header's identity pill) follows the derived colour.
    return { ...vars, color: 'var(--text)' } as CSSProperties
  }, [enabled, data?.page_theme])
}
