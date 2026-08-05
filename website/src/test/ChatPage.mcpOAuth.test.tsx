/**
 * Guards the MCP OAuth banner wiring in ChatPage.
 *
 * ChatPage.renderMessage must route messages with role 'mcp_oauth' to
 * renderMcpOAuthMessage so the Authorize banner renders inline. If that branch
 * (or its import) is dropped, the message falls through to AssistantMessage and
 * the raw "🔐 … requires authentication." text is shown instead of the banner.
 *
 * This is a source-contract test: ChatPage's message list is driven by the
 * custom virtualizer (useVirtualChat), which mounts an empty window under jsdom
 * (no layout engine), so a full-page render produces no message DOM. The
 * banner's rendering behaviour is covered by McpOAuthBanner.test.tsx; this test
 * locks in the wiring that connects the role to that renderer.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const chatPageSrc = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')

describe('ChatPage – MCP OAuth banner wiring', () => {
  it('imports the OAuth banner renderer', () => {
    expect(chatPageSrc).toMatch(
      /import\s*\{\s*renderMcpOAuthMessage\s*\}\s*from\s*['"][^'"]*McpOAuthBanner['"]/,
    )
  })

  it('routes the mcp_oauth message role to the banner renderer', () => {
    expect(chatPageSrc).toMatch(/role\s*===\s*['"]mcp_oauth['"]/)
    expect(chatPageSrc).toMatch(/renderMcpOAuthMessage\s*\(/)
  })

  it('keeps the banner branch and its renderer call in the same render path', () => {
    const idxRole = chatPageSrc.search(/role\s*===\s*['"]mcp_oauth['"]/)
    const idxCall = chatPageSrc.indexOf('renderMcpOAuthMessage(')
    expect(idxRole).toBeGreaterThanOrEqual(0)
    expect(idxCall).toBeGreaterThanOrEqual(0)
    expect(Math.abs(idxCall - idxRole)).toBeLessThan(400)
  })
})
