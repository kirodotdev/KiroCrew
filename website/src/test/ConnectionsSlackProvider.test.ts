/**
 * Slack is a pre-registered Connections provider: its card renders "needs
 * configuration" until a Client ID is saved, and a configured `slack` MCP row at
 * Slack's own endpoint belongs to that card. The MCP Servers table reads the
 * second half (`connectionProviderForServer`) to send the row's sign-in to the
 * card instead of treating it as a server with its own OAuth client.
 */
import { describe, expect, it } from 'vitest'
import type { McpServer } from '../types'
import {
  CONNECTION_PROVIDERS,
  connectionProviderForServer,
  isPreregisteredProvider,
  serverForConnection,
} from '../pages/connections/registry'

function row(name: string, url: string): McpServer {
  return { name, url, clientId: '123.456', status: 'needs_auth', source: 'mcp.json', enabled: true } as McpServer
}

describe('Slack Connections provider', () => {
  it('shows a card as a pre-registered client that needs its secret', () => {
    const slack = CONNECTION_PROVIDERS.find(p => p.slug === 'slack')
    expect(slack).toBeDefined()
    expect(isPreregisteredProvider(slack!)).toBe(true)
    expect(slack!.auth?.confidential).toBe(true)
    expect(slack!.auth?.redirect_host).toBe('localhost')
  })

  it('owns a slug-named row at a trailing-slash or letter-case variant of the registry URL', () => {
    expect(connectionProviderForServer(row('slack', 'https://mcp.slack.com/mcp'))?.slug).toBe('slack')
    // The card must own these: its Connect writes the registry URL onto the row
    // before it signs in. A card that does not own the row adds a second `slack`
    // entry instead, which the gateway refuses with 409.
    expect(connectionProviderForServer(row('slack', 'https://mcp.slack.com/mcp/'))?.slug).toBe('slack')
    expect(connectionProviderForServer(row('slack', 'HTTPS://MCP.SLACK.COM/mcp'))?.slug).toBe('slack')
    expect(connectionProviderForServer(row('notion', 'https://mcp.notion.com/mcp/'))?.slug).toBe('notion')
  })

  it('leaves the row alone while no OAuth app is saved, and owns it once one is', () => {
    const unconfigured = { ...row('slack', 'https://mcp.slack.com/mcp'), connectionUnconfigured: true }
    expect(connectionProviderForServer(unconfigured)).toBeUndefined()
    expect(connectionProviderForServer({ ...unconfigured, connectionUnconfigured: false })?.slug).toBe('slack')
  })

  it('keeps a marked row as the card\'s own row: only the MCP Servers table leaves it alone', () => {
    const slack = CONNECTION_PROVIDERS.find(p => p.slug === 'slack')!
    const unconfigured = { ...row('slack', 'https://mcp.slack.com/mcp'), connectionUnconfigured: true }
    // The card must find its row, or it reads as Not connected and its Connect
    // adds a second `slack` entry, which the gateway refuses with 409.
    expect(serverForConnection(slack, [row('docs', 'https://docs.example.com/mcp'), unconfigured])).toBe(unconfigured)
    expect(serverForConnection(slack, [row('slack', 'https://slack.example.com/mcp')])).toBeUndefined()
  })

  it('keeps the card ownership but disables its row sign-in until Crew sign-in is live', () => {
    const slack = CONNECTION_PROVIDERS.find(p => p.slug === 'slack')!
    const needsCrewSignIn = { ...row('slack', slack.mcp_url), connectionNeedsCrewSignIn: true }
    expect(connectionProviderForServer(needsCrewSignIn)).toBeUndefined()
    expect(serverForConnection(slack, [needsCrewSignIn])).toBe(needsCrewSignIn)
    expect(connectionProviderForServer({ ...needsCrewSignIn, connectionNeedsCrewSignIn: false })?.slug).toBe('slack')
  })

  it('does not own a row at another endpoint or under another name', () => {
    expect(connectionProviderForServer(row('slack', 'https://slack.example.com/mcp'))).toBeUndefined()
    expect(connectionProviderForServer(row('my-slack', 'https://mcp.slack.com/mcp'))).toBeUndefined()
  })
})
