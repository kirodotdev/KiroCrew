import { describe, it, expect } from 'vitest'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

function remoteCommand(output: string): ChatMessage {
  return {
    role: 'tool',
    content: '🔧 run_command',
    cls: '',
    meta: { tool_call_id: 'g', mcp_server: 'kirocrew-workspace', input: '{"command":"gh pr create --title x"}', output, done: true },
  }
}

function render(message: ChatMessage) {
  const store = createTestStore({ chat: { messages: [message], toolLog: [], slotRunning: false } as unknown as ChatState })
  return renderWithProviders(<ToolCallLine message={message} running={false} />, { store })
}

describe('ToolCallLine gh proxy tag', () => {
  it('labels a remote command whose gh ran on this machine, with the login it ran as', () => {
    const { getByTestId } = render(remoteCommand(
      '[gh-proxy · ran on your machine as @octo-owner: gh pr create (acme/widgets)]\nhttps://github.com/acme/widgets/pull/7\n[exit 0]',
    ))
    const tag = getByTestId('tool-gh-proxy-tag')
    expect(tag.textContent).toBe('gh-proxy· ran on your machine as your GitHub account @octo-owner')
    expect(tag.getAttribute('title')).toContain("gh proxy, with your GitHub account")
  })

  it('leaves a plain workspace command unlabelled', () => {
    const { queryByTestId } = render(remoteCommand('hello\n[gh-proxy · ran on your machine as @x: gh pr view (a/b)]\n[exit 0]'))
    expect(queryByTestId('tool-gh-proxy-tag')).toBeNull()
  })
})
