import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { normalizeAutomationRecord, type LegacyGoalLoop } from '../monitoring/automation'
import SessionAutomationPopover from '../components/SessionAutomationPopover'

const raw = {
  id: 'scheduled-1',
  slot_key: 'chat-1',
  message: 'follow up with the release owner',
  idle_secs: 60,
  max_cycles: 1,
  cycle_count: 0,
  active: true,
  last_fire_ts: 0,
  next_due_ts: 2_000_000_000,
  scheduled_message: true,
  scheduled_at: 2_000_000_000,
  stopped_reason: '',
}

const scheduled = normalizeAutomationRecord(raw) as LegacyGoalLoop

afterEach(() => {
  vi.unstubAllGlobals()
})

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

describe('scheduled AutoNudge records', () => {
  it('normalizes the absolute scheduled time without changing the automation family', () => {
    expect(scheduled).toMatchObject({
      kind: 'legacy_goal_loop',
      scheduledMessage: true,
      scheduledAt: 2_000_000_000,
      nextDueAt: 2_000_000_000,
      maxCycles: 1,
    })
  })

  it('keeps ordinary goal loops free of a scheduled marker', () => {
    const goal = normalizeAutomationRecord({
      ...raw,
      scheduled_message: false,
      scheduled_at: 0,
    }) as LegacyGoalLoop
    expect(goal.scheduledMessage).toBeUndefined()
    expect(goal.scheduledAt).toBeUndefined()
  })

  it('renders scheduled details instead of the Set a Goal editor', () => {
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    expect(screen.getByText('Scheduled message')).toBeInTheDocument()
    expect(screen.getByText('follow up with the release owner')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Goal description' })).not.toBeInTheDocument()
    expect(document.querySelector('.lucide-clock')).toBeTruthy()
  })

  it('gives the run-once datetime input the full narrow-panel width', () => {
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('Send at')).toHaveClass('w-full', 'min-w-0')
  })

  it('explains why Unschedule is disabled after dispatch starts', () => {
    const sending = normalizeAutomationRecord({ ...raw, cycle_count: 1 }) as LegacyGoalLoop
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={sending}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: 'Unschedule' })).toBeDisabled()
    expect(screen.getByText('This message is already sending. Wait for delivery to finish.'))
      .toBeInTheDocument()
  })

  it('disables Save alongside Unschedule once dispatch starts and describes both with the reason', () => {
    const sending = normalizeAutomationRecord({ ...raw, cycle_count: 1 }) as LegacyGoalLoop
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={sending}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    const reason = screen.getByText('This message is already sending. Wait for delivery to finish.')
    expect(reason).toHaveAttribute('id')
    const save = screen.getByRole('button', { name: 'Save' })
    const unschedule = screen.getByRole('button', { name: 'Unschedule' })
    expect(save).toBeDisabled()
    expect(unschedule).toBeDisabled()
    expect(save).toHaveAccessibleDescription(
      'This message is already sending. Wait for delivery to finish.',
    )
    expect(unschedule).toHaveAccessibleDescription(
      'This message is already sending. Wait for delivery to finish.',
    )
  })

  it('leaves Save enabled and undescribed while the message is still pending', () => {
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'still editable' },
    })
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeEnabled()
    expect(save).not.toHaveAttribute('aria-describedby')
    expect(screen.getByRole('button', { name: 'Unschedule' })).toBeEnabled()
  })

  it('cancels through the existing AutoNudge delete route', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      ok: true,
      message: 'follow up with the release owner',
    })))
    vi.stubGlobal('fetch', fetchMock)
    const onChange = vi.fn()
    const onOpenChange = vi.fn()
    const onRestoreScheduledMessage = vi.fn()
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={onOpenChange}
        onChange={onChange}
        onRestoreScheduledMessage={onRestoreScheduledMessage}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Unschedule' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null))
    expect(onRestoreScheduledMessage).toHaveBeenCalledWith(
      'follow up with the release owner',
    )
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/autonudge/scheduled-1?intent=stop',
      { method: 'DELETE' },
    )
  })

  it('treats a provenance-less 200 cancel as success and restores the record text', async () => {
    // `api_autonudge_delete` answers `{ok: true}` with no `message` when the
    // row's provenance record is missing or unreadable -- the row is removed
    // regardless, so the popover must clear the schedule rather than report a
    // failure over a banner the server no longer backs.
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({ ok: true })))
    vi.stubGlobal('fetch', fetchMock)
    const onChange = vi.fn()
    const onOpenChange = vi.fn()
    const onRestoreScheduledMessage = vi.fn()
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={onOpenChange}
        onChange={onChange}
        onRestoreScheduledMessage={onRestoreScheduledMessage}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Unschedule' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null))
    expect(onRestoreScheduledMessage).toHaveBeenCalledWith('follow up with the release owner')
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(screen.queryByText('The monitor request failed. Try again.')).not.toBeInTheDocument()
  })

  it('still reports a non-2xx cancel as a failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(
      { error: 'boom' },
      500,
    ))))
    const onChange = vi.fn()
    const onRestoreScheduledMessage = vi.fn()
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={vi.fn()}
        onChange={onChange}
        onRestoreScheduledMessage={onRestoreScheduledMessage}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Unschedule' }))

    expect(await screen.findByText("Couldn't schedule the message. Your draft was kept — try again."))
      .toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
    expect(onRestoreScheduledMessage).not.toHaveBeenCalled()
  })

  it('keeps the schedule and draft when delivery wins the unschedule race', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(
      {
        error: 'scheduled message delivery has already started',
        code: 'scheduled_message_in_flight',
      },
      409,
    ))))
    const onChange = vi.fn()
    const onRestoreScheduledMessage = vi.fn()
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={vi.fn()}
        onChange={onChange}
        onRestoreScheduledMessage={onRestoreScheduledMessage}
      />,
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'keep this scheduled edit' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Unschedule' }))

    expect(await screen.findByText('This message is already sending. Wait for delivery to finish.')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('keep this scheduled edit')
    expect(onChange).not.toHaveBeenCalled()
    expect(onRestoreScheduledMessage).not.toHaveBeenCalled()
  })
})


