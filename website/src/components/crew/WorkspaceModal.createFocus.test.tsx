// Focus on the Create workspace dialog's Create button while its request is out.
//
// A native `disabled` on the pressed button blurs it to <body>, outside the
// dialog, so a keyboard user lost their place until the create answered, and a
// failure notice rendered in a dialog that no longer held focus.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const createWorkspace = vi.fn()

vi.mock('../../api/client', () => ({
  api: { createWorkspace: (...args: unknown[]) => createWorkspace(...args) },
}))

import { WorkspaceModal } from './crewFields'

describe('WorkspaceModal Create focus', () => {
  it('keeps focus on Create while the request is out and after it fails, and sends one request', async () => {
    let reject: (err: unknown) => void = () => {}
    createWorkspace.mockImplementation(() => new Promise((_resolve, rej) => { reject = rej }))
    const user = userEvent.setup()
    render(<WorkspaceModal open workspaceOptions={[]} onCreated={vi.fn()} onClose={vi.fn()} />)

    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'staging')
    const create = screen.getByRole('button', { name: 'Create' })
    create.focus()
    await user.keyboard('{Enter}')
    expect(createWorkspace).toHaveBeenCalledTimes(1)
    expect(create.getAttribute('aria-disabled')).toBe('true')
    expect(create.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(create)

    // Inert while pending: SendBtn's `busy` swallows the press.
    await user.keyboard('{Enter}')
    expect(createWorkspace).toHaveBeenCalledTimes(1)

    reject(new Error('Workspace already exists'))
    expect(await screen.findByText('Workspace already exists')).toBeTruthy()
    expect(document.activeElement).toBe(create)
    expect(create.hasAttribute('aria-disabled')).toBe(false)
  })
})
