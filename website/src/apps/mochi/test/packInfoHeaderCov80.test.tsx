/**
 * PackInfoHeader — the shared metadata header for both pack editors.
 *
 * The flip-X control is the reason this has a test: it is a div with
 * `role="switch"`, so its keyboard path (Enter / Space, with preventDefault) is
 * hand-rolled — a regression there leaves the only way to flip a sprite as a
 * mouse click, and nothing about the rendered markup would look wrong.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { PackInfoHeader } from '../src/renderer/PackInfoHeader'

function setup(flipX = false) {
  const handlers = {
    onNameChange: vi.fn(),
    onAuthorChange: vi.fn(),
    onDescriptionChange: vi.fn(),
    onFlipXChange: vi.fn(),
  }
  render(
    <PackInfoHeader
      title={<span>zzq title</span>}
      name="zzq name"
      author="zzq author"
      description="zzq description"
      flipX={flipX}
      {...handlers}
    />,
  )
  return handlers
}

describe('PackInfoHeader', () => {
  it('renders the title node and the three current values', () => {
    setup()
    expect(screen.getByText('zzq title')).toBeTruthy()
    expect((screen.getByDisplayValue('zzq name') as HTMLInputElement).value).toBe('zzq name')
    expect(screen.getByDisplayValue('zzq author')).toBeTruthy()
    expect(screen.getByDisplayValue('zzq description')).toBeTruthy()
    expect(screen.getByText('Name *')).toBeTruthy()
    expect(screen.getByText('Author')).toBeTruthy()
    expect(screen.getByText('Character Description')).toBeTruthy()
  })

  it('reports each field edit to its own handler', () => {
    const h = setup()
    fireEvent.change(screen.getByDisplayValue('zzq name'), { target: { value: 'edited-name' } })
    fireEvent.change(screen.getByDisplayValue('zzq author'), { target: { value: 'edited-author' } })
    fireEvent.change(screen.getByDisplayValue('zzq description'), {
      target: { value: 'edited-desc' },
    })
    expect(h.onNameChange).toHaveBeenCalledWith('edited-name')
    expect(h.onAuthorChange).toHaveBeenCalledWith('edited-author')
    expect(h.onDescriptionChange).toHaveBeenCalledWith('edited-desc')
  })

  it('exposes flipX as a real switch with an accessible name', () => {
    setup(true)
    const sw = screen.getByRole('switch', { name: 'Flip Horizontal' })
    expect(sw.getAttribute('aria-checked')).toBe('true')
    expect(sw.getAttribute('tabindex')).toBe('0')
  })

  it('toggles on click, sending the OPPOSITE of the current value', () => {
    const h = setup(false)
    fireEvent.click(screen.getByRole('switch'))
    expect(h.onFlipXChange).toHaveBeenCalledWith(true)
  })

  it('toggles on Enter and on Space, and on nothing else', () => {
    const h = setup(true)
    const sw = screen.getByRole('switch')
    fireEvent.keyDown(sw, { key: 'Enter' })
    fireEvent.keyDown(sw, { key: ' ' })
    expect(h.onFlipXChange).toHaveBeenCalledTimes(2)
    expect(h.onFlipXChange).toHaveBeenLastCalledWith(false)

    fireEvent.keyDown(sw, { key: 'a' })
    fireEvent.keyDown(sw, { key: 'Tab' })
    expect(h.onFlipXChange).toHaveBeenCalledTimes(2)
  })
})
