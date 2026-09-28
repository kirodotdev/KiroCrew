import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import NotificationItem from '../pages/chat/NotificationItem'
import type { Notification } from '../types'

const baseNotif: Notification = {
  kind: 'cron',
  title: 'Test notification',
  body: 'This is the body text',
  ts: '2025-01-01T00:00:00Z',
}

describe('NotificationItem', () => {
  it('renders title and body preview', () => {
    render(<NotificationItem n={baseNotif} onDelete={() => {}} />)
    expect(screen.getByText('Test notification')).toBeInTheDocument()
    expect(screen.getByText('This is the body text')).toBeInTheDocument()
  })

  it('shows cron icon for cron kind', () => {
    const { container } = render(<NotificationItem n={baseNotif} onDelete={() => {}} />)
    expect(container.querySelector('.lucide-clock')).toBeInTheDocument()
  })

  it('shows approval icon for approval kind', () => {
    const { container } = render(<NotificationItem n={{ ...baseNotif, kind: 'approval' }} onDelete={() => {}} />)
    expect(container.querySelector('.lucide-lock')).toBeInTheDocument()
  })

  it('shows acknowledged icon when acked', () => {
    const { container } = render(<NotificationItem n={{ ...baseNotif, acked: true }} onDelete={() => {}} />)
    expect(container.querySelector('.lucide-circle-check-big')).toBeInTheDocument()
    expect(screen.getByText('Acknowledged')).toBeInTheDocument()
  })

  it('truncates long body text', () => {
    const longBody = 'x'.repeat(200)
    render(<NotificationItem n={{ ...baseNotif, body: longBody }} onDelete={() => {}} />)
    expect(screen.getByText(/…$/)).toBeInTheDocument()
  })

  it('calls onOpen when clicked', () => {
    const onOpen = vi.fn()
    render(<NotificationItem n={baseNotif} onOpen={onOpen} onDelete={() => {}} />)
    fireEvent.click(screen.getByTitle('Test notification'))
    expect(onOpen).toHaveBeenCalledOnce()
  })

  it('calls onDelete when delete button clicked', () => {
    const onDelete = vi.fn()
    render(<NotificationItem n={baseNotif} onDelete={onDelete} />)
    fireEvent.click(screen.getByLabelText('Delete'))
    expect(onDelete).toHaveBeenCalledWith(baseNotif.ts)
  })

  it('applies active styling', () => {
    const { container } = render(<NotificationItem n={baseNotif} active onDelete={() => {}} />)
    expect(container.firstChild).toHaveClass('border-accent')
  })
})