describe('scheduled AutoNudge editing', () => {
  it('saves an edited prompt without truncating unchanged scheduled seconds', async () => {
    const withSeconds = normalizeAutomationRecord({ ...raw, scheduled_at: 2_000_000_037 }) as LegacyGoalLoop
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      ok: true,
      loop: { ...raw, message: 'updated prompt', scheduled_at: 2_000_000_037 },
    })))
    vi.stubGlobal('fetch', fetchMock)
    const onChange = vi.fn()
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={withSeconds}
        open
        onOpenChange={vi.fn()}
        onChange={onChange}
      />,
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'updated prompt' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1))
    const scheduledCall = fetchMock.mock.calls.find(([url]) => url === '/api/autonudge/scheduled-1')
    expect(scheduledCall).toBeDefined()
    expect(JSON.parse(scheduledCall![1].body)).toEqual({ message: 'updated prompt' })
  })

  it('sends at when the Send later control changes', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      ok: true,
      loop: { ...raw, scheduled_at: 2_000_003_600 },
    })))
    vi.stubGlobal('fetch', fetchMock)
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )
    const moved = new Date(2_000_003_600 * 1000)
    const local = new Date(moved.getTime() - moved.getTimezoneOffset() * 60_000)
      .toISOString().slice(0, 16)

    fireEvent.change(screen.getByLabelText('Send at'), { target: { value: local } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const scheduledCall = fetchMock.mock.calls.find(([url]) => url === '/api/autonudge/scheduled-1')
    expect(scheduledCall).toBeDefined()
    expect(JSON.parse(scheduledCall![1].body)).toEqual({
      at: Math.floor(2_000_003_600 / 60) * 60,
    })
  })

  it('keeps edits visible when save fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(
      { error: 'schedule changed elsewhere' },
      409,
    ))))
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={scheduled}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'keep this draft' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Couldn\'t schedule the message. Your draft was kept — try again.')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('keep this draft')
  })
})


