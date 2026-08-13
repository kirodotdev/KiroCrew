/**
 * A chip row animates its collapse by injecting a closing stylesheet into Pierre
 * (`fccHide`, `animation-fill-mode: forwards`) and holding the body mounted for
 * one animation before Pierre drops it. The hazard is the window in between: if
 * the row is reopened while `closing` is still set, the hide animation keeps
 * running against a row that is now open, so it snaps shut and springs back.
 *
 * The state is only observable through the options handed to Pierre, so this
 * mocks that component to record them — and renders the header-prefix slot, which
 * is where the chevron lives.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup, waitFor } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({ options: [] as { collapsed: boolean; unsafeCSS: string }[] }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ options, renderHeaderPrefix }: {
    options: { collapsed: boolean; unsafeCSS: string }
    renderHeaderPrefix?: () => React.ReactNode
  }) => {
    hoisted.options.push(options)
    return <div data-testid="pierre-pair">{renderHeaderPrefix?.()}</div>
  },
}))

import FileChangeChips from '../components/FileChangeChips'

const latest = () => hoisted.options[hoisted.options.length - 1]

beforeEach(() => {
  hoisted.options.length = 0
  cleanup()
})

describe('chip row collapse animation', () => {
  it('starts collapsed with no closing animation armed', () => {
    render(<FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />)
    expect(latest().collapsed).toBe(true)
    expect(latest().unsafeCSS).not.toContain('fccHide')
  })

  it('does not leave the hide animation armed when a collapsing row is reopened', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const chevron = container.querySelector('[data-testid="fcc-toggle-/a.ts"]')!

    fireEvent.click(chevron)                      // open
    expect(latest().collapsed).toBe(false)
    expect(latest().unsafeCSS).not.toContain('fccHide')

    fireEvent.click(chevron)                      // collapsing — hide armed
    expect(latest().unsafeCSS).toContain('fccHide')

    // Reopen inside the animation window, before the timer clears `closing`.
    fireEvent.click(chevron)
    expect(latest().collapsed).toBe(false)
    expect(latest().unsafeCSS, 'reopening must disarm the collapse animation')
      .not.toContain('fccHide')
  })

  it('disarms the hide animation once the collapse animation has run', async () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const chevron = container.querySelector('[data-testid="fcc-toggle-/a.ts"]')!

    fireEvent.click(chevron)  // open
    fireEvent.click(chevron)  // collapsing — hide armed for one animation
    expect(latest().unsafeCSS).toContain('fccHide')

    // The row is held mounted for ROW_ANIM_MS so the collapse has a frame to
    // animate in; after that the stylesheet must come back off, or a later
    // reopen inherits a `forwards` hide that keeps the body at max-height 0.
    await waitFor(() => expect(latest().unsafeCSS).not.toContain('fccHide'), { timeout: 2000 })
    expect(latest().collapsed).toBe(true)
  })
})
