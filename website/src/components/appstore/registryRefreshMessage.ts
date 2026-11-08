/**
 * The banner text for a registry refresh that left some registries stale.
 *
 * The backend tags a failed registry with `reason: 'auth'` and the `host` from
 * its configured URL when git refused the sign-in: the one failure the user can
 * fix themselves, and the one where restarting the gateway does nothing. Those
 * get a leading sentence per host naming the remedy, ahead of the plain "could
 * not refresh" line that every failure keeps.
 */
import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'

export interface RegistryRefreshResult {
  ok?: boolean
  failed?: string[]
  results?: { name: string; ok: boolean; reason?: 'auth'; host?: string }[]
}

/** Registry names joined the way the reader language joins a list. */
function nameList(names: readonly unknown[]): string {
  return fmtList(names.map(n => String(n ?? '')), { style: 'narrow' })
}

/** The message to show, or `''` when every registry refreshed. */
export function registryRefreshFailureMessage(res: RegistryRefreshResult | undefined): string {
  if (!res || res.ok !== false || !res.failed || res.failed.length === 0) return ''
  const refusedByHost = new Map<string, string[]>()
  for (const r of res.results ?? []) {
    if (r.ok || r.reason !== 'auth') continue
    const host = r.host ?? ''
    refusedByHost.set(host, [...(refusedByHost.get(host) ?? []), r.name])
  }
  const signIn = [...refusedByHost].map(([host, names]) => host
    ? i18nT('components.registryManager.refresh_sign_in_refused', { host, names: nameList(names) })
    : i18nT('components.registryManager.refresh_sign_in_refused_no_host', { names: nameList(names) }))
  const stale = i18nT('components.registryManager.could_not_refresh_still_showing_last_synced',
    { names: nameList(res.failed) })
  return [...signIn, stale].join(' ')
}