describe('scheduled AutoNudge past-due editing', () => {
  // A record whose stored instant has passed but which the server still holds
  // (delivery not yet started). The same retained-time rule as the run-once
  // JobForm applies: the time it already has is saveable until it is changed.
  const pastAt = 1_700_000_000
  const pastDue = normalizeAutomationRecord({
    ...raw, scheduled_at: pastAt, next_due_ts: pastAt,
  }) as LegacyGoalLoop
  const localOf = (epochSecs: number) => {
    const date = new Date(epochSecs * 1000)
    return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
  }

  it('saves a text-only edit with the retained past time and sends no at', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      ok: true,
      loop: { ...raw, message: 'reworded before it goes', scheduled_at: pastAt, next_due_ts: pastAt },
    })))
    vi.stubGlobal('fetch', fetchMock)
    const onChange = vi.fn()
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={pastDue}
        open
        onOpenChange={vi.fn()}
        onChange={onChange}
      />,
    )

    const at = screen.getByLabelText('Send at')
    expect(at).toHaveValue(localOf(pastAt))
    expect(at).toHaveAttribute('aria-invalid', 'false')
    expect(at).not.toHaveAttribute('aria-describedby')
    expect(screen.queryByText('Pick a time in the future')).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'reworded before it goes' },
    })
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1))
    const scheduledCall = fetchMock.mock.calls.find(([url]) => url === '/api/autonudge/scheduled-1')
    expect(scheduledCall).toBeDefined()
    expect(JSON.parse(scheduledCall![1].body)).toEqual({ message: 'reworded before it goes' })
  })

  it('still rejects a newly chosen past time on a past-due record', () => {
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={pastDue}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    const at = screen.getByLabelText('Send at')
    fireEvent.change(at, { target: { value: '2020-01-01T00:00' } })

    const error = screen.getByText('Pick a time in the future')
    expect(at).toHaveAttribute('aria-invalid', 'true')
    expect(at).toHaveAttribute('aria-describedby', error.id)
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()

    // Putting the retained instant back is not a change, so the record is
    // saveable again exactly as it was.
    fireEvent.change(at, { target: { value: localOf(pastAt) } })
    expect(screen.queryByText('Pick a time in the future')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('sends only the new future time when the text is left unchanged', async () => {
    const movedAt = Math.floor(Date.now() / 1000) + 7200
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      ok: true,
      loop: { ...raw, scheduled_at: movedAt, next_due_ts: movedAt },
    })))
    vi.stubGlobal('fetch', fetchMock)
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={pastDue}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByLabelText('Send at'), { target: { value: localOf(movedAt) } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const scheduledCall = fetchMock.mock.calls.find(([url]) => url === '/api/autonudge/scheduled-1')
    expect(scheduledCall).toBeDefined()
    expect(JSON.parse(scheduledCall![1].body)).toEqual({ at: Math.floor(movedAt / 60) * 60 })
  })

  it('keeps Save disabled for a text edit once delivery of a past-due record is in flight', () => {
    const sending = normalizeAutomationRecord({
      ...raw, scheduled_at: pastAt, next_due_ts: pastAt, cycle_count: 1,
    }) as LegacyGoalLoop
    renderWithProviders(
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={sending}
        open
        onOpenChange={vi.fn()}
        onChange={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'too late to reword' },
    })
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    expect(save).toHaveAccessibleDescription(
      'This message is already sending. Wait for delivery to finish.',
    )
    expect(screen.queryByText('Pick a time in the future')).not.toBeInTheDocument()
  })
})
