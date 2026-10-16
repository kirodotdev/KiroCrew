// Fixtures that ConnectionsPage.coverage.test.tsx and
// ConnectionsPage.coverage.connect.test.tsx both read. The api mock they drive
// stays in each test file: its vi.mock factory reads that file's own vi.fn spies.
import { StrictMode } from 'react'
import type { ChatMessage, McpServer, RootState } from '../types'
import ConnectionsPage from '../pages/connections/ConnectionsPage'
import { createTestStore, renderWithProviders } from './helpers'

export const NOTION_URL = 'https://mcp.notion.com/mcp'

export function server(over: Partial<McpServer> = {}): McpServer {
  return {
    name: 'notion',
    command: '',
    url: NOTION_URL,
    status: 'ok',
    source: 'mcp.json',
    enabled: true,
    ...over,
  }
}

/** A gateway `mcp_oauth` banner for `serverName`, exactly as chatSlice holds it. */
export function banner(serverName: string, meta: Record<string, unknown>, ts = '2026-03-04T10:00:00.000Z'): ChatMessage {
  return { role: 'mcp_oauth', content: '', cls: '', ts, meta: { server_name: serverName, ...meta } }
}

interface ChatSeed {
  messages?: ChatMessage[]
  slotMessages?: Record<string, ChatMessage[]>
}

export function mount(
  { servicesEnabled = true, chat = {}, strict = false }: { servicesEnabled?: boolean; chat?: ChatSeed; strict?: boolean } = {},
) {
  const store = createTestStore({
    chat: {
      messages: chat.messages ?? [],
      slotMessages: chat.slotMessages ?? {},
    } as unknown as RootState['chat'],
  })
  const tree = <ConnectionsPage servicesEnabled={servicesEnabled} />
  return renderWithProviders(strict ? <StrictMode>{tree}</StrictMode> : tree, { store })
}

/** The card for one provider, addressed the way the DOM exposes it. */
export function card(slug: string): HTMLElement {
  const el = document.getElementById(`connection-${slug}`)
  if (!el) throw new Error(`no card rendered for ${slug}`)
  return el
}

/** A promise whose settlement this test controls. */
export function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}
