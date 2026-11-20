/**
 * Connected computers (issue 29949), both halves:
 *
 * - `devices`: the REMOTE gateway's list of enrolled user computers, with its
 *   two narrowing actions (revoke, remove).
 * - `local`: THIS computer's own sharing state at its own local gateway: the
 *   grants it holds, a per-grant stop and Off. Starting a share is only the
 *   first half here (`connectedComputersLocalConsent` mints a one-time id); the
 *   desktop shell's native dialog redeems it (`lib/connectedComputers.ts`).
 *
 * The backend is authoritative: while the feature preview is off a route that
 * widens access answers 403 `connected_computers_disabled`. Narrowing routes
 * (revoke, remove, stop, Off) work with it off.
 */

import type { ClientTransport } from './transport'

/** One row of GET /api/connected-computers/devices. Never carries the fingerprint. */
export interface ConnectedComputer {
  device_id: string
  label: string
  /** `active` | `revoked` | `removed` (removed rows are kept so no id is reused). */
  status: string
  created: number
  updated: number
  /** A connector socket is live right now. */
  connected: boolean
  /** The folder the live connector serves; `''` when offline. */
  root: string
  confinement: string
  /** Another computer has the same label: show the id to tell them apart (B09). */
  label_is_shared: boolean
}

/** One row of GET /api/connected-computers/local/grants (`controller.status`). */
export interface LocalGrant {
  grant_id: string
  instance_id: string
  gateway_id: string
  gateway_label: string
  device_id: string
  root: string
  /** Names of the local MCP servers this grant exposes. */
  mcp: string[]
  status: string
  created: number
  revoked_at: number | null
  /** The controller's live state in English (`connected`, `disconnected: <why>`, ...). */
  state: string
}

/** What POST /api/connected-computers/local/consent answers. Nothing is granted yet. */
export interface LocalConsent {
  consent: string
  expires_in: number
  instance: { id: string; name: string }
  mcp: unknown[]
}

export function createConnectedComputersEndpoints({ get, post, del, j }: ClientTransport) {
  const devices = {
    connectedComputers: (): Promise<{ devices: ConnectedComputer[] }> =>
      get('/api/connected-computers/devices').then(j) as Promise<{ devices: ConnectedComputer[] }>,
    /** Revoke: refuses new work at once; the record and the user's files stay. */
    connectedComputerRevoke: (id: string): Promise<{ device: ConnectedComputer }> =>
      post('/api/connected-computers/devices/' + encodeURIComponent(id) + '/revoke', {})
        .then(j) as Promise<{ device: ConnectedComputer }>,
    /** Remove: hides it from lists; never deletes the computer or its files (B12). */
    connectedComputerRemove: (id: string): Promise<{ device: ConnectedComputer }> =>
      post('/api/connected-computers/devices/' + encodeURIComponent(id) + '/remove', {})
        .then(j) as Promise<{ device: ConnectedComputer }>,
    /** This computer's grants. `enabled` is this gateway's own switch (sharing allowed). */
    connectedComputersLocalGrants: (): Promise<{ grants: LocalGrant[]; enabled: boolean }> =>
      get('/api/connected-computers/local/grants').then(j) as Promise<{ grants: LocalGrant[]; enabled: boolean }>,
    /** Step 1 of a share: names the Remote Crew only. The folder and tools are chosen natively. */
    connectedComputersLocalConsent: (instanceId: string): Promise<LocalConsent> =>
      post('/api/connected-computers/local/consent', { instance_id: instanceId }).then(j) as Promise<LocalConsent>,
    /** Stop one grant now; the other grants keep running (B07). */
    connectedComputersLocalRevoke: (grantId: string): Promise<{ revoked: string }> =>
      del('/api/connected-computers/local/grants/' + encodeURIComponent(grantId)).then(j) as Promise<{ revoked: string }>,
    /** Turn every grant off now, offline too; remote revokes follow when reachable. */
    connectedComputersLocalOff: (): Promise<{ revoked: string[] }> =>
      post('/api/connected-computers/local/off', {}).then(j) as Promise<{ revoked: string[] }>,
  }
  return { devices }
}
