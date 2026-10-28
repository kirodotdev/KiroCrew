import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../../../api/client'
import { createTestStore, renderWithProviders } from '../../../test/helpers'
import { KasRunCard } from './KasRunView'
import { kasApproval, kasControls, kasRow } from '../../../test/kasFixtures'

const navigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

// A plain action stands in for the switch thunk so the test can read which chat
// was asked for without driving the slot-detail fetch the real thunk makes.
vi.mock('../../../store/chatSlice', async importOriginal => ({
  ...(await importOriginal<typeof import('../../../store/chatSlice')>()),
  switchSlot: (arg: unknown) => ({ type: 'test/switchSlot', payload: arg }),
}))

let control: ReturnType<typeof vi.spyOn>
let answer: ReturnType<typeof vi.spyOn>
let reconnect: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  navigate.mockReset()
  control = vi.spyOn(api, 'kasRunControl').mockResolvedValue({ workflowId: 'wf-1', row: null })
  answer = vi.spyOn(api, 'kasAnswerApproval').mockResolvedValue({ answered: true })
  reconnect = vi.spyOn(api, 'kasReconnectSlot').mockResolvedValue({})
})

afterEach(() => {
  vi.restoreAllMocks()
})

const button = (name: RegExp) => screen.getByRole('button', { name })
const noButton = (name: RegExp) => expect(screen.queryByRole('button', { name })).toBeNull()

describe('KasRunCard — controls', () => {
  it('a running run offers Pause and Stop, no Delete, and lists its steps in the shared tree', () => {
    renderWithProviders(<KasRunCard row={kasRow()} approvals={[]} />)
    expect(button(/^pause$/i)).toBeEnabled()
    expect(button(/^stop$/i)).toBeEnabled()
    noButton(/^resume$/i)
    noButton(/^delete$/i)
    expect(screen.getByTestId('kas-run-progress')).toHaveTextContent('Step 1/3')
    // The shared, openable step tree (gap 23) replaces the old inert list.
    expect(screen.getByTestId('kas-steps-tree')).toBeInTheDocument()
    const steps = screen.getAllByTestId('kas-tree-step').map(el => el.textContent)
    expect(steps.some(t => t?.includes('Review'))).toBe(true)
  })

  it('Pause sends pause for this run', async () => {
    renderWithProviders(<KasRunCard row={kasRow()} approvals={[]} />)
    fireEvent.click(button(/^pause$/i))
    await waitFor(() => expect(control).toHaveBeenCalledWith('wf-1', 'pause'))
  })

  it('a pausing run shows a disabled Pausing… button and no Pause', () => {
    renderWithProviders(
      <KasRunCard row={kasRow({ pausePending: true, controls: kasControls({ display: 'pausing', primary: null }) })} approvals={[]} />,
    )
    expect(button(/pausing/i)).toBeDisabled()
    noButton(/^pause$/i)
  })

  it('a paused run offers Resume', async () => {
    renderWithProviders(
      <KasRunCard row={kasRow({ status: 'paused', controls: kasControls({ display: 'paused', primary: 'resume' }) })} approvals={[]} />,
    )
    fireEvent.click(button(/^resume$/i))
    await waitFor(() => expect(control).toHaveBeenCalledWith('wf-1', 'resume'))
  })

  it('a terminal run offers only Delete (behind a confirm) and Open chat', async () => {
    renderWithProviders(
      <KasRunCard
        row={kasRow({ status: 'aborted', completedSteps: null, totalSteps: null, controls: kasControls({ display: 'stopped', primary: null, canStop: false, canDelete: true }) })}
        approvals={[]}
      />,
    )
    noButton(/^pause$/i)
    noButton(/^resume$/i)
    noButton(/^stop$/i)
    expect(screen.getByTestId('kas-run-state')).toHaveTextContent('Stopped')
    fireEvent.click(button(/^delete$/i))
    expect(control).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: /delete run/i }))
    await waitFor(() => expect(control).toHaveBeenCalledWith('wf-1', 'delete'))
  })
})

describe('KasRunCard — Stop is always confirmed', () => {
  it('cancelling the confirm sends nothing', async () => {
    renderWithProviders(<KasRunCard row={kasRow()} approvals={[]} />)
    fireEvent.click(button(/^stop$/i))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(control).not.toHaveBeenCalled()
  })

  it('confirming sends cancel for this run', async () => {
    renderWithProviders(<KasRunCard row={kasRow()} approvals={[]} />)
    fireEvent.click(button(/^stop$/i))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /stop run/i }))
    await waitFor(() => expect(control).toHaveBeenCalledWith('wf-1', 'cancel'))
  })
})

