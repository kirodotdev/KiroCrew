import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { publishNoticeKey } from '../components/PublishHub'
import type { ArtifactPublication, PublishProviderDescriptor } from '../types'

/**
 * The accessible label and tooltip of the library's publication badge, shared by
 * the gallery card and the table row so the two never drift.
 *
 * A sync issue or a notice keeps its own wording. Otherwise the badge names the
 * destination (`providerName`, the provider's own `display_name`) with the
 * visibility, and falls back to the provider-neutral "Published (<visibility>)"
 * when no name is known.
 */
export function publicationBadgeLabel(pub: ArtifactPublication, providerName?: string): string {
  if (pub.last_error) return i18nT('pages.artifactsPage.published_sync_issue')
  if (pub.notice) {
    return i18nT(publishNoticeKey({
      rolling_out: 'pages.artifactsPage.published_rolling_out',
      distribution_disabled: 'pages.artifactsPage.published_distribution_disabled',
      origin_mismatch: 'pages.artifactsPage.published_origin_mismatch',
      notice_generic: 'pages.artifactsPage.published_notice_generic',
    }, pub.notice_code))
  }
  const visibility = pub.visibility.toLowerCase()
  const provider = (providerName ?? '').trim()
  if (provider) return i18nT('pages.artifactsPage.published_on_provider', { provider, visibility })
  return i18nT('pages.artifactsPage.published', { visibility })
}

/**
 * The display name of the provider a publication went to, read from the same
 * provider list (and query cache entry) the Artifacts page already loads, the
 * way the detail page resolves its "Published to <provider>" label. `undefined`
 * while the list loads, when it fails, and when the provider is not listed.
 * Asks nothing when there is no publication.
 */
export function usePublicationProviderName(pub: ArtifactPublication | null | undefined): string | undefined {
  const { data } = useQuery<{ providers: PublishProviderDescriptor[] }>({
    queryKey: ['publish-providers', 'widget'],
    queryFn: () => api.getArtifactPublishProviders('widget'),
    staleTime: 300_000,
    enabled: !!pub,
  })
  if (!pub) return undefined
  return data?.providers?.find((p) => p.name === pub.provider)?.display_name || undefined
}
