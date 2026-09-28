/**
 * The edition capability manager (/api/capability): list, install and
 * uninstall MCP servers, skills and agents, plugin list and sync, and the
 * capability MCP registry.
 */

import type { ClientTransport } from './transport'

export function createCapabilityManagerEndpoints({ post, j }: ClientTransport) {
  const catalog = {
    // Graceful no-ops on a public install, where AIM is stubbed; the panels
    // render empty when the feature is absent.
    capabilityMcpList: () => fetch('/api/capability/mcp').then(j),
    capabilityMcpInstall: (serverId: string) => post('/api/capability/mcp/install', { server_id: serverId }).then(j),
    capabilityMcpUninstall: (serverId: string) => post('/api/capability/mcp/uninstall', { server_id: serverId }).then(j),
    capabilitySkillsList: () => fetch('/api/capability/skills').then(j),
    capabilitySkillsInstall: (pkg: string) => post('/api/capability/skills/install', { package: pkg }).then(j),
    capabilitySkillsUninstall: (pkg: string) => post('/api/capability/skills/uninstall', { package: pkg }).then(j),
    capabilityAgentsList: () => fetch('/api/capability/agents').then(j),
    capabilityAgentsInstall: (pkg: string) => post('/api/capability/agents/install', { package: pkg }).then(j),
    capabilityAgentsUninstall: (pkg: string) => post('/api/capability/agents/uninstall', { package: pkg }).then(j),
    // Plugin packages (agent-client integrations). The response pairs the installed
    // rows with `out_of_sync` — packages installed as agents but missing their
    // plugin counterpart — so the UI can offer a one-click reconcile.
    capabilityPluginsList: () => fetch('/api/capability/plugins').then(j),
    capabilityPluginsSync: () => post('/api/capability/plugins/sync', {}).then(j),
    capabilityMcpRegistry: () => fetch('/api/capability/mcp/registry').then(j),
  }

  return { catalog }
}
