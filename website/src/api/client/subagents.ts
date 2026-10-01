/**
 * Spawned subagents (/api/spawn): list, spawn, status, delete, stop-all and
 * retry.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import type { SubagentInfo } from '../../types'
import type { ClientTransport } from './transport'

export function createSubagentsEndpoints({ post, del, j }: ClientTransport) {
  const spawned = {
    // Spawn
    spawnList: (): Promise<{ agents?: SubagentInfo[] }> => fetch(relocateRequestUrl('/api/spawn')).then(j),
    spawn: (task: string) => post('/api/spawn', { task }).then(j),
    spawnStatus: (id: string, opts?: { signal?: AbortSignal }) => fetch(relocateRequestUrl('/api/spawn/' + encodeURIComponent(id)), opts).then(j),
    spawnDelete: (id: string) => del('/api/spawn/' + encodeURIComponent(id)).then(j),
    spawnStopAll: (slot: string) => post('/api/spawn/stop-all', { slot }).then(j),
    spawnRetry: (id: string) => post('/api/spawn/' + encodeURIComponent(id) + '/retry', {}).then(j),
  }

  return { spawned }
}