describe('KasRunCard — approvals', () => {
  it('renders the command and one button per option; rejects answer like any other', async () => {
    renderWithProviders(<KasRunCard row={kasRow()} approvals={[kasApproval()]} />)
    const card = screen.getByTestId('kas-approval')
    expect(within(card).getByText('npm test -- --run').tagName).toBe('CODE')
    const options = within(card).getAllByRole('button')
    expect(options.map(b => b.textContent)).toEqual(['Allow once', 'Always allow', 'Reject'])
    expect(options[2]).toHaveAttribute('data-option-kind', 'reject_once')
    fireEvent.click(options[2])
    await waitFor(() => expect(answer).toHaveBeenCalledWith('ap-1', 'o-reject'))
  })

  it('a plan approval lists its plan steps', () => {
    renderWithProviders(
      <KasRunCard
        row={kasRow()}
        approvals={[kasApproval({ command: null, title: 'Approve plan', plan: { kind: 'plan', steps: [{ id: 'a', label: 'Plan' }, { id: 'b', label: 'Build' }] } })]}
      />,
    )
    expect(screen.getByTestId('kas-plan-steps')).toHaveTextContent('PlanBuild')
  })

  it('a plan handoff titles itself and shows the plain-text plan verbatim, keeping the option buttons', () => {
    const planText = 'review\n  find the bug\n  write the fix\nverify'
    renderWithProviders(
      <KasRunCard
        row={kasRow()}
        approvals={[
          kasApproval({
            command: null,
            title: 'Run shell command',
            isPlan: true,
            planText,
            options: [
              { optionId: 'o-allow', kind: 'allow_once', name: 'Approve' },
              { optionId: 'o-reject', kind: 'reject_once', name: 'Reject' },
            ],
          }),
        ]}
      />,
    )
    const card = screen.getByTestId('kas-approval')
    expect(card).toHaveAttribute('data-plan', 'true')
    // The plan title replaces the raw approval title.
    expect(within(card).getByText('Approve the workflow plan')).toBeInTheDocument()
    expect(within(card).queryByText('Run shell command')).toBeNull()
    // The plan text is shown verbatim (whitespace preserved), not parsed into a list.
    const block = screen.getByTestId('kas-plan-text')
    expect(block.textContent).toBe(planText)
    expect(screen.queryByTestId('kas-plan-steps')).toBeNull()
    // The offered buttons stay as KAS sent them.
    expect(within(card).getAllByRole('button').map(b => b.textContent)).toEqual(['Approve', 'Reject'])
  })
})

describe('KasRunCard — reconnect and chat', () => {
  it('shows Reconnect for an undriven unfinished run and calls the slot route', async () => {
    renderWithProviders(<KasRunCard row={kasRow({ driven: false })} approvals={[]} />)
    fireEvent.click(button(/reconnect/i))
    await waitFor(() => expect(reconnect).toHaveBeenCalledWith('chat-abc'))
  })

  it('hides Reconnect while a live process drives the run', () => {
    renderWithProviders(<KasRunCard row={kasRow({ driven: true })} approvals={[]} />)
    noButton(/reconnect/i)
  })

  it('Open chat switches to the run chat and navigates to /chat', () => {
    const store = createTestStore()
    const dispatch = vi.spyOn(store, 'dispatch')
    renderWithProviders(<KasRunCard row={kasRow()} approvals={[]} />, { store })
    fireEvent.click(button(/open chat/i))
    expect(dispatch).toHaveBeenCalledWith({ type: 'test/switchSlot', payload: { key: 'chat-abc', announceOnMissing: true, gesture: true } })
    expect(navigate).toHaveBeenCalledWith('/chat')
  })

  it('a run waiting on a person offers Respond in chat as its primary', () => {
    renderWithProviders(
      <KasRunCard
        row={kasRow({ blocking: { nodeId: 'review', label: 'Review', kind: 'question', sessionId: 's', toolCallId: null }, controls: kasControls({ primary: 'open', needsAttention: true }) })}
        approvals={[]}
      />,
    )
    expect(button(/respond in chat/i)).toBeEnabled()
    noButton(/open chat/i)
    expect(screen.getByText(/waiting for an answer: review/i)).toBeInTheDocument()
  })
})
