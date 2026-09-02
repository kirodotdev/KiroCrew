import registryJson from '../../../../src/kiro_crew/connections/registry.json'
import type { McpServer } from '../../types'

export interface ConnectionProvider {
  name: string
  slug: string
  tier: 1 | 2 | 3
  mcp_url: string
  revoke_page_url: string
  docs_url: string
  /**
   * Short, imperative, BLOCKING provider-side requirement — the only string a
   * card renders as a pre-connect warning, and only when present. Long
   * reference notes live in `gotcha_copy`, which cards deliberately do not
   * render: a warning on every card is a warning on none.
   */
  prerequisite_copy?: string
  gotcha_copy: string
  smoke_fixture: {
    tool: string
    args: Record<string, unknown>
  }
  launch_gate_passed: boolean
  vendor_approval_pending: boolean
}

const registry = registryJson as ConnectionProvider[]

/** A visible card is a promise: only providers that passed the launch gate ship. */
export const CONNECTION_PROVIDERS = registry.filter(
  provider => provider.launch_gate_passed && !provider.vendor_approval_pending,
)

function normalizedUrl(value: string | undefined): string {
  return (value || '').trim().replace(/\/+$/, '').toLowerCase()
}

/**
 * Connections creates remote entries under the provider slug. Requiring both
 * that stable name and the registry URL prevents a hand-authored custom server
 * that happens to use the same endpoint from being labelled as managed.
 */
export function connectionProviderForServer(server: McpServer): ConnectionProvider | undefined {
  const serverUrl = normalizedUrl(server.url)
  if (!serverUrl) return undefined
  return CONNECTION_PROVIDERS.find(provider =>
    server.name === provider.slug && normalizedUrl(provider.mcp_url) === serverUrl,
  )
}

export function serverForConnection(
  provider: ConnectionProvider,
  servers: readonly McpServer[],
): McpServer | undefined {
  return servers.find(server => connectionProviderForServer(server)?.slug === provider.slug)
}
