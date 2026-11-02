/**
 * The edition capability manager (/api/capability): list, install and
 * uninstall MCP servers, skills and agents, plugin list and sync, and the
 * capability MCP registry.
 */

import type { ClientTransport } from './transport'

/** One thing a marketplace install would place: the listing itself, a bundled component or a dependency. */
export interface MarketPlanItem {
  ref: string
  kind: string
  semver: string
  sha256: string
  publisher: { kind: string; display: string }
  reason: string
  requiredBy: string[]
  target: string
  action: string
}

/** What installing a listing would place, and the union of what it may do. */
export interface MarketPlan {
  installable: boolean
  refusal: { code: string; message: string } | null
  items: MarketPlanItem[]
  permissions: {
    commands: string[][]
    network: string[] | 'none' | 'any'
    files: string[]
    secrets: string[]
  }
  conflicts: string[]
  planDigest: string
}

export function createCapabilityManagerEndpoints({ post, j, jfetch: fetch }: ClientTransport) {
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
    // Marketplace listings, installed by the edition's own installer. `plan` writes
    // nothing; `apply` installs only the plan whose digest the user confirmed.
    capabilityMarketStatus: (): Promise<{ available: boolean }> => fetch('/api/capability/market').then(j),
    capabilityMarketPlan: (ref: string): Promise<MarketPlan> => post('/api/capability/market/plan', { ref }).then(j),
    capabilityMarketApply: (ref: string, planDigest: string) =>
      post('/api/capability/market/apply', { ref, plan_digest: planDigest }).then(j),
  }

  return { catalog }
}
