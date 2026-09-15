// A switch refusal must reach the reader through the SHARED notice, not bespoke markup: the
// severity icon, the report lookup and the dismiss wording all live there.
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import AgentSwitchNotice from '../components/AgentSwitchNotice'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import { ApiError } from '../api/client'
import { initI18n } from '../i18n/all'
import { i18nT } from '../i18n/t'

initI18n('en')

const WORKSPACE_503 = new ApiError(
  503,
  'the configured workspace directory is unavailable',
  JSON.stringify({
    error: 'the configured workspace directory is unavailable',
    code: 'workspace_unavailable',
  }),
)

describe('the agent-switch notice', () => {
  it('renders the failure through the shared ErrorNotice surface', () => {
    render(<AgentSwitchNotice message={agentSwitchFailureMessage(WORKSPACE_503)} onDismiss={() => undefined} />)
    // The shared component's own testId. Asserted rather than the copy alone, because bespoke
    // markup can carry identical text while sharing none of the notice's behaviour.
    const notice = screen.getByTestId('agent-switch-error')
    expect(notice).toBeTruthy()
    // "project folder" deliberately: the control that fixes this reads "Project folder:
    // {{path}} - change", so a refusal naming a workspace sends the reader to no setting.
    expect(notice.textContent).toContain("project folder isn't available")
  })

  it('carries the withheld-not-broken severity, and its icon', () => {
    render(<AgentSwitchNotice message="A turn is running." onDismiss={() => undefined} />)
    const notice = screen.getByTestId('agent-switch-error')
    // warn, not the danger default: the switch was withheld, nothing broke.
    expect(notice.className).toContain('border-warn')
    expect(notice.className).not.toContain('border-danger')
    // Severity is encoded by icon too, never colour alone.
    expect(notice.querySelector('svg')).toBeTruthy()
  })

  it('announces once, so a nested live region cannot double-announce', () => {
    const { container } = render(
      <AgentSwitchNotice message="A turn is running." onDismiss={() => undefined} />,
    )
    const live = container.querySelectorAll('[role="alert"], [role="status"]')
    expect(live.length).toBe(1)
  })

  it('dismisses through the shared affordance', async () => {
    const onDismiss = vi.fn()
    render(<AgentSwitchNotice message="A turn is running." onDismiss={onDismiss} />)
    const notice = screen.getByTestId('agent-switch-error')
    // By label, not the first button: the hand-off renders ahead of dismiss in the same row.
    const dismiss = notice.querySelector<HTMLButtonElement>('button[aria-label]')
    expect(dismiss).toBeTruthy()
    dismiss!.click()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('offers the agent hand-off for an unavailable workspace, which needs a human look', () => {
    render(
      <AgentSwitchNotice
        message={i18nT('utils.agentSwitchFeedback.workspace_unavailable')}
        onDismiss={() => undefined}
      />,
    )
    const notice = screen.getByTestId('agent-switch-error')
    expect(Array.from(notice.querySelectorAll('button')).length).toBeGreaterThan(1)
  })

  it('withholds the hand-off while a turn is in flight, so the user is not moved off it', () => {
    render(
      <AgentSwitchNotice
        message={i18nT('utils.agentSwitchFeedback.turn_in_flight')}
        onDismiss={() => undefined}
      />,
    )
    const notice = screen.getByTestId('agent-switch-error')
    const buttons = Array.from(notice.querySelectorAll('button'))
    expect(buttons.length).toBe(1)
    expect(notice.textContent).not.toContain(i18nT('components.askAgent.ask_the_agent'))
  })

  it('renders nothing at all without a message', () => {
    const { container } = render(<AgentSwitchNotice message="" onDismiss={() => undefined} />)
    expect(container.firstChild).toBeNull()
  })

  it('keeps the floating wrapper opaque, since the notice tint is translucent', () => {
    render(<AgentSwitchNotice message="A turn is running." onDismiss={() => undefined} />)
    const wrapper = screen.getByTestId('agent-switch-notice')
    expect(wrapper.className).toContain('bg-bg-elevated')
    expect(wrapper.className).toContain('fixed')
  })

  it('gives the new workspace_unavailable refusal a recovery path, not a dead end', () => {
    // The refusal this change introduces cannot be acted on by the user -- the configured root is
    // not there -- so the surface reporting it must offer the agent hand-off.
    const message = agentSwitchFailureMessage(WORKSPACE_503)
    render(<AgentSwitchNotice message={message} onDismiss={() => undefined} />)
    const notice = screen.getByTestId('agent-switch-error')

    // Code-specific copy, not the generic failure line: the 503's `code` is what selected it.
    expect(notice.textContent).toContain("project folder isn't available")
    // More than dismiss alone, which is what a dead end looks like.
    const buttons = Array.from(notice.querySelectorAll('button'))
    expect(buttons.length).toBeGreaterThan(1)
    const labelled = buttons.filter(b => b.getAttribute('aria-label'))
    expect(labelled.length).toBeGreaterThan(0)
  })
})
