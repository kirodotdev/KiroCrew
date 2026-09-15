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
import { useEffect, useRef } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup, waitFor } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({
  options: [] as { collapsed: boolean; unsafeCSS: string }[],
  renderPrefix: true,
  prefixVisible: true,
}))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ options, fallbackText, fallbackClassName, onVisible, renderHeaderPrefix }: {
    options: { collapsed: boolean; unsafeCSS: string }
    fallbackText?: string
    fallbackClassName?: string
    onVisible?: () => void
    renderHeaderPrefix?: () => React.ReactNode
  }) => {
    hoisted.options.push(options)
    const visibleNotified = useRef(false)
    useEffect(() => {
      if (!hoisted.prefixVisible) {
        visibleNotified.current = false
        return
      }
      if (visibleNotified.current) return
      visibleNotified.current = true
      onVisible?.()
    })
    return (
      <div data-testid="pierre-pair">
        {hoisted.renderPrefix
          ? <span style={{ visibility: hoisted.prefixVisible ? 'visible' : 'hidden' }}>{renderHeaderPrefix?.()}</span>
          : <pre data-fcc-warm-body className={fallbackClassName}>{fallbackText}</pre>}
      </div>
    )
  },
}))

import FileChangeChips from '../components/FileChangeChips'

const latest = () => hoisted.options[hoisted.options.length - 1]

beforeEach(() => {
  hoisted.options.length = 0
  hoisted.renderPrefix = true
  hoisted.prefixVisible = true
  cleanup()
})

