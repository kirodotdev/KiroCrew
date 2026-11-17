import registryJson from '../../../../src/kiro_crew/connections/registry.json'
import type { McpServer } from '../../types'

export interface ConnectionProvider {
  name: string
  slug: string
  tier: 1 | 2 | 3
  mcp_url: string
  /** Scopes a new install requests; the warm path sends the same list. */
  recommended_scopes?: string[]
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
  /** `<file>.md#<anchor>` of the shipped docs section an organization admin
   *  follows to allow this install's sign-in. Its presence makes the status feed
   *  report `signInRedirect`, and a refused sign-in links to it. */
  org_admin_guide?: string
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
 *
 * The URL is compared without regard to a trailing slash or letter case, so the
 * card owns such a row and its Connect writes the registry URL onto it before
 * signing in. A card that does not own the row adds a second entry under the
 * slug instead, which the gateway refuses.
 */
export function providerOwningRow(server: McpServer): ConnectionProvider | undefined {
  const serverUrl = normalizedUrl(server.url)
  if (!serverUrl) return undefined
  return CONNECTION_PROVIDERS.find(provider =>
    server.name === provider.slug && normalizedUrl(provider.mcp_url) === serverUrl,
  )
}

/**
 * The provider whose card the MCP Servers table sends this row's sign-in to.
 *
 * A row the gateway marks `connectionUnconfigured` is left alone: its provider
 * has no OAuth Apps record yet, so the card could only fail, and the row's own
 * chat guidance is where the client the user wrote signs in on Kiro CLI (and on
 * any backend while the `crew_oauth` preview is off). The row is still
 * the card's own (`serverForConnection`). A configured row marked
 * `connectionNeedsCrewSignIn` offers setup guidance until Crew can sign in.
 */
export function connectionProviderForServer(server: McpServer): ConnectionProvider | undefined {
  return server.connectionUnconfigured === true || server.connectionNeedsCrewSignIn === true
    ? undefined
    : providerOwningRow(server)
}

/** The row a provider's card owns, marked `connectionUnconfigured` or not. */
export function serverForConnection(
  provider: ConnectionProvider,
  servers: readonly McpServer[],
): McpServer | undefined {
  return servers.find(server => providerOwningRow(server)?.slug === provider.slug)
}
