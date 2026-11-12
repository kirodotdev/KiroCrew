import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useInlineStyledConfirm, type InlineConfirmOptions } from '../components/InlineStyledConfirm'
import { registeredWithin } from '../uiLocations/targetRegistry'

function Probe({ onAnswer, options }: { onAnswer: (ok: boolean) => void; options?: Partial<InlineConfirmOptions> }) {
  const { confirm, confirmDialog } = useInlineStyledConfirm()
  return <>
    <button onClick={async () => onAnswer(await confirm({ ...options, title: 'Delete “Boba”?', confirmLabel: 'Delete' }))}>ask</button>
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
    const backdrop = dialog.parentElement?.previousElementSibling as HTMLElement
    expect(backdrop.style.position).toBe('fixed')
    expect(backdrop.style.inset).toBe('0')
    expect(dialog.style.boxShadow).toBe('var(--shadow-lg)')
  })

  it('uses the caller’s backdrop inset and radius', async () => {
    render(<Probe onAnswer={() => {}} options={{ backdropInset: 24, backdropRadius: 16 }} />)
    const dialog = await ask()
    const backdrop = dialog.parentElement?.previousElementSibling as HTMLElement
    expect(backdrop.style.inset).toBe('24px')
    expect(backdrop.style.borderRadius).toBe('16px')
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

  it('registers only the confirm action for guides and releases it on close', async () => {
    render(<Probe onAnswer={() => {}} />)
    const dialog = await ask()
    const del = screen.getByRole('button', { name: 'Delete' })
    expect(del).toHaveAttribute('data-guide-confirm', '')
    expect(registeredWithin('confirm', dialog)).toEqual([del])
    expect(screen.getByRole('button', { name: 'Cancel' })).not.toHaveAttribute('data-guide-confirm')
    await act(async () => { fireEvent.click(del) })
    expect(registeredWithin('confirm', dialog)).toEqual([])
  })

  it('leaves a composing Escape to the IME', async () => {
    const onAnswer = vi.fn()
    render(<Probe onAnswer={onAnswer} />)
    const dialog = await ask()
    const escape = new KeyboardEvent('keydown', { key: 'Escape', isComposing: true, bubbles: true, cancelable: true })
    fireEvent(dialog, escape)
    expect(escape.defaultPrevented).toBe(false)
    expect(dialog).toBeInTheDocument()
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('starts on the dialog and keeps Tab and Shift+Tab inside both actions', async () => {
    const user = userEvent.setup()
    render(<Probe onAnswer={() => {}} />)
    const dialog = await ask()
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    const del = screen.getByRole('button', { name: 'Delete' })
    expect(dialog).toHaveFocus()
    expect(dialog.style.outlineStyle).toBe('none')
    await user.tab()
    expect(cancel).toHaveFocus()
    await user.tab()
    expect(del).toHaveFocus()
    await user.tab()
    expect(cancel).toHaveFocus()
    await user.tab({ shift: true })
    expect(del).toHaveFocus()
    dialog.focus()
    await user.tab({ shift: true })
    expect(del).toHaveFocus()
  })

  it('answers false when its owner unmounts mid-question', async () => {
    const onAnswer = vi.fn()
    const { unmount } = render(<Probe onAnswer={onAnswer} />)
    await ask()
    unmount()
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
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
    const opener = screen.getByRole('button', { name: 'ask A' })
    opener.focus()
    fireEvent.click(opener)
    await screen.findByRole('dialog', { name: 'A' })
    const del = screen.getByRole('button', { name: 'Delete' })
    del.focus()
    expect(del).toHaveFocus()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'ask B' })) })
    expect(answers).toEqual([['A', false]])
    expect(screen.getByRole('dialog', { name: 'B' })).toHaveFocus()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete' })) })
    expect(answers).toEqual([['A', false], ['B', true]])
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
  })
})
