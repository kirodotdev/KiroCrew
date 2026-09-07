import { describe, it, expect, vi, beforeEach } from 'vitest'
import type React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import MoveToCrewDialog from './MoveToCrewDialog'
import { api } from '../api/client'

/**
 * Crew-to-crew work migration (issue #7577) — the shared dialog every move
 * surface uses. It asks for a target crew, calls the caller's plan function,
 * and renders what the plan says WOULD happen. It never claims a move happened,
 * because the transmit step does not exist yet.
 *
 * The dialog reads the configured peers to OFFER them as suggestions, so every
 * case needs a query client. It must stay usable when that list is empty or
 * forbidden (Instances feature off), which the last two cases pin down.
 */

vi.mock('../api/client', () => ({
  api: { listInstances: vi.fn() },
}))

const listInstances = vi.mocked(api.listInstances)

/** Instances present, in the real InstanceView shape (id + name). */
function withCrews() {
  listInstances.mockResolvedValue({
    active: true,
    instances: [
      { id: 'remote-ec2', name: 'EC2 box' },
      { id: 'lab', name: 'Lab host' },
    ],
    warm_set_cap: 2,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any)
}

function renderDialog(node: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>)
}

beforeEach(() => {
  listInstances.mockReset()
  // Default: the Instances feature is off, which is how a single-crew install
  // answers. Cases that need suggestions call withCrews() explicitly.
  listInstances.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
})

const plan = {
  handoff_id: 'abc123def456',
  bundle_kind: 'cron',
  bundle_version: 1,
  target_crew: 'remote-ec2',
  ships: 25,
  requirements: [
    { kind: 'agent', identity: 'kirocrew', severity: 'blocking' },
    { kind: 'script_path', identity: '~/.kiro/crew/crons/x.py:go', severity: 'blocking' },
  ],
  findings: [] as Array<{ kind: string; detail: string; severity: string; detail_key: string }>,
}

