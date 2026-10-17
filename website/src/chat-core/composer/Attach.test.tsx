import { createRef } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ComposerAttach } from './Attach'

const file = () => new File(['example'], 'example.pdf', { type: 'application/pdf' })

describe('ComposerAttach', () => {
  it('forwards the real input ref and keeps host hints and identity', () => {
    const ref = createRef<HTMLInputElement>()
    const onFiles = vi.fn()
    render(<ComposerAttach ref={ref} onFiles={onFiles} id="picker" aria-label="Files" accept=".pdf" multiple className="sr-only" />)
    expect(ref.current).toBe(screen.getByLabelText('Files'))
    expect(ref.current).toHaveAttribute('type', 'file')
    expect(ref.current).toHaveAttribute('accept', '.pdf')
    expect(ref.current).toHaveAttribute('multiple')
    expect(ref.current).toHaveAttribute('id', 'picker')
    expect(ref.current).toHaveClass('sr-only')
    fireEvent.change(ref.current!, { target: { files: [] } })
    expect(onFiles).not.toHaveBeenCalled()
    const picked = file()
    Object.defineProperty(ref.current, 'value', { configurable: true, writable: true, value: 'selected-file' })
    fireEvent.change(ref.current!, { target: { files: [picked] } })
    expect(onFiles).toHaveBeenCalledWith([picked])
    expect(ref.current!.value).toBe('')
    fireEvent.change(ref.current!, { target: { files: [picked] } })
    expect(onFiles).toHaveBeenCalledTimes(2)
  })
})

