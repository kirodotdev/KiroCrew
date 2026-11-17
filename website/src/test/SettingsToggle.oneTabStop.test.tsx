/**
 * A settings switch row is one keyboard stop, and that stop says what it is.
 *
 * The row used to be a button with the switch inside it. Every row then took
 * two Tab presses, and the first stop was read out as "label, description,
 * button" with no on or off. The row is now a plain container that handles
 * nothing. The switch is the single stop and carries the name, the row's
 * description and the state; the name beside it is its <label>, so a mouse
 * press on it presses the switch. (The label is stretched over the whole row
 * in CSS; jsdom lays nothing out, so that part is checked in a browser.)
 */
import type { MouseEvent } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SettingsToggle } from '../components/settings'
import ReportProblemModal from '../components/ReportProblemModal'
import { renderWithProviders } from './helpers'

describe('SettingsToggle keyboard and screen-reader path', () => {
  it('takes one Tab press per row', async () => {
    const user = userEvent.setup()
    render(
      <>
        <button type="button">Before</button>
        <SettingsToggle label="First" description="What the first one does" checked={false} onChange={() => {}} />
        <SettingsToggle label="Second" checked onChange={() => {}} />
        <button type="button">After</button>
      </>,
    )
    screen.getByRole('button', { name: 'Before' }).focus()
    await user.tab()
    expect(screen.getByRole('switch', { name: 'First' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('switch', { name: 'Second' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus()
  })

  it('is not a button around the switch', () => {
    render(<SettingsToggle label="Only a switch" description="Explains it" checked={false} onChange={() => {}} />)
    const sw = screen.getByRole('switch', { name: 'Only a switch' })
    expect(sw.closest('[role="button"]')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('reads the state and the row description at the switch', () => {
    render(
      <>
        <SettingsToggle label="Described" description="Sends a ping every hour" checked onChange={() => {}} describedBy="extra-note" />
        <p id="extra-note">Restarts the service</p>
      </>,
    )
    const sw = screen.getByRole('switch', { name: 'Described' })
    expect(sw).toBeChecked()
    // The row's own text comes first, then the caller's note.
    expect(sw).toHaveAccessibleDescription('Sends a ping every hour Restarts the service')
  })

  it('leaves a row with no description undescribed', () => {
    render(<SettingsToggle label="Bare" checked={false} onChange={() => {}} />)
    expect(screen.getByRole('switch', { name: 'Bare' })).not.toHaveAttribute('aria-describedby')
  })

  it('flips once per press: Space, Enter, the label, the switch', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const { container } = render(<SettingsToggle label="Flip" description="Row text" checked={false} onChange={onChange} />)
    await user.tab()
    // The switch is a real button; its own press-to-click must not add a
    // second flip to the one the key handler makes.
    await user.keyboard(' ')
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenLastCalledWith(true)
    await user.keyboard('{Enter}')
    expect(onChange).toHaveBeenCalledTimes(2)
    const label = screen.getByText('Flip')
    expect(label.tagName).toBe('LABEL')
    expect(label).toHaveAttribute('for', screen.getByRole('switch').id)
    await user.click(label)
    expect(onChange).toHaveBeenCalledTimes(3)
    await user.click(screen.getByRole('switch'))
    expect(onChange).toHaveBeenCalledTimes(4)
    expect(container.firstElementChild).not.toHaveAttribute('tabindex')
    expect(container.firstElementChild).not.toHaveAttribute('role')
  })

  it('ignores a press on the label while disabled', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<SettingsToggle label="Off limits" description="Row text" checked={false} onChange={onChange} disabled />)
    await user.click(screen.getByText('Off limits'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('leaves a link in the description to do its own job', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onLink = vi.fn((e: MouseEvent) => e.preventDefault())
    render(
      <SettingsToggle
        label="With a link"
        description={<>Read <a href="https://example.com/doc" onClick={onLink}>the note</a> first.</>}
        checked={false}
        onChange={onChange}
      />,
    )
    await user.click(screen.getByRole('link', { name: 'the note' }))
    expect(onLink).toHaveBeenCalledTimes(1)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('keeps Enter on the "?" tip from flipping the setting', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<SettingsToggle label="With a tip" hint="Help text" checked={false} onChange={onChange} />)
    screen.getByRole('button', { name: 'More information' }).focus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Help text')
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('Report a problem: the logs switch has a name', () => {
  it('names the switch after its visible label and describes it with the hint', () => {
    renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)
    const sw = screen.getByRole('switch', { name: 'Include recent logs' })
    expect(sw).toBeChecked()
    expect(sw).toHaveAccessibleDescription()
    // No switch on the dialog is left nameless.
    for (const el of screen.getAllByRole('switch')) expect(el).toHaveAccessibleName()
  })
})
