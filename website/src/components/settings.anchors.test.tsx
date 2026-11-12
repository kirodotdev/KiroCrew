import { fireEvent, render, within } from '@testing-library/react'
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

  it('announces the row as disabled and busy, out of the tab order, while it saves', () => {
    // A screen reader must not announce the clickable row as an active button
    // that does nothing while the save runs: it is aria-disabled + aria-busy and
    // not a tab stop, and a press is ignored. The inner switch keeps its own tab
    // stop (its own busy), so focus is not lost.
    const onChange = vi.fn()
    const { getByRole } = render(
      <SettingsToggle label="Row" checked={false} onChange={onChange} busy />,
    )
    const row = getByRole('button')
    expect(row).toHaveAttribute('aria-disabled', 'true')
    expect(row).toHaveAttribute('aria-busy', 'true')
    expect(row).toHaveAttribute('tabindex', '-1')
    fireEvent.click(row)
    expect(onChange).not.toHaveBeenCalled()
    // The switch stays focusable and is announced busy, so focus is kept.
    const sw = within(row).getByRole('switch')
    expect(sw).toHaveAttribute('tabindex', '0')
    expect(sw).toHaveAttribute('aria-busy', 'true')
  })

  it('leaves the row an active button when it is not busy', () => {
    const onChange = vi.fn()
    const { getByRole } = render(
      <SettingsToggle label="Row" checked={false} onChange={onChange} />,
    )
    const row = getByRole('button')
    expect(row).not.toHaveAttribute('aria-disabled')
    expect(row).not.toHaveAttribute('aria-busy')
    expect(row).toHaveAttribute('tabindex', '0')
    fireEvent.click(row)
    expect(onChange).toHaveBeenCalledWith(true)
  })
})
