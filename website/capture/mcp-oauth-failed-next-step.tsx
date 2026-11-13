/**
 * The failed MCP sign-in notice in the two hand-off modes, from the REAL
 * `renderMcpOAuthMessage` out of `src/`:
 *
 *   data-state="handoff-off"  ChatEmbed / ChatPane (the list's `handOff={false}`)
 *   data-state="handoff-on"   the chat page (default)
 *
 * The before/after difference comes from which version of McpOAuthBanner.tsx
 * is on disk, never from classes written here (Tailwind does not scan
 * `capture/`). See scripts/capture-mcp-oauth-failed-next-step.mjs.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { renderMcpOAuthMessage } from '../src/pages/chat/McpOAuthBanner'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const FAILED: ChatMessage = {
  role: 'mcp_oauth',
  content: '',
  cls: '',
  ts: '1',
  meta: { server_name: 'github-mcp', failed: true, error: 'token exchange rejected' },
}

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text flex flex-col gap-3 p-5" style={{ width: 620 }}>
    <div data-state="handoff-off">{renderMcpOAuthMessage(FAILED, false, false)}</div>
    <div data-state="handoff-on">{renderMcpOAuthMessage(FAILED)}</div>
  </div>,
)
