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

  it('marks the Escape that answers the question, so a window handler behind it can skip it', async () => {
    const seen: boolean[] = []
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') seen.push(e.defaultPrevented) }
    window.addEventListener('keydown', onKey)
    try {
      render(<Probe onAnswer={() => {}} />)
      await ask()
      await act(async () => { fireEvent.keyDown(document.activeElement ?? window, { key: 'Escape' }) })
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      fireEvent.keyDown(window, { key: 'Escape' })
    } finally {
      window.removeEventListener('keydown', onKey)
    }
    expect(seen, 'only the Escape that answered the question is marked').toEqual([true, false])
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

  it.each([
    ['Cancel', false, () => fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))],
    ['Escape', false, () => fireEvent.keyDown(window, { key: 'Escape' })],
    ['the backdrop', false, (dialog: HTMLElement) => {
      // The backdrop is the sibling before the dialog's positioning layer.
      const backdrop = dialog.parentElement?.previousElementSibling as HTMLElement
      expect(backdrop).toHaveAttribute('role', 'presentation')
      fireEvent.click(backdrop)
    }],
    ['the confirm button', true, () => fireEvent.click(screen.getByRole('button', { name: 'Delete' }))],
  ])('answers and returns focus to the opener on %s', async (_name, expected, close) => {
    const onAnswer = vi.fn()
    render(<Probe onAnswer={onAnswer} />)
    const opener = screen.getByRole('button', { name: 'ask' })
    opener.focus()
    const dialog = await ask()
    expect(dialog.contains(document.activeElement)).toBe(true)
    await act(async () => { close(dialog) })
    expect(onAnswer).toHaveBeenCalledWith(expected)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
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
})
