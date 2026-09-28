/**
 * Guards the MCP OAuth banner wiring on ChatPage.
 *
 * The regression this file exists for: commit 4e555e3 dropped the page's
 * `mcp_oauth` branch while touching unrelated code, and the main chat printed
 * `🔐 X requires authentication.` as raw text while app-sdk drew the banner.
 * Since chat-core P5-a the page dispatches every row through the app-sdk
 * registry, and since P5-c it keeps NO `mcp_oauth` entry of its own: the row
 * resolves to the registry default (`renderMcpOAuthMessage` behind
 * `ctx.hideCardOwnedOAuth`), the same entry every pane and embed reads. What
 * is left to pin is therefore the opposite of the old contract --
 *
 *  1. the default registry still claims the role and still draws the banner;
 *  2. the page does not shadow it (no host entry claiming `mcp_oauth`, no
 *     private call to the banner renderer);
 *  3. the page feeds the registry the card-owned suppression flag from the
 *     shared `connections_ui` hook, so chat and the Connections gallery cannot
 *     disagree about whether a card already owns the prompt.
 *
 * Source-contract form: ChatPage's message list is driven by the custom
 * virtualizer (useVirtualChat), which mounts an empty window under jsdom (no
 * layout engine), so a full-page render produces no message DOM. The banner's
 * own rendering is covered by McpOAuthBanner.test.tsx; the default entry's
 * gating by ChatMessageList.test.tsx.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { defaultMessageRenderers, resolveRenderer } from '../app-sdk/messageRenderers'
import type { ChatMessage } from '../types'

const here = dirname(fileURLToPath(import.meta.url))
const chatPageSrc = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')

function rendererBlock(): string {
  const start = chatPageSrc.indexOf('fallback: bubbleRenderer } = useMemo')
  const end = chatPageSrc.indexOf('const renderMessage = useCallback', start)
  if (start < 0 || end < 0) throw new Error('ChatPage renderer block not found -- did the P5-a dispatch move?')
  return chatPageSrc.slice(start, end)
}

describe('ChatPage – MCP OAuth banner wiring', () => {
  it('the registry default claims mcp_oauth and draws through renderMcpOAuthMessage', () => {
    const entry = defaultMessageRenderers.find(r => r.roles.includes('mcp_oauth'))
    expect(entry?.id).toBe('mcp_oauth')
    const m: ChatMessage = {
      role: 'mcp_oauth',
      content: '🔐 notion requires authentication.',
      cls: '',
      ts: '2026-09-08T00:00:00.000Z',
      meta: { server_name: 'notion', oauth_url: 'https://example.com/authorize' },
    }
    expect(resolveRenderer(m, defaultMessageRenderers)?.id).toBe('mcp_oauth')
    // Under vitest's SSR transform the import reads as
    // `(0,__vite_ssr_import_N__.renderMcpOAuthMessage)(m, ctx.hideCardOwnedOAuth)`,
    // so match the call shape, not the bare identifier.
    expect(entry!.render.toString()).toMatch(/renderMcpOAuthMessage\)?\(\s*m,\s*ctx\.hideCardOwnedOAuth\s*\)/)
  })

  it('ChatPage keeps no mcp_oauth entry of its own (P5-c): the row reaches the registry default', () => {
    // A page entry claiming the role would shadow the shared banner on this
    // surface alone -- the fork 4e555e3 turned into raw text. The page must
    // neither register the role nor call the banner renderer itself.
    expect(rendererBlock()).not.toMatch(/id:\s*'mcp_oauth'/)
    expect(rendererBlock()).not.toMatch(/roles:\s*\[[^\]]*'mcp_oauth'/)
    expect(chatPageSrc).not.toMatch(/\brenderMcpOAuthMessage\b/)
    expect(chatPageSrc).not.toMatch(/from\s*['"][^'"]*McpOAuthBanner['"]/)
  })

  it('no page entry claims the role by a broader match either', () => {
    // The page's shape entries claim `'*'`; each carries a `match` that a bare
    // mcp_oauth row must not satisfy, or the banner would be swallowed before
    // the registry default is reached. Pinned by NAME: the two `'*'` entries the
    // page writes are the invisible-assistant skip and nothing else -- a new
    // `'*'` entry has to be added here with its non-matching guard reasoned.
    const stars = [...rendererBlock().matchAll(/id: '([a-z_]+)',\s*\n\s*roles: \['\*'\]/g)].map(x => x[1])
    expect(stars).toEqual(['hidden_invisible_assistant'])
  })

  /**
   * Chat hides a card-owned banner only while the Connections gallery is
   * reachable. Hardcoding that argument, or dropping it, would take the only
   * authorize prompt away from every install whose gallery flag is off — the
   * live regression this wiring exists to prevent. The flag must come from the
   * shared hook and reach the registry through the render context.
   */
  it('gates the card-owned suppression on the shared connections_ui flag, through ctx', () => {
    expect(chatPageSrc).toMatch(
      /import\s*\{\s*useConnectionsUiEnabled\s*\}\s*from\s*['"][^'"]*useConnectionsUi['"]/,
    )
    expect(chatPageSrc).toMatch(/const\s+connectionsUiOn\s*=\s*useConnectionsUiEnabled\(\)/)
    expect(chatPageSrc).toMatch(/hideCardOwnedOAuth:\s*connectionsUiOn\s*,/)
  })
})
