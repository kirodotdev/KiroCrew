import type { TrustAppTarget } from './TrustAppModal'
import { knownChatLaunch } from './chatLaunch'
import { i18nT } from '../../i18n/t'
import { memoryProviderPreviewOff } from '../../app-sdk/memoryProviderPreview'

/** The API prefix granted by the manifest's literal `memoryAdmin: true`. */
export const MEMORY_ADMIN_API_PREFIX = '/api/memory/provider'

/** Use the same description at install, re-approval and on the permissions card.
 *  While this Crew does not run the memory provider preview the grant opens nothing,
 *  and the line says so. */
export function apiGrantLabel(path: string): string {
  if (path !== MEMORY_ADMIN_API_PREFIX) return path
  return memoryProviderPreviewOff()
    ? i18nT('pages.overview.memoryProvider.admin_grant_not_enabled')
    : i18nT('pages.overview.memoryProvider.admin_grant')
}

export function disclosedApi(permissions: DisclosedPermissions | undefined): string[] | undefined {
  return permissions?.memoryAdmin === true
    ? [...new Set([...(permissions.api ?? []), MEMORY_ADMIN_API_PREFIX])]
    : permissions?.api
}

/** The manifest `permissions` fields the trust prompt discloses. */
export interface DisclosedPermissions {
  api?: string[]
  memoryAdmin?: boolean
  sessionApproval?: boolean
  memberThreads?: boolean
  workerAutoApprove?: boolean
  chatSlots?: boolean
  hiddenSessions?: boolean
  chatLaunch?: unknown
  reachableByUserSessions?: string[]
  desktopCapabilities?: string[]
  browser?: string[]
  kiroOperations?: string[]
  webhookSource?: boolean
}

type TrustGrants = Pick<
  TrustAppTarget,
  'sessionApproval' | 'memberThreads' | 'workerAutoApprove' | 'chatSlots' | 'hiddenSessions' | 'chatLaunch' | 'reachableByUserSessions' | 'desktopCapabilities' | 'browser' | 'kiroOperations' | 'api' | 'webhookSource'
>

/**
 * The declared grants a trust prompt shows, read off an app's manifest
 * `permissions`. Every caller that opens the prompt reads them here, so a grant
 * the prompt learns to show reaches every entry point at once. A flag counts
 * only as the literal `true`, as the gateway reads it.
 */
export function trustGrants(permissions: DisclosedPermissions | undefined): TrustGrants {
  return {
    sessionApproval: permissions?.sessionApproval === true,
    memberThreads: permissions?.memberThreads === true,
    workerAutoApprove: permissions?.workerAutoApprove === true,
    chatSlots: permissions?.chatSlots === true,
    hiddenSessions: permissions?.hiddenSessions === true,
    chatLaunch: knownChatLaunch(permissions?.chatLaunch),
    reachableByUserSessions: permissions?.reachableByUserSessions,
    desktopCapabilities: permissions?.desktopCapabilities,
    browser: permissions?.browser,
    kiroOperations: permissions?.kiroOperations,
    api: disclosedApi(permissions),
    webhookSource: permissions?.webhookSource === true,
  }
}
