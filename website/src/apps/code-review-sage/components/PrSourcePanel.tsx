// The app's cache policy for the pull request's provider payload, plus the two
// notices that wrap it.
//
// Data comes from the dashboard's OWN PR endpoint (`/api/source/pull-request`,
// the one the GitHub side panel uses), deliberately rather than a new Sage route:
//   * it already normalises GitHub and GitLab into one shape,
//   * it caches server-side on a short TTL, so opening the same PR twice — or
//     having it open in the side panel at the same time — costs one provider
//     call, not two,
//   * it keeps credentials in the provider CLI on the gateway.
//
// Rendering that payload is NOT done here: the detail pane embeds the shared
// `PullRequestPanel`, so description, checks, comments, the file diff and the
// commit list all have one implementation rather than a Sage-local copy.
import { useEffect, useMemo } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { api } from '../../../api/client'
import type { PullRequestSource } from '../../../types'
import { readSnapshot, writeSnapshot } from '../lib/persist'

import { i18nT } from '../../../i18n/t'
/** The server's own cache TTL is short; match it rather than hammering. */
const SOURCE_STALE_MS = 30_000

/** The provider payload for one pull request.
 *
 * Deliberately keyed the SAME as the shared `PullRequestPanel`'s own query
 * (`['pull-request-source', url]`) rather than under a Sage-private key. The
 * detail pane renders that panel, so a private key would make the two observers
 * separate cache entries and fetch the identical payload twice per open. One key
 * means one entry, one in-flight request, and an invalidation from either side
 * (publishing a review, refreshing the panel) refreshes both.
 *
 * Seeded from the last successful payload with its ORIGINAL fetch timestamp, so
 * reopening a pull request paints its header and threads at once and revalidates
 * behind them. The timestamp is what makes that work: without it the replay would
 * look freshly fetched and suppress the refetch for the whole staleTime window,
 * leaving you reading a payload that never refreshed. */
export function usePrSource(url: string): UseQueryResult<PullRequestSource, Error> {
  const snapshot = useMemo(
    () => (url ? readSnapshot<PullRequestSource>(`pr-source:${url}`) : undefined),
    [url],
  )
  const query = useQuery({
    queryKey: ['pull-request-source', url],
    queryFn: () => api.pullRequestSource(url),
    staleTime: SOURCE_STALE_MS,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.at,
  })
  useEffect(() => {
    if (url && query.data && query.isSuccess) {
      writeSnapshot(`pr-source:${url}`, query.data)
    }
  }, [url, query.data, query.isSuccess])
  return query
}

/** Label keys as full literals in one indexable map, so the key-resolution
 *  gate can verify each one exists. Indexed at the call site, not read off a
 *  local — that indirection is what made these sites unverifiable. */
export function SourceError({ error }: { error: Error }) {
  return (
    <div className="text-[12.5px] text-muted">
      {i18nT('apps.codeReviewSage.components.prSourcePanel.load_failed',
        { reason: error.message })}
    </div>
  )
}

export function PartialNote({ src }: { src: PullRequestSource }) {
  if (!(src.partialSections?.length ?? 0)) return null
  return (
    <div className="text-[11.5px] text-muted opacity-80 leading-[1.5] mt-3">
      {i18nT('apps.codeReviewSage.components.prSourcePanel.partial_sections',
        { sections: src.partialSections?.join(', ') ?? '' })}
    </div>
  )
}
