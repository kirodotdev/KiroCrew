import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { SettingsCard, SettingsGroupHeader, SettingsSection, SettingsSelect, SettingsInput } from '../components/settings'
import { Card, CardFrameContext, CardTitle } from '../components/ui'

/**
 * The Settings group layout (website/docs/page-layout.md, "Settings groups"):
 * a heading and description ABOVE one group box, never inside it; small
 * controls on one row; text typed in order stacked.
 */
const box = (el: HTMLElement) => el.closest('[data-settings-box]')

describe('Settings grouped layout', () => {
  it('SettingsSection draws its title and description outside the box', () => {
    render(
      <SettingsSection title="Group" description="One line about it">
        <SettingsCard><div>Row</div></SettingsCard>
      </SettingsSection>,
    )
    expect(box(screen.getByText('Group'))).toBeNull()
    expect(box(screen.getByText('One line about it'))).toBeNull()
    expect(box(screen.getByText('Row'))).not.toBeNull()
  })

  it('SettingsGroupHeader renders a heading and its description', () => {
    render(<SettingsGroupHeader title="Fallbacks" description="Tried in order" />)
    expect(screen.getByRole('heading', { name: 'Fallbacks' })).toBeInTheDocument()
    expect(screen.getByText('Tried in order')).toBeInTheDocument()
  })

  it('a grouped Card lifts its CardTitle and lede above the box', () => {
    render(
      <CardFrameContext.Provider value="grouped">
        <Card>
          <CardTitle>Subagents</CardTitle>
          <p>Changes apply when you press Save.</p>
          <div>Max turns</div>
        </Card>
      </CardFrameContext.Provider>,
    )
    expect(box(screen.getByText('Subagents'))).toBeNull()
    expect(box(screen.getByText('Changes apply when you press Save.'))).toBeNull()
    expect(box(screen.getByText('Max turns'))).not.toBeNull()
  })

  it('a grouped Card keeps a lone paragraph inside, so the box is never empty', () => {
    render(
      <CardFrameContext.Provider value="grouped">
        <Card>
          <CardTitle>Memory Graph</CardTitle>
          <p>No memory data yet.</p>
        </Card>
      </CardFrameContext.Provider>,
    )
    expect(box(screen.getByText('No memory data yet.'))).not.toBeNull()
  })

  it('a grouped Card paints a caller style on the box, not on the wrapper', () => {
    render(
      <CardFrameContext.Provider value="grouped">
        <Card style={{ background: 'red' }}><div>Hero</div></Card>
      </CardFrameContext.Provider>,
    )
    expect((box(screen.getByText('Hero')) as HTMLElement).style.background).toBe('red')
  })

  it('inside a group box a select row goes inline and wraps; a text input stays stacked', () => {
    const { container } = render(
      <SettingsCard>
        <SettingsSelect label="Pick" value="a" options={['a', 'b']} onChange={() => {}} />
        <SettingsInput label="Name" value="" onChange={() => {}} />
      </SettingsCard>,
    )
    const rows = container.querySelectorAll('[data-setting-label]')
    // Inline is keyed on the box, never on a viewport breakpoint, and wraps
    // instead of clipping when the pane is narrow.
    expect(rows[0].className).toContain('in-[[data-settings-box]]:flex-row')
    expect(rows[0].className).toContain('in-[[data-settings-box]]:flex-wrap')
    expect(rows[0].className).not.toMatch(/\bsm:/)
    expect(rows[1].className).not.toContain('flex-row')
  })

  it('a select trigger carries its selected label as a title', () => {
    render(<SettingsSelect label="Pick" value="b" options={['a', 'b']} optionLabels={['Short', 'A much longer selected label']} onChange={() => {}} />)
    expect(screen.getByLabelText('Pick')).toHaveAttribute('title', 'A much longer selected label')
  })
})
