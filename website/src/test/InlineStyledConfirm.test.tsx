import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useInlineStyledConfirm } from '../components/InlineStyledConfirm'

function Probe({ onAnswer }: { onAnswer: (ok: boolean) => void }) {
  const { confirm, confirmDialog } = useInlineStyledConfirm()
  return <>
    <button onClick={async () => onAnswer(await confirm({ title: 'Delete “Boba”?', confirmLabel: 'Delete' }))}>ask</button>
    {confirmDialog}
  </>
}

const ask = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'ask' }))
  return screen.findByRole('dialog', { name: 'Delete “Boba”?' })
}

describe('useInlineStyledConfirm', () => {
  it('positions itself with inline styles, since its windows may load no stylesheet', async () => {
    render(<Probe onAnswer={() => {}} />)
    const dialog = await ask()
    expect((dialog.parentElement as HTMLElement).style.position).toBe('fixed')
  })

  it('reports confirmOpen while a question is up', async () => {
    function OpenProbe() {
      const { confirm, confirmDialog, confirmOpen } = useInlineStyledConfirm()
      return <>
        <button onClick={() => { void confirm({ title: 'Q', confirmLabel: 'Delete' }) }}>ask</button>
        <span data-testid="open">{String(confirmOpen)}</span>
        {confirmDialog}
      </>
    }
    render(<OpenProbe />)
    expect(screen.getByTestId('open')).toHaveTextContent('false')
    fireEvent.click(screen.getByRole('button', { name: 'ask' }))
    await screen.findByRole('dialog')
    expect(screen.getByTestId('open')).toHaveTextContent('true')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })) })
    expect(screen.getByTestId('open')).toHaveTextContent('false')
  })

  it('gives the destructive button more weight than Cancel, from theme variables', async () => {
    render(<Probe onAnswer={() => {}} />)
    await ask()
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    const del = screen.getByRole('button', { name: 'Delete' })
    expect(cancel.style.background).toBe('transparent')
    expect(del.getAttribute('style')).toContain('var(--danger-subtle')
    expect(del.getAttribute('style')).toContain('var(--danger-text')
    expect(del.getAttribute('style')).toContain('var(--danger)')
  })

  it('answers true on the confirm button', async () => {
    const onAnswer = vi.fn()
    render(<Probe onAnswer={onAnswer} />)
    await ask()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete' })) })
    expect(onAnswer).toHaveBeenCalledWith(true)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([
    ['Cancel', () => fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))],
    ['Escape', () => fireEvent.keyDown(window, { key: 'Escape' })],
    ['the backdrop', () => fireEvent.click(document.querySelector('[aria-hidden="true"]') as HTMLElement)],
  ])('answers false on %s', async (_name, dismiss) => {
    const onAnswer = vi.fn()
    render(<Probe onAnswer={onAnswer} />)
    await ask()
    await act(async () => { dismiss() })
    expect(onAnswer).toHaveBeenCalledWith(false)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('answers false when its owner unmounts mid-question', async () => {
    const onAnswer = vi.fn()
    const { unmount } = render(<Probe onAnswer={onAnswer} />)
    await ask()
    unmount()
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('keeps Tab and Shift+Tab inside the dialog', async () => {
    render(<Probe onAnswer={() => {}} />)
    await ask()
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    const del = screen.getByRole('button', { name: 'Delete' })
    del.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(cancel).toHaveFocus()
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(del).toHaveFocus()
  })

  it('opens with focus on the dialog, so no focus ring sits on Cancel', async () => {
    render(<Probe onAnswer={() => {}} />)
    const dialog = await ask()
    expect(dialog).toHaveFocus()
    expect(dialog.style.outlineStyle).toBe('none')
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(screen.getByRole('button', { name: 'Delete' })).toHaveFocus()
  })

  it('answers false to a question a newer one replaces, and keeps the newer one', async () => {
    const answers: Array<[string, boolean]> = []
    function TwoProbe() {
      const { confirm, confirmDialog } = useInlineStyledConfirm()
      const ask = (title: string) => async () => { answers.push([title, await confirm({ title, confirmLabel: 'Delete' })]) }
      return <>
        <button onClick={ask('A')}>ask A</button>
        <button onClick={ask('B')}>ask B</button>
        {confirmDialog}
      </>
    }
    render(<TwoProbe />)
    fireEvent.click(screen.getByRole('button', { name: 'ask A' }))
    await screen.findByRole('dialog', { name: 'A' })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'ask B' })) })
    expect(answers).toEqual([['A', false]])
    expect(screen.getByRole('dialog', { name: 'B' })).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete' })) })
    expect(answers).toEqual([['A', false], ['B', true]])
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([
    ['Cancel', () => fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))],
    ['Escape', () => fireEvent.keyDown(window, { key: 'Escape' })],
    ['the confirm button', () => fireEvent.click(screen.getByRole('button', { name: 'Delete' }))],
  ])('returns focus to the opener after %s', async (_name, close) => {
    render(<Probe onAnswer={() => {}} />)
    const opener = screen.getByRole('button', { name: 'ask' })
    opener.focus()
    const dialog = await ask()
    expect(dialog.contains(document.activeElement)).toBe(true)
    await act(async () => { close() })
    expect(opener).toHaveFocus()
  })
})
