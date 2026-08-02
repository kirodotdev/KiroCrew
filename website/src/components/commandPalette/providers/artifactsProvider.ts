import { createElement } from 'react'
import { LayoutGrid } from 'lucide-react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import { fuzzyMatch, makeScoreThenNameComparator, substringIndices } from '../../../utils/fuzzyMatch'
import type { Artifact } from '../../../types'
import type { Result, ResourceProvider } from '../types'

/**
 * Artifacts provider for the Search Everywhere command palette.
 *
 * Backs the **Artifacts** tab. Wraps `api.artifacts(...)` with the opt-in
 * `?snippet=1` + `?content=1` params (the artifact list endpoint's
 * content-search mode) and maps each hit to a {@link Result}:
 *  - `title`    — the artifact name (fuzzy-highlighted client-side).
 *  - `subtitle` — the server's redacted content preview: a match-centered
 *    snippet for a content query, else the opening lines (falls back to the
 *    artifact description / kind when no snippet is returned).
 *  - `onActivate` (Enter) — open the artifact at `/artifacts/<slug>`.
 *
 * The backend does the relevance filtering (name + tags + description +
 * content), so the {@link fuzzyMatch} pass here is only for client-side
 * highlight indices on the returned titles (per the `frontend-security` lint rule —
 * highlights render as React `<mark>` nodes keyed off `indices`, never HTML
 * strings). Title matches additionally bias the client-side ordering; non-title
 * (body-only) matches are kept with a neutral score so backend hits are never
 * dropped.
 */

const PROVIDER_ID = 'artifacts'
/**
 * Catalog KEY for the tab label, not the label itself: this const is evaluated
 * at module load, so an `i18nT()` here would freeze the boot language. The call
 * happens in `createArtifactsProvider()`, which `useArtifactsProvider()` runs
 * from a `useMemo` during render.
 *
 * REUSES the existing sidebar-nav key rather than adding a third copy of
 * "Artifacts" to the catalog — the palette tab and the nav entry name the same
 * feature, so they should move together when a locale revises the term.
 */
const PROVIDER_LABEL_KEY = 'nav.artifacts'

/** Cache server responses briefly so retyping the same query is free. */
const ARTIFACTS_STALE_MS = 30_000

/** Shape of the `GET /api/artifacts` response envelope. */
export interface ArtifactsResponse {
  artifacts?: Artifact[]
}

/**
 * Injectable dependencies for {@link createArtifactsProvider}. Keeping the
 * concrete provider free of React hooks makes it unit-testable with a plain
 * mock fetch + open callback; {@link useArtifactsProvider} wires the real
 * React-Query fetch + router navigation.
 */
export interface ArtifactsProviderDeps {
  /** Fetch content-search hits for a query (React-Query-cached in the hook). */
  fetchArtifacts: (query: string) => Promise<ArtifactsResponse>
  /** Open an artifact's detail page (Enter). */
  openArtifact: (slug: string) => void
}

function artifactIcon() {
  return createElement(LayoutGrid, { className: 'lucide-inline' })
}

/**
 * Build the Artifacts {@link ResourceProvider} from injected dependencies.
 * Pure (no hooks) so it can be exercised directly in tests.
 */
export function createArtifactsProvider(deps: ArtifactsProviderDeps): ResourceProvider {
  const { fetchArtifacts, openArtifact } = deps

  return {
    id: PROVIDER_ID,
    // A GETTER, not a plain call: the provider object is built inside a `useMemo`
    // whose deps do not include the language, so `label: i18nT(...)` would resolve
    // once and keep the pre-switch wording forever. `LanguageProvider` forces a
    // re-RENDER via `cloneElement` (it deliberately does NOT remount — see its own
    // comment rejecting `key={active}`), and a re-render does not recompute a memo.
    // An accessor moves the lookup to the consumer's render, where the tab strip
    // reads it. Satisfies `ResourceProvider.label: string`.
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
    icon: artifactIcon(),
    async search(query: string): Promise<Result[]> {
      const q = query.trim()
      const data = await fetchArtifacts(q)
      const artifacts = data?.artifacts ?? []

      const results: Result[] = artifacts.map((a) => {
        const title = a.name || a.slug
        // Highlight + client-side rank bias; never used to drop backend hits.
        const match = fuzzyMatch(q, title)
        const subtitle = a.snippet || a.description || a.kind
        // Highlight the query within the snippet/description (content match).
        const subIdx = q ? substringIndices(q, subtitle) : []
        return {
          id: `${PROVIDER_ID}:${a.slug}`,
          providerId: PROVIDER_ID,
          title,
          subtitle,
          subtitleIndices: subIdx.length ? subIdx : undefined,
          icon: artifactIcon(),
          score: match ? match.score : 0,
          indices: match ? match.indices : [],
          // Not yet ported to the declarative §2 Enter matrix — the central
          // dispatcher falls back to onActivate. Enter opens the artifact.
          onActivate: () => openArtifact(a.slug),
        }
      })

      // Title matches first, then deterministic name order. Skip the re-rank on
      // an empty query so the backend's recency ordering is preserved (the
      // Artifacts tab + All-tab recents rely on it).
      if (q.length > 0) {
        results.sort(makeScoreThenNameComparator<Result>(r => r.score, r => r.title))
      }
      return results
    },
  }
}

/**
 * React hook that returns a live Artifacts provider wired to React-Query and
 * the router.
 *
 * Per the `use-react-query` lint rule the server fetch goes through
 * React-Query with the key `['palette', 'artifacts', q]`. `fetchQuery` is used
 * rather than `useQuery` because a {@link ResourceProvider}'s `search` is an
 * imperative call from the palette, not a render-time subscription. The list is
 * requested with `snippet=1` always (for the preview) and `content=1` only when
 * there is a query (so the empty-query recents stay a cheap meta-only scan).
 */
export function useArtifactsProvider(): ResourceProvider {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  return useMemo(
    () =>
      createArtifactsProvider({
        fetchArtifacts: (q) =>
          queryClient.fetchQuery<ArtifactsResponse>({
            queryKey: ['palette', 'artifacts', q],
            queryFn: () =>
              api.artifacts({ q: q || undefined, snippet: true, contentMatch: q.length > 0 }),
            staleTime: ARTIFACTS_STALE_MS,
          }),
        openArtifact: (slug) => navigate(`/artifacts/${slug}`),
      }),
    [navigate, queryClient],
  )
}
