import { useQuery } from '@tanstack/react-query'
import { Globe } from 'lucide-react'
import { api } from '../../api/client'
import { findLiveSite } from '../PublishHub'
import { safeHttpUrl } from '../../lib/safeUrl'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_ARTIFACT_DEPLOY } from '../../utils/previewFlags'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'

function useLiveSites(enabled: boolean) {
  return useQuery({
    queryKey: ['deploy-web', 'sites'],
    queryFn: () => api.deploySites(),
    enabled,
    staleTime: 30_000,
    // The library stays open for long stretches, and each read reaches every
    // registered AWS account: re-read on a mount or a deploy, not on every focus.
    refetchOnWindowFocus: false,
    retry: false,
  })
}

/**
 * The link to an artifact's public-web site, on its library entry.
 *
 * Read from the deploy's own site listing, the same query and cache entry the
 * Publish panel and the Artifact Deploy page use, so nothing is stored on the
 * artifact and a destroyed or reaped site takes its link with it. Every entry
 * shares that one request. Renders nothing until the Artifact Deploy preview is
 * on and the listing names a site for this slug.
 */
export function LiveSiteLink({ slug }: { slug: string }) {
  const deployPreview = usePreviewFlag(PREVIEW_ARTIFACT_DEPLOY)
  const { data } = useLiveSites(deployPreview)
  const raw = deployPreview ? findLiveSite(data, slug)?.url : undefined
  const url = raw ? safeHttpUrl(raw) : null
  if (!url) return null
  const { host, pathname } = new URL(url)
  return (
    // `relative` lifts it above the gallery card's open button; the click stays
    // off the table row, whose own click opens the artifact. Not draggable, so
    // a press-and-drag here still drags the entry into a folder.
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={url}
      draggable={false}
      onClick={(e) => e.stopPropagation()}
      className="relative flex items-center gap-1 mt-0.5 max-w-[min(100%,400px)] w-fit text-[11px] text-info hover:underline"
    >
      <Globe className="lucide-inline" aria-hidden="true" />
      <span className="truncate font-mono">{`${host}${pathname}`}</span>
    </a>
  )
}

/**
 * The library's one notice for a site listing that failed, so a missing link
 * never reads as "not published". A listing that could not reach an account
 * answers HTTP 200 with the reason in `error` or `profile_errors`, so on a host
 * with deploy configured those count as failures too. The entries stay usable, and links from an earlier read stay.
 */
export function LiveSitesNotice({ askAgent }: { askAgent: boolean }) {
  const deployPreview = usePreviewFlag(PREVIEW_ARTIFACT_DEPLOY)
  const sites = useLiveSites(deployPreview)
  if (!deployPreview) return null
  // `configured: false` is a host with no AWS profile, or one where deploy cannot
  // run at all (native Windows answers with `error`): nothing to list, not a failure.
  const listingErrors = sites.data?.configured === false
    ? []
    : [sites.data?.error, ...(sites.data?.profile_errors ?? [])].filter((e): e is string => !!e)
  const message = sites.isError
    ? (errMessage(sites.error) || i18nT('components.errorBoundary.something_went_wrong'))
    : listingErrors.join('; ')
  if (!message) return null
  return (
    <div className="mb-4 flex items-start gap-2">
      <ErrorNotice
        title={i18nT('pages.artifactsPage.live_links_check_failed')}
        message={message}
        messagePlacement="below"
        askAgent={askAgent}
        className="flex-1"
      />
      <Btn onClick={() => { void sites.refetch() }} disabled={sites.isFetching} className="shrink-0">
        {i18nT('pages.artifactsPage.retry')}
      </Btn>
    </div>
  )
}
