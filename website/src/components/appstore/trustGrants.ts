import type { TrustAppTarget } from './TrustAppModal'

/** The manifest `permissions` fields the trust prompt discloses. */
export interface DisclosedPermissions {
  sessionApproval?: boolean
  memberThreads?: boolean
  workerAutoApprove?: boolean
  chatSlots?: boolean
  reachableByUserSessions?: string[]
  desktopCapabilities?: string[]
}

type TrustGrants = Pick<TrustAppTarget, 'sessionApproval' | 'memberThreads' | 'workerAutoApprove' | 'chatSlots' | 'reachableByUserSessions' | 'desktopCapabilities'>

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
    reachableByUserSessions: permissions?.reachableByUserSessions,
    desktopCapabilities: permissions?.desktopCapabilities,
  }
}
