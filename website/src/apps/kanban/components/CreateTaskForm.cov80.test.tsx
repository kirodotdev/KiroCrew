/**
 * The create-task form's submit guard.
 *
 * The guard is the whole point of this component: a blank or whitespace-only
 * prompt must not create a task (the backend refuses it with `title_required`,
 * so submitting anyway costs a round trip and shows the user an error for
 * something the form could have prevented), and a submit while a refine is
 * already in flight would fire a second create for the same intent.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { i18nT } from '../../../i18n/t'
import { CreateTaskForm } from './CreateTaskForm'

describe('CreateTaskForm', () => {
  it('refuses to submit an empty prompt', async () => {
    const onSubmit = vi.fn()
    render(<CreateTaskForm onSubmit={onSubmit} onCancel={() => {}} />)
    const submit = screen.getAllByRole('button').find((b) => b.getAttribute('type') === 'submit')!
    expect(submit).toBeDisabled()
    await userEvent.click(submit)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('refuses a whitespace-only prompt', async () => {
    const onSubmit = vi.fn()
    render(<CreateTaskForm onSubmit={onSubmit} onCancel={() => {}} />)
    await userEvent.type(screen.getByRole('textbox'), '   ')
    const submit = screen.getAllByRole('button').find((b) => b.getAttribute('type') === 'submit')!
    expect(submit).toBeDisabled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submits the trimmed prompt', async () => {
    const onSubmit = vi.fn()
    render(<CreateTaskForm onSubmit={onSubmit} onCancel={() => {}} />)
    await userEvent.type(screen.getByRole('textbox'), '  add a health check  ')
    const submit = screen.getAllByRole('button').find((b) => b.getAttribute('type') === 'submit')!
    await userEvent.click(submit)
    expect(onSubmit).toHaveBeenCalledWith('add a health check')
  })

  it('submits on the first click — there is no in-flight lock to sit through', async () => {
    const onSubmit = vi.fn()
    const onCancel = vi.fn()
    render(<CreateTaskForm onSubmit={onSubmit} onCancel={onCancel} />)
    const box = screen.getByRole('textbox')
    expect(box).not.toBeDisabled()
    await userEvent.type(box, 'do the thing')
    const submit = screen.getAllByRole('button').find((b) => b.getAttribute('type') === 'submit')!
    await userEvent.click(submit)
    // Creation is instant now: the form hands the raw prompt straight up and the
    // parent closes it, so nothing here waits on a model.
    expect(onSubmit).toHaveBeenCalledWith('do the thing')
  })

  it('cancel reaches the caller', async () => {
    const onCancel = vi.fn()
    render(<CreateTaskForm onSubmit={() => {}} onCancel={onCancel} />)
    // The two non-submit buttons (the X and Cancel) both cancel.
    const buttons = screen.getAllByRole('button').filter((b) => b.getAttribute('type') === 'button')
    await userEvent.click(buttons[0])
    expect(onCancel).toHaveBeenCalled()
  })
})

describe('a typed prompt is not discarded by one keypress', () => {
  const type = async (text: string) => {
    const box = await screen.findByPlaceholderText(/Describe your task/i)
    await userEvent.type(box, text)
    return box
  }

  it('Escape asks before throwing the prompt away', async () => {
    // The textarea invites a paragraph and the prompt exists nowhere else, so an
    // accidental Escape would destroy the only copy. A failed create hands the
    // prompt back; a cancel has nothing to hand it back from.
    const onCancel = vi.fn()
    render(<CreateTaskForm onSubmit={vi.fn()} onCancel={onCancel} />)
    await type('rewrite the cache layer, then benchmark it')

    await userEvent.keyboard('{Escape}')

    expect(onCancel).not.toHaveBeenCalled()
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('Keep editing returns to the form with the text intact', async () => {
    const onCancel = vi.fn()
    render(<CreateTaskForm onSubmit={vi.fn()} onCancel={onCancel} />)
    await type('a long prompt')
    await userEvent.keyboard('{Escape}')

    await userEvent.click(screen.getByText(i18nT('apps.kanban.createTaskForm.keep_editing')))

    expect(onCancel).not.toHaveBeenCalled()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByDisplayValue('a long prompt')).toBeInTheDocument()
  })

  it('Discard is the one path that closes', async () => {
    const onCancel = vi.fn()
    render(<CreateTaskForm onSubmit={vi.fn()} onCancel={onCancel} />)
    await type('a long prompt')
    await userEvent.keyboard('{Escape}')

    await userEvent.click(screen.getByText(i18nT('apps.kanban.createTaskForm.discard')))

    expect(onCancel).toHaveBeenCalled()
  })

  it('an empty form still closes without a prompt', async () => {
    // The guard is scoped to a typed prompt; an untouched form has nothing to lose.
    const onCancel = vi.fn()
    render(<CreateTaskForm onSubmit={vi.fn()} onCancel={onCancel} />)
    await userEvent.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalled()
  })
})
