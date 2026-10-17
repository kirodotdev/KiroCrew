/**
 * Persistent multi-agent channels (/api/channels): list, presets, detail,
 * create/close, post, and per-agent add/update/dismiss/wake/approve and context
 * clearing. Every path parameter passes through `encodeURIComponent`
 * (persistent-agent-channels.md).
 */

import type { ClientTransport } from './transport'

export function createAgentChannelsEndpoints({ post, del, patch, j, jfetch: fetch }: ClientTransport) {
  const channels = {
    // Channels
    channelsList: () => fetch('/api/channels').then(j),
    channelPresets: () => fetch('/api/channels/presets').then(j),
    channelGet: (id: string) => fetch('/api/channels/' + encodeURIComponent(id)).then(j),
    channelCreate: (topic: string, agents: object[]) => post('/api/channels', { topic, agents }).then(j),
    channelClose: (id: string) => del('/api/channels/' + encodeURIComponent(id)).then(j),
    channelPost: (id: string, content: string, mention?: string | string[], thread_id?: string) => post('/api/channels/' + encodeURIComponent(id) + '/messages', { content, mention, thread_id }).then(j),
    channelAddAgent: (id: string, agent: object) => post('/api/channels/' + encodeURIComponent(id) + '/agents', agent).then(j),
    channelUpdateAgent: (id: string, aid: string, updates: object) => patch('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid), updates).then(j),
    channelDismissAgent: (id: string, aid: string) => del('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid)).then(j),
    channelWakeAgent: (id: string, aid: string) => post('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid) + '/wake', {}).then(j),
    channelApproveAgent: (id: string, aid: string, action: string, pattern?: string) => post('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid) + '/approve', pattern ? { action, pattern } : { action }).then(j),
    channelClearContext: (id: string, scope: 'all' | 'agent', agentId?: string) => post('/api/channels/' + encodeURIComponent(id) + '/clear-context', scope === 'agent' ? { scope, agent_id: agentId } : { scope }).then(j),
  }

  return { channels }
}
