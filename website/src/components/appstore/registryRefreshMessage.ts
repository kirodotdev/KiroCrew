/**
 * The banner text for registries whose index fetch failed: after a refresh that
 * left some registries stale, and on a store listing that is missing a
 * registry's apps.
 *
 * The backend names why a registry failed (`reason` on a refresh result,
 * `cause` on a listing or registries-list status) and the `host` from its
 * configured URL. A cause the user can fix gets a leading sentence per remedy
 * and host: a host name that did not resolve (connect to the VPN), an SSH host
 * key this machine does not trust (known_hosts), a refused sign-in (renew the
 * credentials; restarting the gateway does nothing), and a host that could not
 * be reached. Every failure keeps the plain line after them.
 */
import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'

export interface RegistryRefreshResult {
  ok?: boolean
  failed?: string[]
  /**
   * `cached` on a failed row: whether the listing still serves that registry's
   * last-synced apps. `false` means its apps are missing from the store; a row
   * without it is read as last synced.
   */
  results?: { name: string; ok: boolean; reason?: string; host?: string; cached?: boolean }[]
}

/** One registry whose latest index fetch failed, and why when the backend knows. */
export interface RegistryFetchFailure {
  name: string
  cause?: string
  host?: string
  /** The listing still serves the registry's apps from its last successful fetch. */
  cached?: boolean
}

type Remedy = 'auth' | 'host_not_found' | 'unreachable' | 'host_key'

/** Registry names joined the way the reader language joins a list. */
function nameList(names: readonly unknown[]): string {
  return fmtList(names.map(n => String(n ?? '')), { style: 'narrow' })
}

/**
 * The remedy a cause gets, and the host its sentence names (`''` for none).
 * `other`, an unknown cause and a host key without a host get none.
 */
function remedyOf(cause: string | undefined, host: string): { remedy: Remedy; host: string } | null {
  switch (cause) {
    case 'auth': return { remedy: 'auth', host }
    case 'dns': return host ? { remedy: 'host_not_found', host } : { remedy: 'unreachable', host: '' }
    case 'network':
    case 'timeout': return { remedy: 'unreachable', host: '' }
    case 'host_key': return host ? { remedy: 'host_key', host } : null
    default: return null
  }
}

function remedySentence(remedy: Remedy, host: string, names: string): string {
  switch (remedy) {
    case 'auth': return host
      ? i18nT('components.registryManager.refresh_sign_in_refused', { host, names })
      : i18nT('components.registryManager.refresh_sign_in_refused_no_host', { names })
    case 'host_not_found': return i18nT('components.registryManager.registry_host_not_found', { host, names })
    case 'unreachable': return i18nT('components.registryManager.registry_unreachable', { names })
    case 'host_key': return i18nT('components.registryManager.registry_host_key_untrusted', { host, names })
  }
}

/** One sentence per remedy and host, in the order the failures first name them. */
export function registryFetchRemedies(failures: readonly RegistryFetchFailure[]): string[] {
  const groups = new Map<string, { remedy: Remedy; host: string; names: string[] }>()
  for (const f of failures) {
    const found = remedyOf(f.cause, f.host ?? '')
    if (!found) continue
    const id = `${found.remedy}\n${found.host}`
    const group = groups.get(id)
    if (group) group.names.push(f.name)
    else groups.set(id, { ...found, names: [f.name] })
  }
  return [...groups.values()].map(g => remedySentence(g.remedy, g.host, nameList(g.names)))
}

/** The message to show, or `''` when every registry refreshed. */
export function registryRefreshFailureMessage(res: RegistryRefreshResult | undefined): string {
  if (!res || res.ok !== false || !res.failed || res.failed.length === 0) return ''
  const remedies = registryFetchRemedies(
    (res.results ?? []).filter(r => !r.ok).map(r => ({ name: r.name, cause: r.reason, host: r.host })),
  )
  const missing = new Set((res.results ?? []).filter(r => !r.ok && r.cached === false).map(r => r.name))
  const stale = res.failed.filter(name => !missing.has(name))
  return [
    ...remedies,
    missing.size > 0 ? i18nT('components.registryManager.registry_apps_missing', { names: nameList([...missing]) }) : '',
    stale.length > 0 ? i18nT('components.registryManager.could_not_refresh_still_showing_last_synced', { names: nameList(stale) }) : '',
  ].filter(Boolean).join(' ')
}

/**
 * The message for a store listing some of whose registries failed to fetch, or
 * `''` when none did. A registry served from its last successful fetch is named
 * as stale; one with no such copy as missing from the store. One line per
 * remedy, then one for the missing and one for the stale registries, so a
 * notice naming several causes still reads as a list.
 */
export function registryListingFailureMessage(failures: readonly RegistryFetchFailure[]): string {
  if (failures.length === 0) return ''
  const missing = failures.filter(f => !f.cached).map(f => f.name)
  const stale = failures.filter(f => f.cached).map(f => f.name)
  return [
    ...registryFetchRemedies(failures),
    missing.length > 0 ? i18nT('components.registryManager.registry_apps_missing', { names: nameList(missing) }) : '',
    stale.length > 0 ? i18nT('components.registryManager.could_not_refresh_still_showing_last_synced', { names: nameList(stale) }) : '',
  ].filter(Boolean).join('\n')
}