describe('MoveToCrewDialog', () => {
  it('keeps a blank target out of the failure surface', async () => {
    // A blank field is the user's own in-progress input, not something that
    // failed. Rendering it through ErrorNotice made a normal state look broken,
    // and — because the two shared one slot — left the peer lookup's own error
    // with nowhere to go.
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    expect(screen.getByTestId('move-to-crew-hint')).toBeInTheDocument()
    expect(screen.queryByTestId('move-to-crew-error')).not.toBeInTheDocument()
  })

  it('reports a non-403 crew lookup failure instead of showing an empty list', async () => {
    // 403 means the Instances feature is off and stays silent. Any other failure
    // used to vanish, so "we could not ask" was indistinguishable from "this
    // install has no peers".
    listInstances.mockRejectedValue(Object.assign(new Error('HTTP 500'), { status: 500 }))
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    expect(await screen.findByTestId('move-to-crew-error')).toBeInTheDocument()
  })

  it('lays the plan out narrow-first so a handoff id cannot clip', async () => {
    // The required 320px viewport leaves ~216px of body. A label column plus an
    // unbreakable id exceeded it, so the value was cut off rather than wrapped.
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    await userEvent.type(screen.getByLabelText(/target crew/i), 'remote-ec2')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    const list = (await screen.findByText('abc123def456')).closest('dl')
    expect(list).not.toBeNull()
    // one column by default, two only from the breakpoint up
    expect(list!.className).toContain('grid-cols-1')
    expect(list!.className).toMatch(/sm:grid-cols-\[max-content_1fr\]/)
  })

  it('does not call the planner until a target crew is given', async () => {
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    expect(onPlan).not.toHaveBeenCalled()
    expect(screen.getByText(/target crew is required/i)).toBeInTheDocument()
  })

  it('renders the plan: handoff, field count and blocking requirements', async () => {
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    await userEvent.type(screen.getByLabelText(/target crew/i), 'remote-ec2')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    await waitFor(() => expect(onPlan).toHaveBeenCalledWith('remote-ec2'))
    expect(await screen.findByText(/abc123def456/)).toBeInTheDocument()
    expect(screen.getByText(/25/)).toBeInTheDocument()
    expect(screen.getByText(/agent/)).toBeInTheDocument()
    expect(screen.getByText(/kirocrew/)).toBeInTheDocument()
  })

  it('shows advisory findings so a degraded move is never silent', async () => {
    const onPlan = vi.fn().mockResolvedValue({
      ok: true,
      plan: {
        ...plan,
        bundle_kind: 'session',
        findings: [
          { kind: 'project_checkout', detail: 'project dropped', severity: 'advisory', detail_key: 'project' },
          { kind: 'session_context', detail: 'Layer B unavailable', severity: 'advisory', detail_key: 'layer_b' },
        ],
      },
    })
    renderDialog(<MoveToCrewDialog unitKind="session" unitId="chat-3" onPlan={onPlan} onClose={() => {}} />)
    await userEvent.type(screen.getByLabelText(/target crew/i), 'dst')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    expect(await screen.findByText(/Layer B unavailable/)).toBeInTheDocument()
    expect(screen.getByText(/project dropped/)).toBeInTheDocument()
  })

  it('leads the plan with an unmuted statement that nothing has moved', async () => {
    // Position and weight are the point, not mere presence: the rows below read
    // like a completed-transfer receipt, so the correction has to be the first
    // thing in the panel and must not be the smallest text on it.
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    await userEvent.type(screen.getByLabelText(/target crew/i), 'dst')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    const note = await screen.findByTestId('move-to-crew-plan-only')
    expect(note).toHaveTextContent(/nothing has moved/i)
    expect(note.className).not.toContain('text-muted')
    expect(note.className).not.toContain('text-[12px]')
    // It precedes the receipt-shaped rows rather than trailing them.
    const rows = note.parentElement!.querySelector('dl')!
    expect(note.compareDocumentPosition(rows) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('surfaces a planner error instead of a blank dialog', async () => {
    const onPlan = vi.fn().mockRejectedValue(new Error('cron job not found: j9'))
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j9" onPlan={onPlan} onClose={() => {}} />)
    await userEvent.type(screen.getByLabelText(/target crew/i), 'dst')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    expect(await screen.findByText(/not found: j9/)).toBeInTheDocument()
  })

  it('closes on cancel without planning', async () => {
    const onClose = vi.fn()
    const onPlan = vi.fn()
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={onClose} />)
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onClose).toHaveBeenCalled()
    expect(onPlan).not.toHaveBeenCalled()
  })

  it('names the outcome, so no label promises a move the code cannot perform', () => {
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={vi.fn()} onClose={() => {}} />)
    // The heading is read BEFORE acting, so it must name a preview rather than a
    // move. The submit control has to agree with it.
    expect(screen.getByRole('heading')).toHaveTextContent(/preview moving/i)
    expect(screen.getByRole('heading')).not.toHaveTextContent(/^Move /)
    expect(screen.getByRole('button', { name: /preview move/i })).toBeInTheDocument()
  })

  it('offers the configured crews as suggestions while still accepting free text', async () => {
    withCrews()
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    const field = screen.getByLabelText(/target crew/i)
    await waitFor(() => expect(field).toHaveAttribute('list', 'move-to-crew-crews'))

    // The suggestion VALUE must be the id, because that is what reaches to_crew.
    const options = Array.from(
      document.querySelectorAll<HTMLOptionElement>('#move-to-crew-crews option'),
    )
    expect(options.map(o => o.value)).toEqual(['remote-ec2', 'lab'])

    // Free text unrelated to any suggestion still plans — the picker must not
    // have narrowed what the endpoint accepts.
    await userEvent.type(field, 'not-a-configured-crew')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    await waitFor(() => expect(onPlan).toHaveBeenCalledWith('not-a-configured-crew'))
  })

  it('stays usable when no crews are configured instead of offering an empty picker', async () => {
    const onPlan = vi.fn().mockResolvedValue({ ok: true, plan })
    renderDialog(<MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />)
    const field = screen.getByLabelText(/target crew/i)
    expect(field).not.toHaveAttribute('list')
    expect(document.querySelector('#move-to-crew-crews')).toBeNull()
    await userEvent.type(field, 'typed-by-hand')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))
    await waitFor(() => expect(onPlan).toHaveBeenCalledWith('typed-by-hand'))
  })
})


  it('uses the shared modal primitive and closes on Escape', async () => {
    const onClose = vi.fn()
    renderDialog(
      <MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={vi.fn()} onClose={onClose} />,
    )

    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('data-state', 'open')
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('uses the shared error notice for planner failures', async () => {
    const onPlan = vi.fn().mockRejectedValue(new Error('planner unavailable'))
    renderDialog(
      <MoveToCrewDialog unitKind="cron" unitId="j1" onPlan={onPlan} onClose={() => {}} />,
    )
    await userEvent.type(screen.getByLabelText(/target crew/i), 'dst')
    await userEvent.click(screen.getByRole('button', { name: /preview move/i }))

    const notice = await screen.findByTestId('move-to-crew-error')
    expect(notice).toHaveTextContent('planner unavailable')
    expect(notice.querySelector('svg')).not.toBeNull()
  })
