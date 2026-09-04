/**
 * The shared row-disclosure mechanism.
 *
 * Every expand/collapse control inside the virtualised transcript goes through
 * useRowDisclosure, so proving durability here covers all of them rather than
 * one component at a time. ThinkingBlock stands in as a real consumer.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { RowDisclosureProvider, useRowDisclosure } from '../pages/chat/rowDisclosure'
import ThinkingBlock from '../pages/chat/ThinkingBlock'

/** Minimal consumer, so the mechanism is testable without any one component. */
function Probe({ id }: { id?: string }) {
  const [open, setOpen] = useRowDisclosure(id, false)
  return <button onClick={() => setOpen(v => !v)} aria-expanded={open}>{id ?? 'nokey'}</button>
}

/** `mounted` mirrors the virtualizer: false means the row is not rendered. */
function Host({ children }: { children: (mounted: boolean) => React.ReactNode }) {
  const [mounted, setMounted] = useState(true)
  const [slot, setSlot] = useState('slot-a')
  return (
    <RowDisclosureProvider resetKey={slot}>
      <button data-testid="recycle" onClick={() => setMounted(m => !m)}>recycle</button>
      <button data-testid="switch-slot" onClick={() => setSlot('slot-b')}>switch</button>
      {children(mounted)}
    </RowDisclosureProvider>
  )
}

const expandedOf = (name: string) => screen.getByRole('button', { name }).getAttribute('aria-expanded')
const recycle = () => fireEvent.click(screen.getByTestId('recycle'))

describe('useRowDisclosure', () => {
  it('keeps a choice across an unmount and remount', () => {
    render(<Host>{m => (m ? <Probe id="row-1" /> : null)}</Host>)
    fireEvent.click(screen.getByRole('button', { name: 'row-1' }))
    expect(expandedOf('row-1')).toBe('true')

    recycle()
    expect(screen.queryByRole('button', { name: 'row-1' })).toBeNull()
    recycle()

    expect(expandedOf('row-1')).toBe('true')
  })

  it('keeps an explicit collapse across an unmount too', () => {
    render(<Host>{m => (m ? <Probe id="row-1" /> : null)}</Host>)
    fireEvent.click(screen.getByRole('button', { name: 'row-1' }))
    fireEvent.click(screen.getByRole('button', { name: 'row-1' }))
    expect(expandedOf('row-1')).toBe('false')
    recycle(); recycle()
    expect(expandedOf('row-1')).toBe('false')
  })

  it('does not leak a choice between different keys', () => {
    render(<Host>{m => (m ? <><Probe id="row-1" /><Probe id="row-2" /></> : null)}</Host>)
    fireEvent.click(screen.getByRole('button', { name: 'row-1' }))
    expect(expandedOf('row-1')).toBe('true')
    expect(expandedOf('row-2')).toBe('false')
    recycle(); recycle()
    expect(expandedOf('row-1')).toBe('true')
    expect(expandedOf('row-2')).toBe('false')
  })

  it('drops choices on a slot switch, since keys are only unique per slot', () => {
    render(<Host>{m => (m ? <Probe id="row-1" /> : null)}</Host>)
    fireEvent.click(screen.getByRole('button', { name: 'row-1' }))
    expect(expandedOf('row-1')).toBe('true')
    fireEvent.click(screen.getByTestId('switch-slot'))
    expect(expandedOf('row-1')).toBe('false')
  })

  it('falls back to local state with no key, so unprovided hosts still work', () => {
    render(<Probe />)   // no provider at all
    expect(expandedOf('nokey')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'nokey' }))
    expect(expandedOf('nokey')).toBe('true')
  })
})

describe('ThinkingBlock disclosure survives virtualizer recycling', () => {
  const block = () => screen.getAllByRole('button').find(b => b.hasAttribute('aria-expanded'))!

  it('keeps the reasoning open when the row is recycled', () => {
    render(<Host>{m => (m ? <ThinkingBlock content="some reasoning" disclosureKey="think-1" /> : null)}</Host>)
    expect(block().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(block())
    expect(block().getAttribute('aria-expanded')).toBe('true')

    recycle()
    expect(screen.getAllByRole('button').some(b => b.hasAttribute('aria-expanded'))).toBe(false)
    recycle()

    expect(block().getAttribute('aria-expanded')).toBe('true')
  })

  it('stays collapsed by default when never opened', () => {
    render(<Host>{m => (m ? <ThinkingBlock content="some reasoning" disclosureKey="think-1" /> : null)}</Host>)
    recycle(); recycle()
    expect(block().getAttribute('aria-expanded')).toBe('false')
  })
})
