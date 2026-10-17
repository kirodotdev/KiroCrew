// A switch refusal must reach the reader through the SHARED notice, not bespoke markup: the
// severity icon and the dismiss wording both live there.
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import AgentSwitchNotice from '../components/AgentSwitchNotice'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import { ApiError } from '../api/client'
import { initI18n } from '../i18n/all'
import { i18nT } from '../i18n/t'

initI18n('en')

describe('the agent-switch notice', () => {
  it('renders the failure through the shared ErrorNotice surface', () => {
    render(
      <AgentSwitchNotice
        message={agentSwitchFailureMessage(new ApiError(400, 'no such agent: nope', '{}'))}
        onDismiss={() => undefined}
      />,
    )
    // The shared component's own testId. Asserted rather than the copy alone, because bespoke
    // markup can carry identical text while sharing none of the notice's behaviour.
    expect(screen.getByTestId('agent-switch-error')).toBeTruthy()
  })

  it('reports a WITHHELD switch as a failure too, not a polite status', () => {
    // A refusal that withheld the switch is still a rejected request carrying a backend
    // `{ error, code }` body, so it keeps danger chrome and `role="alert"`:
    // `errors-use-error-notice` (blocking) counts a warn- or status-toned failure as the same
    // violation as a hand-rolled red div. Pinned on the LOCALIZED refusal, the one this module
    // actually produces, so the assertion cannot drift from the live copy.
    render(
      <AgentSwitchNotice
        message={i18nT('utils.agentSwitchFeedback.turn_in_flight')}
        onDismiss={() => undefined}
      />,
    )
    const notice = screen.getByTestId('agent-switch-error')
    expect(notice.className).toContain('border-danger')
    expect(notice.className).not.toContain('border-warn')
    expect(notice.querySelector('[role="alert"]') ?? notice.closest('[role="alert"]')).toBeTruthy()
    expect(notice.querySelector('[role="status"]')).toBeNull()
    // Severity is encoded by icon too, never colour alone.
    expect(notice.querySelector('svg')).toBeTruthy()
  })

  it('keeps danger severity for a switch that genuinely broke, not only withheld', () => {
    render(
      <AgentSwitchNotice
        message={agentSwitchFailureMessage(new ApiError(400, 'no such agent: nope', '{}'))}
        onDismiss={() => undefined}
      />,
    )
    const notice = screen.getByTestId('agent-switch-error')
    expect(notice.className).toContain('border-danger')
    expect(notice.className).not.toContain('border-warn')
    expect(notice.querySelector('[role="alert"]') ?? notice.closest('[role="alert"]')).toBeTruthy()
  })

  it('announces once, so a nested live region cannot double-announce', () => {
    const { container } = render(
      <AgentSwitchNotice message="A turn is running." onDismiss={() => undefined} />,
    )
    const live = container.querySelectorAll('[role="alert"], [role="status"]')
    expect(live.length).toBe(1)
  })

  it('dismisses through the shared affordance', () => {
    const onDismiss = vi.fn()
    render(<AgentSwitchNotice message="A turn is running." onDismiss={onDismiss} />)
    const notice = screen.getByTestId('agent-switch-error')
    const dismiss = notice.querySelector<HTMLButtonElement>('button[aria-label]')
    expect(dismiss).toBeTruthy()
    dismiss!.click()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('withholds the hand-off, since a draft can be mounted under this floating notice', () => {
    render(
      <AgentSwitchNotice
        message={i18nT('utils.agentSwitchFeedback.turn_in_flight')}
        onDismiss={() => undefined}
      />,
    )
    const notice = screen.getByTestId('agent-switch-error')
    expect(Array.from(notice.querySelectorAll('button')).length).toBe(1)
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

  it('routes a session-folder failure through ErrorNotice, since the backend sets it only inside except SessionFolderError', () => {
    render(
      <AgentSwitchNotice
        message={i18nT('store.chatSlice.session_folder_unavailable')}
        onDismiss={() => undefined}
      />,
    )
    const notice = screen.getByTestId('agent-switch-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.className).toContain('border-danger')
    expect(screen.queryByTestId('agent-switch-status')).toBeNull()
    const wrapper = screen.getByTestId('agent-switch-notice')
    expect(wrapper.querySelectorAll('[role="alert"], [role="status"]').length).toBe(1)
  })
})
