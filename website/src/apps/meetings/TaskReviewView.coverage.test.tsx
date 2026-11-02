/** TaskReviewView: keeps the meeting open until every item is decided; files, archives and restores through the callbacks; links only absolute http(s) refs. */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Task } from './api'
import TaskReviewView from './TaskReviewView'

function task(overrides: Partial<Task> & { id: string; description: string }): Task {
  return {
    assignee: null,
    priority: 'normal',
    review_status: 'pending',
    filed_ref: null,
    ...overrides,
  } as unknown as Task
}

let handlers: Record<"onBack"|"onClose"|"onFile"|"onArchive"|"onUnarchive", ReturnType<typeof vi.fn>>

function renderView(tasks: Task[], filing: string | null = null) {
  render(
    <TaskReviewView
      tasks={tasks}
      transcript={[]}
      partialTranscript=""
      transcriptFull={false}
      provider="Jira"
      filing={filing}
      onBack={handlers.onBack}
      onClose={handlers.onClose}
      onFile={handlers.onFile}
      onArchive={handlers.onArchive}
      onUnarchive={handlers.onUnarchive}
    />,
  )
}

beforeEach(() => {
  handlers = {
    onBack: vi.fn(),
    onClose: vi.fn(),
    onFile: vi.fn(),
    onArchive: vi.fn(),
    onUnarchive: vi.fn(),
  }
})

describe('TaskReviewView', () => {
  it('keeps the meeting open and counts the pending items while any are undecided', () => {
    renderView([
      task({ id: 't1', description: 'Send the rollout notes' }),
      task({ id: 't2', description: 'Book the review room' }),
    ])
    expect(screen.getByText('2 left to review')).toBeInTheDocument()
    const closeButton = screen.getByRole('button', { name: 'Close the meeting' })
    expect(closeButton).toBeDisabled()
    fireEvent.click(closeButton)
    expect(handlers.onClose).not.toHaveBeenCalled()
  })

  it('lets the meeting close once nothing is pending', () => {
    renderView([task({ id: 't1', description: 'Done already', review_status: 'pushed' })])
    expect(screen.getByText('All reviewed')).toBeInTheDocument()
    const closeButton = screen.getByRole('button', { name: 'Close the meeting' })
    expect(closeButton).toBeEnabled()
    fireEvent.click(closeButton)
    expect(handlers.onClose).toHaveBeenCalledTimes(1)
  })

  it('sends each pending item to archive from its row, and archives all of them at once', () => {
    renderView([
      task({ id: 't1', description: 'First item' }),
      task({ id: 't2', description: 'Second item' }),
    ])
    const rows = screen.getByText('First item').closest('div.flex') as HTMLElement
    fireEvent.click(within(rows).getByRole('button', { name: 'Archive' }))
    expect(handlers.onArchive).toHaveBeenCalledWith('t1')

    fireEvent.click(screen.getByRole('button', { name: 'Archive the rest' }))
    expect(handlers.onArchive).toHaveBeenCalledWith('t2')
    expect(handlers.onArchive).toHaveBeenCalledTimes(3)
  })

  it('files a pending item through the provider callback', () => {
    renderView([task({ id: 't1', description: 'File this one' })])
    fireEvent.click(screen.getByRole('button', { name: 'File' }))
    expect(handlers.onFile).toHaveBeenCalledWith('t1')
  })

  it('disables the row actions and shows Filing while that item is being filed', () => {
    renderView([task({ id: 't1', description: 'In flight' })], 't1')
    expect(screen.getByText('Filing…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'File' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Archive' })).toBeDisabled()
  })

  it('shows the assignee and marks a high priority item in the error tone', () => {
    renderView([task({ id: 't1', description: 'Urgent', assignee: 'Dana', priority: 'high' })])
    expect(screen.getByText('Assigned to Dana')).toBeInTheDocument()
    expect(screen.getByText('High')).toHaveClass('bg-danger-subtle')
  })

  it('links a filed item only when its URL is absolute http(s)', () => {
    renderView([
      task({
        id: 't1',
        description: 'Filed with a link',
        review_status: 'pushed',
        filed_ref: { id: 'JIRA-7', url: 'https://tracker.example.com/JIRA-7' } as Task['filed_ref'],
      }),
    ])
    const link = screen.getByRole('link', { name: 'JIRA-7' })
    expect(link).toHaveAttribute('href', 'https://tracker.example.com/JIRA-7')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('never renders a javascript: URL as a link and shows the plain id instead', () => {
    renderView([
      task({
        id: 't1',
        description: 'Filed with a hostile link',
        review_status: 'pushed',
        filed_ref: { id: 'REF-9', url: 'javascript:alert(1)' } as Task['filed_ref'],
      }),
    ])
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText('REF-9')).toBeInTheDocument()
  })

  it('shows the filed section under the provider name, with no link when there is no ref', () => {
    renderView([task({ id: 't1', description: 'Filed without ref', review_status: 'pushed' })])
    expect(screen.getByText('Filed to Jira')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('lists archived items struck through, and brings one back on Unarchive', () => {
    renderView([task({ id: 't9', description: 'Set aside', review_status: 'archived' })])
    expect(screen.getByText('Set aside')).toHaveClass('line-through')
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }))
    expect(handlers.onUnarchive).toHaveBeenCalledWith('t9')
  })

  it('returns to the meeting from the back button', () => {
    renderView([])
    fireEvent.click(screen.getByRole('button', { name: 'Back to the meeting' }))
    expect(handlers.onBack).toHaveBeenCalledTimes(1)
  })
})
