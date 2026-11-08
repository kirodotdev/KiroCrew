// The finding card's dismiss and undo buttons keep keyboard focus while they
// save. Both used native `disabled` while the save ran, and a failed save keeps
// each button on screen, so the keyboard user who pressed it was left on <body>.
// Fixture and mocks follow `CodeReviewSageFindingCard.test.tsx`.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import type { Finding } from './lib/types'

vi.mock('../../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span data-testid="md">{content}</span>,
}))

const FindingCard = (await import('./components/FindingCard')).default

function finding(over: Partial<Finding> = {}): Finding {
  return {
    dimension: 'Correctness',
    severity: 'red',
    file: 'src/a.ts',
    line: 88,
    headline: 'Revoking the blob URL blanks every image already in scrollback.',
    observation: 'cleanup() revokes a shared blob URL.',
    consequence: 'Historic messages lose their images.',
    suggestion: 'Revoke on unmount instead.',
    ...over,
  }
}

/** A dismissal the test settles by hand, to hold the save in flight. */
function pendingDismiss() {
  let reject: (e: Error) => void = () => {}
  const onDismiss = vi.fn(() => new Promise<void>((_, r) => { reject = r }))
  return { onDismiss, fail: () => reject(new Error('offline')) }
}

describe('FindingCard dismiss buttons while saving', () => {
  it('keeps focus on "Dismiss finding" while it saves and after it fails', async () => {
    const { onDismiss, fail } = pendingDismiss()
    render(<FindingCard finding={finding()} onPost={() => {}} onDismiss={onDismiss} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    fireEvent.change(screen.getByLabelText('Why dismiss this finding?'), { target: { value: 'nope' } })
    const confirm = screen.getByRole('button', { name: 'Dismiss finding' })
    confirm.focus()
    fireEvent.click(confirm)

    expect(confirm).toHaveAttribute('aria-disabled', 'true')
    expect(confirm).not.toBeDisabled()
    expect(document.activeElement).toBe(confirm)
    // A second press while the first is saving sends nothing.
    fireEvent.click(confirm)
    expect(onDismiss).toHaveBeenCalledTimes(1)

    await act(async () => { fail() })
    expect(await screen.findByText('Could not save the dismissal. Try again.')).toBeTruthy()
    expect(document.activeElement).toBe(confirm)
    expect(confirm).not.toHaveAttribute('aria-disabled')
  })

  it('keeps focus on Undo while it saves and after it fails', async () => {
    const { onDismiss, fail } = pendingDismiss()
    render(<FindingCard finding={finding()} dismissed={{ reason: 'x' }} onDismiss={onDismiss} />)
    const undo = screen.getByRole('button', { name: 'Undo' })
    undo.focus()
    fireEvent.click(undo)

    expect(undo).toHaveAttribute('aria-disabled', 'true')
    expect(undo).not.toBeDisabled()
    expect(document.activeElement).toBe(undo)
    fireEvent.click(undo)
    expect(onDismiss).toHaveBeenCalledTimes(1)

    await act(async () => { fail() })
    expect(await screen.findByText('Could not undo. The finding is still dismissed.')).toBeTruthy()
    expect(document.activeElement).toBe(undo)
  })
})
