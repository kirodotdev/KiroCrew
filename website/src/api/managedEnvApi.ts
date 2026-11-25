/**
 * The managed-environment lane's routes (`kiro_crew/managed_env/routes.py`), on the
 * blessed `apiTransport`. Every route runs the owner check and answers 404 while the
 * `crew_client` feature preview is off. No answer here carries a credential.
 */
import { apiTransport } from './apiTransport'

const BASE = '/api/managed-environments'

/** One row of GET list. */
export interface ManagedEnvItem {
  /** The environment id; what the action routes take. */
  sandbox_id: string
  /** Upper-case service status: PENDING, RUNNING, STOPPING, STOPPED, DELETING, FAILED, ...,
   *  or GONE for a deleted own id the service no longer lists. */
  status: string
  display_name?: string
  /** True for an environment this Crew created; only those take an action. */
  own: boolean
  /** `crew-sbx-<id>`, on own environments only: the Remote Crew row's ssh host. */
  ssh_host?: string
}

export interface ManagedEnvList {
  sandboxes: ManagedEnvItem[]
}

export type ManagedEnvAction = 'stop' | 'resume' | 'delete'

const id = (sandboxId: string) => `${BASE}/${encodeURIComponent(sandboxId)}`

/** Whether `~/.ssh/config` holds the lane's `crew-sbx-*` block, where that file is, and
 *  the block's text. GET reads it and writes nothing; POST writes it. */
export interface ManagedEnvSshConfig {
  present: boolean
  path: string
  /** The exact lines a write adds, for the dashboard to show first. */
  block?: string
}

export const managedEnvApi = {
  sshConfig: async (): Promise<ManagedEnvSshConfig> =>
    apiTransport.j(await apiTransport.get(`${BASE}/ssh-config`)) as Promise<ManagedEnvSshConfig>,
  /** Write the block with the gateway's own Python. */
  addSshConfig: async (): Promise<ManagedEnvSshConfig> =>
    apiTransport.j(await apiTransport.post(`${BASE}/ssh-config`, {})) as Promise<ManagedEnvSshConfig>,
  list: async (): Promise<ManagedEnvList> =>
    apiTransport.j(await apiTransport.get(`${BASE}/list`)) as Promise<ManagedEnvList>,
  /** Delete answers `deleting`; it removes no Remote Crew row. */
  act: async (sandboxId: string, action: ManagedEnvAction): Promise<{ status?: string }> =>
    apiTransport.j(await apiTransport.post(`${id(sandboxId)}/${action}`, {})) as Promise<{ status?: string }>,
}
