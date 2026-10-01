import { useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { setConfigAutolinkRules } from '../utils/autolinkRules'

/**
 * Register the operator's link rules (`dashboard.link_patterns`) into the
 * module-level autolink registry that `MarkdownRenderer` reads. Owned by the
 * app shell so the registry is populated for EVERY surface — an app page, a
 * settings pane, a widget — not just once a chat page has rendered. The chat
 * page shares the same `['dashboardConfig']` query for its GitLab/Jira source
 * hosts; the registry itself has one owner, here.
 *
 * The write is ref-guarded and idempotent: `setConfigAutolinkRules` replaces
 * the whole set and validates each entry, so re-applying the same serialized
 * value on a re-render is a no-op. Applied during render (not in an effect) so
 * the pass that delivers a config change also paints with it, matching how the
 * registry validates an edition-registered rule.
 */
export function useConfigAutolinkRules(): void {
  const { data } = useQuery<{ link_patterns?: Array<{ pattern: string; url: string }> }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })
  const linkPatternRules = data?.link_patterns
  const linkPatternsKey = JSON.stringify(linkPatternRules ?? [])
  const appliedLinkPatternsRef = useRef('')
  if (appliedLinkPatternsRef.current !== linkPatternsKey) {
    appliedLinkPatternsRef.current = linkPatternsKey
    setConfigAutolinkRules(linkPatternRules ?? [])
  }
}
