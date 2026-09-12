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
  /** Gallery bucket from the backend's closed `PROVIDER_CATEGORIES` vocabulary;
   *  catalog metadata only, nothing renders or gates on it yet. */
  category?: string
  /**
   * How the OAuth client comes into being. Absent means kiro-cli registers one
   * at runtime (DCR). `preregistered` means an operator must register an app
   * in the vendor console and enter its client id (and secret, when
   * `confidential`) under Settings → OAuth Apps before Connect can work; the
   * card renders a "needs configuration" state until `/api/connections/status`
   * says it is configured. Mirrors `AuthConfig` in the backend registry.
   */
  auth?: {
    mode: 'dcr' | 'preregistered'
    confidential?: boolean
    redirect_host?: string
    redirect_port?: number
    /** Path under `docs/guides/` of the operator runbook. */
    registration_guide?: string
  }
}

const registry = registryJson as ConnectionProvider[]

export function isPreregisteredProvider(provider: ConnectionProvider): boolean {
  return provider.auth?.mode === 'preregistered'
}

/**
 * A visible card is a promise: only providers that passed the launch gate ship --
 * with one exception that is not a promise to connect. A PRE-REGISTERED provider
 * is shown regardless of the gate, because until an operator configures a client
 * its card is an instruction ("an administrator must configure an OAuth app"),
 * and hiding the instruction is how the operator never learns the step exists.
 * Vendor approval stays a hard hide either way. Mirrors the backend's
 * `get_visible_providers`; the two must agree or a card and its status row
 * disagree about whether the provider exists.
 */
export const CONNECTION_PROVIDERS = registry.filter(
  provider =>
    !provider.vendor_approval_pending &&
    (provider.launch_gate_passed || isPreregisteredProvider(provider)),
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