describe('chip row collapse animation', () => {
  it('starts closed without mounting Pierre', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    expect(hoisted.options).toHaveLength(0)
    expect(container.querySelector('[data-testid="pierre-pair"]')).toBeNull()
  })

  it('does not leave the hide animation armed when a collapsing row is reopened', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const chevron = () => container.querySelector('[data-testid="fcc-toggle-/a.ts"]')!

    fireEvent.click(chevron())                    // open
    expect(latest().collapsed).toBe(false)
    expect(latest().unsafeCSS).not.toContain('fccHide')

    fireEvent.click(chevron())                      // collapsing — hide armed
    expect(latest().unsafeCSS).toContain('fccHide')

    // Reopen inside the animation window, before the timer clears `closing`.
    fireEvent.click(chevron())
    expect(latest().collapsed).toBe(false)
    expect(latest().unsafeCSS, 'reopening must disarm the collapse animation')
      .not.toContain('fccHide')
  })

  it('restores focus when the row is reopened before collapse unmounts Pierre', async () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const chevron = () => container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!

    chevron().focus()
    fireEvent.click(chevron())
    await waitFor(() => expect(document.activeElement).toBe(chevron()))

    chevron().focus()
    fireEvent.click(chevron())
    expect(document.activeElement).toBe(row)
    expect(row).toHaveAttribute('role', 'button')

    fireEvent.keyDown(row, { key: 'Enter' })
    await waitFor(() => expect(document.activeElement).toBe(chevron()))
    expect(latest().unsafeCSS).not.toContain('fccHide')
    expect(row).not.toHaveAttribute('role')
    expect(row).not.toHaveAttribute('tabindex')
  })

  it('disarms the hide animation once the collapse animation has run', async () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const chevron = () => container.querySelector('[data-testid="fcc-toggle-/a.ts"]')!

    fireEvent.click(chevron())  // open
    fireEvent.click(chevron())  // collapsing — hide armed for one animation
    expect(latest().unsafeCSS).toContain('fccHide')

    // The row is held mounted for ROW_ANIM_MS so the collapse has a frame to
    // animate in; after that the stylesheet must come back off, or a later
    // reopen inherits a `forwards` hide that keeps the body at max-height 0.
    await waitFor(
      () => expect(container.querySelector('[data-testid="pierre-pair"]')).toBeNull(),
      { timeout: 2000 },
    )
    expect(container.querySelector('[data-testid^="fcc-header-"]')).not.toBeNull()
  })

  it('hands focus to Pierre’s chevron after the lightweight header unmounts', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const before = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    before.focus()
    fireEvent.click(before)

    const after = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    expect(after).not.toBe(before)
    expect(document.activeElement).toBe(after)
  })

  it('keeps the row proxy until WarmSwap reveals Pierre’s chevron', async () => {
    hoisted.prefixVisible = false
    const fileChanges = [{ path: '/a.ts', before: 'a', after: 'a\nb' }]
    const { container, rerender } = render(<FileChangeChips fileChanges={fileChanges} />)
    const before = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!
    before.focus()
    fireEvent.click(before)

    const hiddenChevron = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    expect(hiddenChevron).not.toBe(before)
    expect(document.activeElement).toBe(row)
    expect(row).toHaveAttribute('role', 'button')
    expect(row).toHaveAttribute('tabindex', '-1')

    hoisted.prefixVisible = true
    rerender(<FileChangeChips fileChanges={[...fileChanges]} />)
    await waitFor(() => expect(document.activeElement).toBe(hiddenChevron))
    expect(row).not.toHaveAttribute('role')
    expect(row).not.toHaveAttribute('tabindex')
  })

  it('keeps focus on the row until Pierre’s lazy chevron mounts', () => {
    hoisted.renderPrefix = false
    const fileChanges = [{ path: '/a.ts', before: 'a', after: 'a\nb' }]
    const { container, rerender } = render(<FileChangeChips fileChanges={fileChanges} />)
    const before = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!
    const label = before.getAttribute('aria-label')
    before.focus()
    fireEvent.click(before)

    expect(document.activeElement).toBe(row)
    expect(row).toHaveAttribute('tabindex', '-1')
    expect(row).toHaveAttribute('role', 'button')
    expect(row).toHaveAttribute('aria-label', label)
    expect(row).toHaveAttribute('aria-expanded', 'true')
    const warmBody = container.querySelector('[data-fcc-warm-body]')
    expect(warmBody).toHaveClass('max-h-[376px]', 'overflow-auto')

    hoisted.renderPrefix = true
    rerender(<FileChangeChips fileChanges={[...fileChanges]} />)
    const after = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    expect(document.activeElement).toBe(after)
    expect(row).not.toHaveAttribute('tabindex')
    expect(row).not.toHaveAttribute('role')
    expect(row).not.toHaveAttribute('aria-label')
    expect(row).not.toHaveAttribute('aria-expanded')
  })

  it('caps the cold fallback preview before Pierre resolves', () => {
    hoisted.renderPrefix = false
    const after = 'x'.repeat(20_000)
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/large.ts', before: '', after }]} />,
    )
    const toggle = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/large.ts"]')!
    toggle.focus()
    fireEvent.click(toggle)

    const body = container.querySelector<HTMLElement>('[data-fcc-warm-body]')!
    expect(body.textContent?.length).toBeLessThan(after.length)
    expect(body.textContent).toMatch(/…$/)
    expect(body).toHaveClass('max-h-[376px]', 'overflow-auto')
  })

  it.each(['Enter', ' '])('lets the temporary row proxy collapse with %j', key => {
    hoisted.renderPrefix = false
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const before = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!
    before.focus()
    fireEvent.click(before)

    fireEvent.keyDown(row, { key })
    expect(latest().unsafeCSS).toContain('fccHide')
    expect(row).toHaveAttribute('aria-expanded', 'false')
  })

  it('does not reclaim focus when the user leaves during a cold Pierre mount', () => {
    hoisted.renderPrefix = false
    const fileChanges = [{ path: '/a.ts', before: 'a', after: 'a\nb' }]
    const view = (changes = fileChanges) => (
      <>
        <button data-testid="outside-focus">Outside</button>
        <FileChangeChips fileChanges={changes} />
      </>
    )
    const { container, getByTestId, rerender } = render(view())
    const before = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!
    before.focus()
    fireEvent.click(before)

    const outside = getByTestId('outside-focus')
    outside.focus()
    expect(document.activeElement).toBe(outside)

    hoisted.renderPrefix = true
    rerender(view([...fileChanges]))
    expect(document.activeElement).toBe(outside)
    expect(row).not.toHaveAttribute('tabindex')
  })

  it('does not reclaim focus after a null-target blur during a cold Pierre mount', () => {
    hoisted.renderPrefix = false
    const fileChanges = [{ path: '/a.ts', before: 'a', after: 'a\nb' }]
    const { container, rerender } = render(<FileChangeChips fileChanges={fileChanges} />)
    const before = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!
    before.focus()
    fireEvent.click(before)
    fireEvent.blur(row, { relatedTarget: null })

    hoisted.renderPrefix = true
    rerender(<FileChangeChips fileChanges={[...fileChanges]} />)
    const after = container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!
    expect(document.activeElement).not.toBe(after)
    expect(row).not.toHaveAttribute('tabindex')
  })

  it('hands focus to the lightweight chevron after Pierre unmounts', async () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const chevron = () => container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!

    fireEvent.click(chevron())
    chevron().focus()
    fireEvent.click(chevron())
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!
    expect(document.activeElement).toBe(row)
    expect(row).toHaveAttribute('tabindex', '-1')

    await waitFor(() => expect(container.querySelector('[data-testid="pierre-pair"]')).toBeNull())
    expect(document.activeElement).toBe(chevron())
    expect(row).not.toHaveAttribute('tabindex')
  })

  it('does not reclaim focus when the user leaves during collapse', async () => {
    const { container, getByTestId } = render(
      <>
        <button data-testid="outside-focus">Outside</button>
        <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />
      </>,
    )
    const chevron = () => container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!

    fireEvent.click(chevron())
    chevron().focus()
    fireEvent.click(chevron())
    const outside = getByTestId('outside-focus')
    outside.focus()

    await waitFor(() => expect(container.querySelector('[data-testid="pierre-pair"]')).toBeNull())
    expect(document.activeElement).toBe(outside)
  })

  it('does not reclaim focus after a null-target blur during collapse', async () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/a.ts', before: 'a', after: 'a\nb' }]} />,
    )
    const chevron = () => container.querySelector<HTMLElement>('[data-testid="fcc-toggle-/a.ts"]')!

    fireEvent.click(chevron())
    chevron().focus()
    fireEvent.click(chevron())
    const row = container.querySelector<HTMLElement>('[data-testid="fcc-row-/a.ts"]')!
    fireEvent.blur(row, { relatedTarget: null })

    await waitFor(() => expect(container.querySelector('[data-testid="pierre-pair"]')).toBeNull())
    expect(document.activeElement).not.toBe(chevron())
  })
})
