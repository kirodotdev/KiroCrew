/**
 * Crew-to-crew work migration (issue #7577).
 *
 * Every route here returns a PLAN, never a performed move: the handoff id, the
 * allow-listed field count, the target's blocking requirements and any advisory
 * findings. The three live together because one backend module
 * (`handlers/migration.py`) owns them and one dialog renders all three plans,
 * even though their paths sit under three different endpoint families — a cron
 * job, a task run and a chat slot are the three things a crew can hold.
 *
 * Transmit lands with the tunnel wiring, so a caller that receives a plan has
 * moved nothing yet.
 */

import type { ClientTransport } from './transport'

export function createMigrationEndpoints({ post, j }: ClientTransport) {
  const plans = {
    planCronMove: (id: string, toCrew: string) =>
      post('/api/crons/' + encodeURIComponent(id) + '/move', { to_crew: toCrew }).then(j),
    planTaskRunMove: (taskId: string, toCrew: string) =>
      post('/api/taskrunner/' + encodeURIComponent(taskId) + '/move', { to_crew: toCrew }).then(j),
    planSessionMove: (slot: string, toCrew: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/move', { to_crew: toCrew }).then(j),
  }
  return { plans }
}
