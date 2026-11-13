import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'

import ErrorNotice from '../../components/ErrorNotice'
import { Card, CardTitle } from '../../components/ui'
import { api, type SecretsUsageResponse, type SecretUsagePlace } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'
import { whenPreviewFlagSaved } from '../../hooks/usePreviewFlagServerSync'
import { PREVIEW_SECRETS_VAULT } from '../../utils/previewFlags'

/** Under the panel's `['secrets']` key, so every vault write that refreshes
 *  the list refreshes where its items are used too. */
const USAGE_QUERY_KEY = ['secrets', 'usage'] as const

const KIND_KEYS = {
  mcp_server: 'settings.secrets.usage_kind_mcp_server',
  env_file: 'settings.secrets.usage_kind_env_file',
  cron_job: 'settings.secrets.usage_kind_cron_job',
  setting: 'settings.secrets.usage_kind_setting',
  connection: 'settings.secrets.usage_kind_connection',
  integration: 'settings.secrets.usage_kind_integration',
  approved_api: 'settings.secrets.usage_kind_approved_api',
  sprites_profile: 'settings.secrets.usage_kind_sprites_profile',
} as const

function useSecretUsage() {
  return useQuery<SecretsUsageResponse>({
    queryKey: USAGE_QUERY_KEY,
    // Read fresh each time a pane opens: a consumer can change on another page
    // (a grant approved on Schedule, a server added) with no vault write here.
    staleTime: 0,
    refetchOnMount: 'always',
    // Same order as the panel's list: the route answers only once the gateway
    // has the preview switch's save.
    queryFn: async () => {
      await whenPreviewFlagSaved(PREVIEW_SECRETS_VAULT)
      return api.secretsUsage()
    },
  })
}

/** The places listed for *name*. An own-property read: an item may be named
 *  `constructor` or `__proto__`, which a plain index would read off the prototype. */
function placesFor(data: SecretsUsageResponse | undefined, name: string): SecretUsagePlace[] {
  const usage = data?.usage
  if (!usage || !Object.hasOwn(usage, name)) return []
  const places = usage[name]
  return Array.isArray(places) ? places : []
}

function PlaceRow({ place }: { place: SecretUsagePlace }) {
  return (
    <div className="grid grid-cols-1 items-center gap-x-4 gap-y-1 border-t border-border py-2.5 first:border-t-0 first:pt-0 last:pb-0 sm:grid-cols-[140px_minmax(0,1fr)]">
      <div className="text-[13px] text-muted">{i18nT(KIND_KEYS[place.kind])}</div>
      <div className="flex min-w-0 items-center gap-2.5 text-[13.5px] text-text-strong">
        <span className="truncate">{place.name}</span>
        {place.key && <code className="ml-auto truncate font-mono text-[12px] text-muted">{place.key}</code>}
      </div>
    </div>
  )
}

/** The places the gateway's scan found reading item *name*. The scan covers
 *  known readers only, so its empty state says no use was found, never that
 *  nothing uses the item; it shows only when every kind of place was checked. */
export function SecretUsageCard({ name, allowAgentHandoff }: { name: string; allowAgentHandoff: boolean }) {
  const { data, isLoading, isError, error } = useSecretUsage()
  const places = placesFor(data, name)
  const skipped = data?.incomplete ?? []
  const incomplete = skipped.length > 0
  return (
    <Card className="mb-0 py-3.5">
      <CardTitle>{i18nT('settings.secrets.usage_title')}</CardTitle>
      {isError ? (
        <ErrorNotice variant="inline" askAgent={allowAgentHandoff} message={i18nT('settings.secrets.usage_error', { error: (error as Error).message })} />
      ) : isLoading ? (
        <p className="text-[13px] text-muted">{i18nT('settings.secrets.loading')}</p>
      ) : (
        <>
          {places.map(place => <PlaceRow key={`${place.kind}\u0000${place.name}\u0000${place.key}`} place={place} />)}
          {places.length === 0 && !incomplete && <p className="text-[13px] text-muted">{i18nT('settings.secrets.usage_none')}</p>}
          {incomplete && <ErrorNotice variant="inline" askAgent={allowAgentHandoff} message={i18nT('settings.secrets.usage_incomplete', { places: fmtList(skipped.map(kind => i18nT(KIND_KEYS[kind] ?? KIND_KEYS.setting))) })} />}
        </>
      )}
    </Card>
  )
}

/** Names the scan found a use for. The rail's "Not used" means an integration
 *  ignores the item; it is withheld for a name in here, so a row never reads
 *  "Not used" beside a pane that lists its users. */
export function useReferencedNames(): ReadonlySet<string> {
  const { data } = useSecretUsage()
  return useMemo(
    () => new Set(Object.entries(data?.usage ?? {}).filter(([, places]) => places.length > 0).map(([name]) => name)),
    [data?.usage],
  )
}

/** A line for the delete confirmation of an item the scan found in use; nothing
 *  otherwise (no line rather than a claim that nothing uses it). */
export function SecretInUseNote({ name }: { name: string }) {
  const { data } = useSecretUsage()
  if (placesFor(data, name).length === 0) return null
  return <span className="basis-full text-[13px] font-medium text-danger">{i18nT('settings.secrets.delete_in_use')}</span>
}
