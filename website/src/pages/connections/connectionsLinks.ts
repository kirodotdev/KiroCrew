/**
 * Where links land a reader on the Connections and MCP Servers pages. Plain
 * strings, so a page that links here does not pull either page into its chunk.
 */

/** Settings → Connections: the provider cards and their OAuth apps. */
export const CONNECTIONS_HREF = '/settings/connections'

/** The MCP Servers page (#21303). One plain string: an interpolated literal
 *  under an ALL-CAPS name trips the i18n lint. */
export const MCP_SERVERS_HREF = '/mcp'

/** The query param naming the Connections card to expand and scroll to. */
export const CONNECTION_PROVIDER_PARAM = 'provider'

/** Settings → Connections with one provider's card expanded. */
export function connectionCardHref(slug: string): string {
  return `${CONNECTIONS_HREF}?${CONNECTION_PROVIDER_PARAM}=${encodeURIComponent(slug)}`
}

/**
 * Where an old Customize link to the Connections tab (`?tab=mcp`) lands now.
 * Its MCP Servers sub-tab (`&view=mcp-servers`) is the MCP Servers page, and
 * that sub-tab's `#mcp-management` fragment is the page's Sharing view; the
 * service cards are Settings → Connections. Every other param rides along.
 */
export function legacyConnectionsTarget(search: string, hash: string): string | null {
  const params = new URLSearchParams(search)
  if (params.get('tab') !== 'mcp') return null
  const mcpServers = params.get('view') === 'mcp-servers'
  params.delete('tab')
  if (mcpServers) {
    params.delete('view')
    if (hash === '#mcp-management') params.set('view', 'sharing')
  }
  const query = params.toString()
  const keptHash = mcpServers && hash === '#mcp-management' ? '' : hash
  return `${mcpServers ? MCP_SERVERS_HREF : CONNECTIONS_HREF}${query ? `?${query}` : ''}${keptHash}`
}

/** The Google entry's key: the six Google cards (Gmail, Drive, Calendar,
 *  Sheets, Docs, Slides) are one entry on the page and can share one client. */
export const GOOGLE_ENTRY_SLUG = 'google'

export function isGoogleSlug(slug: string): boolean {
  return slug === 'gmail' || slug.startsWith('google-')
}
