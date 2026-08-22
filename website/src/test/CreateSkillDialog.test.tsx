import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import CreateSkillDialog from '../pages/chat/CreateSkillDialog'

describe('CreateSkillDialog', () => {
  it('submits the trimmed purpose', () => {
    const onSubmit = vi.fn()
    render(<CreateSkillDialog open={true} onOpenChange={() => {}} onSubmit={onSubmit} />)
    fireEvent.change(screen.getByPlaceholderText('Skill purpose'), { target: { value: '  deploy runbook  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(onSubmit).toHaveBeenCalledWith('deploy runbook')
  })

  it('requires a non-empty purpose: Create disabled and submit is a no-op', () => {
    const onSubmit = vi.fn()
    render(<CreateSkillDialog open={true} onOpenChange={() => {}} onSubmit={onSubmit} />)
    const submit = screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.click(submit)
    expect(onSubmit).not.toHaveBeenCalled()
    const input = screen.getByPlaceholderText('Skill purpose')
    fireEvent.change(input, { target: { value: '   ' } })
    expect(submit.disabled).toBe(true)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submits on Enter once a non-empty purpose is typed', () => {
    const onSubmit = vi.fn()
    render(<CreateSkillDialog open={true} onOpenChange={() => {}} onSubmit={onSubmit} />)
    const input = screen.getByPlaceholderText('Skill purpose')
    fireEvent.change(input, { target: { value: 'a runbook' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSubmit).toHaveBeenCalledWith('a runbook')
  })

  it('opts the purpose input out of password-manager autofill decorations', () => {
    render(<CreateSkillDialog open={true} onOpenChange={() => {}} onSubmit={vi.fn()} />)
    const input = screen.getByPlaceholderText('Skill purpose') as HTMLInputElement
    expect(input.getAttribute('autocomplete')).toBe('off')
    expect(input.getAttribute('data-1p-ignore')).not.toBeNull()
    expect(input.getAttribute('data-lpignore')).toBe('true')
    expect(input.getAttribute('data-form-type')).toBe('other')
  })
  it('keeps the dialog open and preserves the purpose when submission fails', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error('at capacity'))
    const onOpenChange = vi.fn()
    render(<CreateSkillDialog open={true} onOpenChange={onOpenChange} onSubmit={onSubmit} />)
    const input = screen.getByPlaceholderText('Skill purpose') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'a runbook' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('a runbook'))
    // Failure path: the dialog is not closed and the draft is retained.
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
    expect(input.value).toBe('a runbook')
  })
})
