import { fireEvent, render } from '@testing-library/react'
import { vi } from 'vitest'
import { SettingsToggle } from './settings'

/**
 * A toggle with no gateway `configKey` can still carry a stable highlight
 * anchor (`data-setting-id`), so a deep link waits for a row that mounts late
 * (the desktop-app close row renders only after its IPC read, #12204).
 */
describe('SettingsToggle anchors', () => {
  it('renders settingId as data-setting-id beside the label anchor', () => {
    const { container } = render(
      <SettingsToggle label="Row" settingId="display.keep-running-in-the-background-after-closing-the-window" checked={false} onChange={() => {}} />,
    )
    const row = container.querySelector('[data-setting-id="display.keep-running-in-the-background-after-closing-the-window"]')
    expect(row).not.toBeNull()
    expect(row?.getAttribute('data-setting-label')).toBe('Row')
    expect(row?.hasAttribute('data-setting-key')).toBe(false)
  })

  it('drops every anchor, settingId included, when anchor is false', () => {
    const { container } = render(
      <SettingsToggle label="Row" settingId="display.keep-running-in-the-background-after-closing-the-window" anchor={false} checked={false} onChange={() => {}} />,
    )
    expect(container.querySelector('[data-setting-id]')).toBeNull()
    expect(container.querySelector('[data-setting-label]')).toBeNull()
  })

  it('ignores a press on the label while it saves and keeps the switch as the one busy stop', () => {
    // The row is not a control, so it carries no state of its own: no role, no
    // tab stop, and no `aria-busy` (on a container that can make a screen reader
    // skip the switch inside it). The switch is announced disabled and busy and
    // keeps its tab stop, so focus is not lost while the save runs.
    const onChange = vi.fn()
    const { container, getByRole, getByText } = render(
      <SettingsToggle label="Row" checked={false} onChange={onChange} busy />,
    )
    const row = container.querySelector('[data-setting-label="Row"]')!
    expect(row).not.toHaveAttribute('role')
    expect(row).not.toHaveAttribute('tabindex')
    expect(row).not.toHaveAttribute('aria-busy')
    // The label passes the press to the switch, which refuses it while busy.
    fireEvent.click(getByText('Row'))
    expect(onChange).not.toHaveBeenCalled()
    const sw = getByRole('switch')
    expect(sw).toHaveAttribute('tabindex', '0')
    expect(sw).toHaveAttribute('aria-busy', 'true')
    expect(sw).toHaveAttribute('aria-disabled', 'true')
  })

  it('flips on a press of its label when it is not busy, without the row being a button', () => {
    const onChange = vi.fn()
    const { container, getByRole, getByText } = render(
      <SettingsToggle label="Row" checked={false} onChange={onChange} />,
    )
    const row = container.querySelector('[data-setting-label="Row"]')!
    expect(row).not.toHaveAttribute('role')
    expect(row).not.toHaveAttribute('tabindex')
    expect(getByRole('switch')).not.toHaveAttribute('aria-disabled')
    fireEvent.click(getByText('Row'))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(true)
  })
})
