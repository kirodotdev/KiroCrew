import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import type { ChatInputProps } from '../../components/chat-input/props'

/* The Crewmates page's composer is the shared ChatInput minus the session
 * toolbar line. ChatInput is stubbed to record what reaches it: every core
 * piece (text, send, attachments, steer/queue) arrives as given, and none of
 * the agent / model / effort / context / approval chrome does. */

const seen = vi.hoisted(() => ({ props: null as Record<string, unknown> | null }))
vi.mock('../../components/ChatInput', () => ({
  default: (props: Record<string, unknown>) => {
    seen.props = props
    return <div data-testid="chat-input-stub" />
  },
}))

import CrewComposer, { SESSION_CHROME_PROPS } from './CrewComposer'

const noop = () => {}
const CHROME: Partial<ChatInputProps> = {
  onAgentClick: noop, agentLabel: 'oncall', agentIsInheritedDefault: false, agentSource: 'kirocrew',
  onModelClick: noop, modelName: 'claude-opus-5', modelIsInheritedDefault: true, modelIsAutoChosen: false, modelIsJevRouted: false,
  reasoningEffort: 'high', effortIsDefault: false, hasEffort: true,
  contextPct: 42, contextUsedTokens: 4200, contextWindowTokens: 10000,
  approvalMode: 'trust',
}

describe('CrewComposer', () => {
  it('forwards the shared composer pieces and drops the session toolbar line', () => {
    const onSend = vi.fn()
    const onUploadFiles = vi.fn()
    const onSteer = vi.fn()
    render(
      <CrewComposer
        value="hi"
        onChange={noop}
        onSend={onSend}
        onUploadFiles={onUploadFiles}
        pendingFiles={['a.txt']}
        canSteer
        onSteer={onSteer}
        isRunning={false}
        agentName="oncall"
        placeholder="Message oncall…"
        {...CHROME}
      />,
    )
    const got = seen.props!
    expect(got.value).toBe('hi')
    expect(got.onSend).toBe(onSend)
    expect(got.onUploadFiles).toBe(onUploadFiles)
    expect(got.pendingFiles).toEqual(['a.txt'])
    expect(got.onSteer).toBe(onSteer)
    expect(got.placeholder).toBe('Message oncall…')
    // The raw agent alias still feeds the slash-command / skills lookup.
    expect(got.agentName).toBe('oncall')
    for (const key of SESSION_CHROME_PROPS) expect(got).not.toHaveProperty(key)
  })

  it('names every chrome prop the test passes, so a new chip cannot slip through untested', () => {
    expect([...SESSION_CHROME_PROPS].sort()).toEqual(Object.keys(CHROME).sort())
  })
})
