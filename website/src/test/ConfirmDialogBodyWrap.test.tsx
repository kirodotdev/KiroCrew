import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useConfirm } from '../components/ConfirmDialog'
import { Btn } from '../components/ui'

const longName = 'UnbrokenSkillNameForConfirmDialogOverflowVerification'.repeat(5)
const longBody = `Delete skill "${longName}"?`

function ConfirmOwner({ body }: { body: ReactNode }) {
  const { confirm, confirmDialog } = useConfirm()
  return (
    <>
      <Btn onClick={() => { void confirm({ title: 'Delete skill', body, confirmLabel: 'Delete' }) }}>
        Ask
      </Btn>
      {confirmDialog}
    </>
  )
}

describe('confirmation body wrapping', () => {
  it('wraps an unbroken name in the string body paragraph', async () => {
    const user = userEvent.setup()
    render(<ConfirmOwner body={longBody} />)
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    const dialog = await screen.findByRole('dialog')
    const paragraph = within(dialog).getByText(longBody)
    expect(paragraph.tagName).toBe('P')
    expect(paragraph).toHaveClass('break-words')
  })

  it('lets block body content inherit wrapping without changing list markup', async () => {
    const user = userEvent.setup()
    render(<ConfirmOwner body={<><p>Skills to delete:</p><ul><li>{longName}</li></ul></>} />)
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    const dialog = await screen.findByRole('dialog')
    const list = within(dialog).getByRole('list')
    expect(list.parentElement?.tagName).toBe('DIV')
    expect(list.parentElement).toHaveClass('break-words')
    expect(within(list).getByRole('listitem')).toHaveTextContent(longName)
  })
})
