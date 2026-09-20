import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import { safeInternalUrl } from '../components/notifications/notifMeta'
import { approvalNotificationBody } from '../lib/approvalNotificationBody'
import type { RootState } from '../store'
import type { Notification } from '../types'

const mockResolveApproval = vi.fn().mockResolvedValue({})
const mockDeleteNotification = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    deleteNotification: (...args: unknown[]) => mockDeleteNotification(...args),
    resolveApproval: (...args: unknown[]) => mockResolveApproval(...args),
    updateNotificationChannelSettings: vi.fn().mockResolvedValue({}),
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const mkN = (over: Partial<Notification>): Notification => ({
  kind: 'cron', ts: '2026-07-24T10:00:00Z', title: 'Note', body: 'body', acked: false, ...over,
})

function renderFeed(notifs: Notification[], variant: 'panel' | 'mac' = 'panel') {
  const store = createTestStore({ notifications: { items: notifs } as RootState['notifications'] })
  return renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant={variant} />, { store })
}

beforeEach(() => {
  localStorage.clear()
  mockResolveApproval.mockClear()
  mockDeleteNotification.mockClear()
})

describe('safeInternalUrl (RFC Phase 4 security)', () => {
  it('accepts plain internal paths', () => {
    expect(safeInternalUrl('/settings?tab=notifications')).toBe('/settings?tab=notifications')
  })
  it('rejects external, protocol-relative, backslash, and control-char urls', () => {
    expect(safeInternalUrl('https://evil.example.com')).toBeNull()
    expect(safeInternalUrl('//evil.example.com')).toBeNull()
    expect(safeInternalUrl('/\\evil.example.com')).toBeNull()
    expect(safeInternalUrl('/\t/evil.example.com')).toBeNull()
    expect(safeInternalUrl(undefined)).toBeNull()
    expect(safeInternalUrl('relative/path')).toBeNull()
  })
})

