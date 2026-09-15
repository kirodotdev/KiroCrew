import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AddJobSplitButton from '../components/AddJobSplitButton'

/**
 * The ▾ half is a Radix `asChild` trigger, so its child MUST be able to hold a
 * ref. It once wrapped `SendBtn`, a plain function component: React drops the
 * ref, Radix's Popper anchor stays null, `isPositioned` never flips and the menu
 * renders at `translate(0, -200%)` — off screen. jsdom reports no layout, so a
 * `findByText` assertion passes while the menu is invisible in a real browser;
 * the observable signal that survives jsdom is React's own ref warning, which is
 * what this asserts.
 */
describe('AddJobSplitButton', () => {
  afterEach(() => vi.restoreAllMocks())

  it('gives the dropdown trigger a ref-able child (no React ref warning)', () => {
    const errors: string[] = []
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      errors.push(args.map(String).join(' '))
    })

    render(<AddJobSplitButton onBlank={() => {}} onBrowseTemplates={() => {}} />)

    const refWarnings = errors.filter(e => /cannot be given refs|Function components cannot/i.test(e))
    expect(refWarnings, refWarnings.join('\n')).toHaveLength(0)
  })

  it('keeps blank-create a one-click primary and the gallery behind the ▾', async () => {
    const onBlank = vi.fn()
    const onBrowseTemplates = vi.fn()
    render(<AddJobSplitButton onBlank={onBlank} onBrowseTemplates={onBrowseTemplates} />)

    fireEvent.click(screen.getByRole('button', { name: /Add Job/ }))
    expect(onBlank).toHaveBeenCalledTimes(1)
    expect(onBrowseTemplates).not.toHaveBeenCalled()

    // Radix needs keyboard-open in jsdom.
    fireEvent.keyDown(screen.getByLabelText('Browse schedule templates'), { key: 'Enter' })
    fireEvent.click(await screen.findByText('Browse all templates'))
    expect(onBrowseTemplates).toHaveBeenCalledTimes(1)
  })
})
