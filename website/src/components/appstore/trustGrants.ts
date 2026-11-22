import type { TrustAppTarget } from './TrustAppModal'
import { knownChatLaunch } from './chatLaunch'

/** The manifest `permissions` fields the trust prompt discloses. */
export interface DisclosedPermissions {
  api?: string[]
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
    api: permissions?.api,
    webhookSource: permissions?.webhookSource === true,
  }
}