describe('NotificationFeed Phase 4: inline approval actions', () => {
  const approval = mkN({
    ts: '1', kind: 'approval', title: 'Tool approval: shell',
    approval_id: 'apr-123', priority: 'critical',
  })

  it('renders Approve/Reject on unacked approval rows', () => {
    renderFeed([approval])
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeTruthy()
  })

  it('one-click approve resolves via the approvals endpoint', () => {
    renderFeed([approval])
    fireEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(mockResolveApproval).toHaveBeenCalledWith('apr-123', 'approve')
  })

  it('one-click reject resolves via the approvals endpoint', () => {
    renderFeed([approval])
    fireEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(mockResolveApproval).toHaveBeenCalledWith('apr-123', 'reject')
  })

  it('acked approval rows show no inline buttons', () => {
    renderFeed([mkN({ ...approval, acked: true })])
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
  })

  it('an actionable approval row shows every line of the command, not a clamped excerpt', () => {
    // A one-line excerpt would flatten this to `echo safe rm -rf target`, a
    // harmless-looking echo beside a one-click Approve.
    const body = approvalNotificationBody('agent', 'echo safe\nrm -rf target', 'clean up')
    renderFeed([mkN({ ...approval, body })])
    const rendered = screen.getByTestId('approval-body')
    const code = rendered.querySelector('pre, code')
    expect(code).not.toBeNull()
    expect(code!.textContent).toContain('echo safe\nrm -rf target')
    expect(rendered.textContent).not.toContain('echo safe rm -rf target')
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeTruthy()
  })

  it('an acked approval keeps the full command -- reading a pending request must not shrink it', () => {
    // Selecting a row acks it, but the approval is still undecided (the detail
    // panel still offers Approve/Reject), so it must not collapse back into
    // the joined one-liner this row exists to prevent.
    const body = approvalNotificationBody('agent', 'echo safe\nrm -rf target', 'clean up')
    renderFeed([mkN({ ...approval, body, acked: true })])
    const code = screen.getByTestId('approval-body').querySelector('pre, code')
    expect(code!.textContent).toContain('echo safe\nrm -rf target')
    // Raw matcher: the default normalizer folds the newline into a space and
    // would make the faithful render look like the joined line.
    expect(screen.queryByText(/echo safe rm -rf target/, { normalizer: s => s })).toBeNull()
  })

  it('every other kind keeps the one-line excerpt', () => {
    renderFeed([mkN({ ts: '2', kind: 'cron', title: 'Cron', body: 'line one\n\nline two' })])
    expect(screen.queryByTestId('approval-body')).toBeNull()
    expect(screen.getByText(/line one · line two/)).toBeTruthy()
  })

  it('a non-string persisted body renders an empty approval body instead of crashing the route', () => {
    renderFeed([mkN({ ...approval, body: { raw: 1 } as unknown as string })])
    expect(screen.getByTestId('approval-body').textContent).toBe('')
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
  })

  it('the command awaiting authorization carries no edit affordance', () => {
    // EditableCodeBlock's scratch editor edits a local copy that is never
    // written back; beside Approve that would let a reader authorize the
    // original command while looking at their edit.
    const body = approvalNotificationBody('agent', 'echo safe\nrm -rf target', 'clean up')
    renderFeed([mkN({ ...approval, body })])
    expect(screen.getByTestId('approval-body').querySelector('.code-block')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit code block' })).toBeNull()
  })
})

describe('NotificationFeed Phase 4: generic url actions', () => {
  it('renders actions with safe internal urls and navigates', () => {
    renderFeed([mkN({
      ts: '1', actions: [
        { id: 'view', label: 'View schedule', url: '/schedule' },
        { id: 'evil', label: 'Exfil', url: 'https://evil.example.com' },
      ],
    })])
    expect(screen.getByRole('button', { name: 'View schedule' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Exfil' })).toBeNull()
  })

  it('renders no action row when all urls are unsafe', () => {
    renderFeed([mkN({ ts: '1', actions: [{ id: 'x', label: 'Bad', url: '//evil.example.com' }] })])
    expect(screen.queryByRole('button', { name: 'Bad' })).toBeNull()
  })

  it('legacy/corrupted rows with non-string action fields render safely', () => {
    // A persisted action with object id/label rendered as a React child would
    // crash the whole feed. The row must render (title visible) with the
    // malformed action filtered out.
    renderFeed([mkN({
      ts: '1', title: 'Corrupted row survives',
      actions: [
        { id: {} as unknown as string, label: {} as unknown as string, url: '/schedule' },
        { id: 'ok', label: 'Fine', url: '/schedule' },
      ],
    })])
    expect(screen.getByText('Corrupted row survives')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Fine' })).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /Fine|schedule/i })).toHaveLength(1)
  })

  it('a truthy non-array actions field does not crash the feed', () => {
    // `n.actions || []` passes a truthy non-array (e.g. `{}`) straight to
    // .filter, throwing on every feed load.
    renderFeed([mkN({
      ts: '1', title: 'Non-array actions survives',
      actions: {} as unknown as Notification['actions'],
    })])
    expect(screen.getByText('Non-array actions survives')).toBeTruthy()
  })
})

describe('NotificationFeed Phase 4: group_key stacking', () => {
  // Store order is chronological (append); the feed reverses so the NEWEST
  // note (ts '3') is the visible stack head.
  const stack = [
    mkN({ ts: '1', title: 'Build 1 failed', group_key: 'ci-build' }),
    mkN({ ts: '2', title: 'Build 2 failed', group_key: 'ci-build' }),
    mkN({ ts: '3', title: 'Build 3 failed', group_key: 'ci-build' }),
  ]

  it('collapses same-group notes to the newest head with a count pill', () => {
    renderFeed(stack)
    expect(screen.getByText('Build 3 failed')).toBeTruthy()
    expect(screen.queryByText('Build 2 failed')).toBeNull()
    expect(screen.queryByText('Build 1 failed')).toBeNull()
    expect(screen.getByRole('button', { name: /2 more/ })).toBeTruthy()
  })

  it('expands and collapses the stack on pill click', () => {
    renderFeed(stack)
    fireEvent.click(screen.getByRole('button', { name: /2 more/ }))
    expect(screen.getByText('Build 2 failed')).toBeTruthy()
    expect(screen.getByText('Build 1 failed')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Show less/ }))
    expect(screen.queryByText('Build 2 failed')).toBeNull()
  })

  it('does not stack notes without group_key or singleton groups', () => {
    renderFeed([
      mkN({ ts: '2', title: 'Solo grouped', group_key: 'only-one' }),
      mkN({ ts: '1', title: 'Ungrouped' }),
    ])
    expect(screen.getByText('Solo grouped')).toBeTruthy()
    expect(screen.getByText('Ungrouped')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /more/ })).toBeNull()
  })

  it('notes with different group_keys stack independently', () => {
    renderFeed([
      mkN({ ts: '1', title: 'B1', group_key: 'b' }),
      mkN({ ts: '2', title: 'B2', group_key: 'b' }),
      mkN({ ts: '3', title: 'A1', group_key: 'a' }),
      mkN({ ts: '4', title: 'A2', group_key: 'a' }),
    ])
    expect(screen.getByText('A2')).toBeTruthy()
    expect(screen.getByText('B2')).toBeTruthy()
    expect(screen.queryByText('A1')).toBeNull()
    expect(screen.queryByText('B1')).toBeNull()
    expect(screen.getAllByRole('button', { name: /1 more/ })).toHaveLength(2)
  })
})

describe('NotificationFeed Phase 4: mac deck stacking (macOS NC style)', () => {
  const stack = [
    mkN({ ts: '1', title: 'Build 1 failed', group_key: 'ci-build' }),
    mkN({ ts: '2', title: 'Build 2 failed', group_key: 'ci-build' }),
    mkN({ ts: '3', title: 'Build 3 failed', group_key: 'ci-build' }),
  ]

  it('collapsed mac stack has no pill -- clicking the head expands it', () => {
    renderFeed(stack, 'mac')
    expect(screen.queryByRole('button', { name: /more/ })).toBeNull()
    const head = screen.getByRole('button', { name: /Expand 3 grouped notifications/ })
    fireEvent.click(head)
    expect(screen.getByText('Build 2 failed')).toBeTruthy()
    expect(screen.getByText('Build 1 failed')).toBeTruthy()
  })

  it('expanded mac stack collapses via the Show less capsule', () => {
    renderFeed(stack, 'mac')
    fireEvent.click(screen.getByRole('button', { name: /Expand 3 grouped notifications/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Show less' }))
    expect(screen.queryByText('Build 2 failed')).toBeNull()
    // Head goes back to expand-on-click semantics
    expect(screen.getByRole('button', { name: /Expand 3 grouped notifications/ })).toBeTruthy()
  })

  it('mac approval buttons are text-only quiet capsules (no solid fill)', () => {
    renderFeed([mkN({ ts: '1', kind: 'approval', title: 'Tool approval: shell', approval_id: 'apr-1' })], 'mac')
    const approve = screen.getByRole('button', { name: 'Approve' })
    expect(approve.className).toContain('text-ok')
    expect(approve.className).not.toContain('bg-ok')
  })

  it('a mac approval card shows every line of the command beside Approve/Reject, not the two-line clamp', () => {
    // The bell popover is a live surface that keeps one-click Approve/Reject.
    // The shared card's default excerpt is a 140-char two-line clamp, which
    // would hide the tail of `echo safe` + `rm -rf target` next to the
    // control that authorizes it.
    const body = approvalNotificationBody('agent', 'echo safe\nrm -rf target', 'clean up')
    renderFeed([mkN({ ts: '1', kind: 'approval', title: 'Tool approval: shell', approval_id: 'apr-1', body })], 'mac')
    const card = document.querySelector('[data-notification-card]')!
    const rendered = card.querySelector('[data-testid="approval-body"]')
    expect(rendered).not.toBeNull()
    const code = rendered!.querySelector('pre, code')
    expect(code!.textContent).toContain('echo safe\nrm -rf target')
    expect(card.querySelector('.line-clamp-2')).toBeNull()
    expect(screen.queryByText(/echo safe rm -rf target/, { normalizer: s => s })).toBeNull()
    expect(card.contains(screen.getByRole('button', { name: 'Approve' }))).toBe(true)
    expect(card.contains(screen.getByRole('button', { name: 'Reject' }))).toBe(true)
  })

  it('a mac approval card keeps the full command once read, and other kinds keep the clamp', () => {
    const body = approvalNotificationBody('agent', 'echo safe\nrm -rf target', 'clean up')
    renderFeed([
      mkN({ ts: '1', kind: 'approval', title: 'Tool approval: shell', approval_id: 'apr-1', body, acked: true }),
      mkN({ ts: '2', kind: 'cron', title: 'Cron', body: 'line one\n\nline two' }),
    ], 'mac')
    const cards = Array.from(document.querySelectorAll('[data-notification-card]'))
    expect(cards).toHaveLength(2)
    const approvalCard = cards.find(c => c.textContent!.includes('Tool approval'))!
    expect(approvalCard.querySelector('[data-testid="approval-body"] pre, [data-testid="approval-body"] code')!.textContent).toContain('echo safe\nrm -rf target')
    const cronCard = cards.find(c => c.textContent!.includes('Cron'))!
    expect(cronCard.querySelector('[data-testid="approval-body"]')).toBeNull()
    expect(cronCard.querySelector('.line-clamp-2')!.textContent).toBe('line one · line two')
  })
})
