import { render } from '@testing-library/react'
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
})
