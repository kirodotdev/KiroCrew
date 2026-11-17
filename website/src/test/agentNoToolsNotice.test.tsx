import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'

import type { ChatMessage } from '../types'
import { agentNoToolsAgent, resolveGatewayNotice } from '../pages/chat/gatewayNotice'
import AgentNoToolsNotice from '../pages/chat/AgentNoToolsNotice'
import { agentTemplateHref } from '../pages/overview/agentTemplateLink'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import en from '../i18n/locales/en.json'
import '../i18n/all'

/**
 * The chat notice for an agent that lists no tools
 * (`dashboard/agent_no_tools_notice.py`): worded from its token, never from the
 * row's English, and followed by a link to that agent's page under Customize.
 */

const row = (over: Partial<ChatMessage> = {}): ChatMessage =>
  ({
    role: 'notice',
    content: 'fallback prose the dashboard never shows',
    cls: '',
    meta: { kind: 'agent_no_tools', notice: 'agent_no_tools', notice_args: { agent: 'research helper' } },
    ...over,
  }) as ChatMessage

function Where() {
  const loc = useLocation()
  return <span data-testid="where">{loc.pathname + loc.search}</span>
}

afterEach(cleanup)

describe('agent no-tools notice', () => {
  it('is matched on its token and names the agent', () => {
    expect(agentNoToolsAgent(row())).toBe('research helper')
    expect(agentNoToolsAgent(row({ meta: { notice: 'tools_not_ready' } } as Partial<ChatMessage>))).toBeNull()
    expect(agentNoToolsAgent(row({ meta: { notice: 'agent_no_tools' } } as Partial<ChatMessage>))).toBeNull()
    expect(agentNoToolsAgent(row({ role: 'error' }))).toBeNull()
  })

  it.each([
    ['main transcript registry', mergeRenderers(createTranscriptRenderers({ slot: 's1' }))],
    ['app-sdk default registry', defaultMessageRenderers],
  ])('%s draws it with its own renderer, not the plain notice card', (_name, registry) => {
    expect(resolveRenderer(row(), registry)?.id).toBe('agent_no_tools')
    expect(resolveRenderer(row({ meta: { notice: 'tools_not_ready' } } as Partial<ChatMessage>), registry)?.id).toBe('notice')
  })

  it('reads in plain words, with no key name or JSON to type', () => {
    const text = resolveGatewayNotice(row(), [], 0)?.text ?? ''
    expect(text).toBe(en.pages.chat.gatewayNotice.agent_no_tools.replace('{{agent}}', 'research helper'))
    expect(text).not.toMatch(/"tools"|\[|\*/)
  })

  it('opens that agent on the Custom agents tab', () => {
    render(
      <MemoryRouter initialEntries={['/chat/one']}>
        <AgentNoToolsNotice content="copy" agent="research helper" />
        <Where />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByTestId('agent-no-tools-open'))
    expect(screen.getByTestId('where').textContent).toBe(agentTemplateHref('research helper'))
    expect(agentTemplateHref('research helper')).toBe('/capabilities?tab=templates&template=research%20helper')
  })

  it('draws the copy alone where there is no page to open', () => {
    render(<AgentNoToolsNotice content="copy" agent="research helper" />)
    expect(screen.getByTestId('notice-card-text').textContent).toContain('copy')
    expect(screen.queryByTestId('agent-no-tools-open')).toBeNull()
  })
})
