/**
 * The redacted-projection surface in AutoNudgePopover.
 *
 * `GET /api/autonudge` serves a credential-SCRUBBED `message` and the popover seeds its
 * textarea from it, so two hazards were invisible to the person typing: the mask appeared
 * in their own words unexplained, and a deliberate re-submit of that masked text was
 * dropped by the server's echo guard behind a 200 (`ignored_fields: ["message"]`).
 *
 * `renders_no_notice_when_not_redacted` is the negative control: a notice rendered
 * unconditionally satisfies the first test and fails that one. Rationale in the CR
 * description, reachable from blame via the cr: footer.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'

const BASE: AutoNudgeLoop = {
  id: 'loop-1',
  slot_key: 'chat-1-123',
  message: 'deploy using [REDACTED: aws-access-key-id]',
  idle_secs: 60,
  max_cycles: 0,
  cycle_count: 0,
  active: true,
  last_fire_ts: 0,
} as AutoNudgeLoop

function renderPopover(
  loop: AutoNudgeLoop,
  onOpenChange: (open: boolean) => void = () => {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AutoNudgePopover
        open
        onOpenChange={onOpenChange}
        slotKey="chat-1-123"
        loop={loop}
        onChange={() => {}}
      />
    </QueryClientProvider>,
  )
}

function rerenderOpen(
  rerender: (ui: React.ReactElement) => void,
  loop: AutoNudgeLoop,
  open: boolean,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  rerender(
    <QueryClientProvider client={qc}>
      <AutoNudgePopover
        open={open}
        onOpenChange={() => {}}
        slotKey="chat-1-123"
        loop={loop}
        onChange={() => {}}
      />
    </QueryClientProvider>,
  )
}

function patchBody(fetchMock: { mock: { calls: unknown[][] } }): Record<string, unknown> {
  // The popover may issue other requests while open, so select the PATCH by method
  // rather than trusting a call index.
  const call = fetchMock.mock.calls.find(
    c => (c[1] as { method?: string } | undefined)?.method === 'PATCH',
  )
  if (!call) throw new Error('no PATCH request was issued')
  return JSON.parse((call[1] as { body: string }).body)
}

describe('AutoNudgePopover — the redacted projection is marked, not silent', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    window.localStorage.clear()
  })

  it('marks the textarea when the served message was redacted', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const notice = await screen.findByTestId('autonudge-redacted-notice')
    expect(notice).not.toBeNull()
    expect(notice.textContent ?? '').toMatch(/mask/i)
  })

  it('renders_no_notice_when_not_redacted', async () => {
    // NEGATIVE CONTROL: an unconditional notice passes the test above and fails here.
    renderPopover({ ...BASE, message: 'just keep going', message_redacted: false } as AutoNudgeLoop)
    await waitFor(() => expect(screen.getByRole('textbox', { name: /goal|describe/i })).toBeTruthy())
    expect(screen.queryByTestId('autonudge-redacted-notice')).toBeNull()
  })

  it('tells the user in prose that the stored goal was kept, naming no API key', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE, message_ignored: true }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] now' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    const confirmBtn = await screen.findByTestId('autonudge-confirm-overwrite')
    await waitFor(() => expect(confirmBtn.getAttribute('aria-disabled')).toBe('false'))
    fireEvent.click(confirmBtn)

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const notice = await screen.findByTestId('autonudge-ignored-fields')
    const text = notice.textContent ?? ''
    expect(text).toMatch(/stored goal was kept/i)
    // The raw wire key must not reach the user: it is an untranslated token in every
    // non-English catalog and an ambiguous noun phrase in English.
    expect(text).not.toMatch(/\bmessage\b/i)
    expect(text).not.toMatch(/ignored_fields/)
  })

  it('clears the left-unchanged notice as soon as the goal is edited again', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE, message_ignored: true }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] now' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    const confirmBtn = await screen.findByTestId('autonudge-confirm-overwrite')
    await waitFor(() => expect(confirmBtn.getAttribute('aria-disabled')).toBe('false'))
    fireEvent.click(confirmBtn)
    await screen.findByTestId('autonudge-ignored-fields')

    fireEvent.change(area, { target: { value: 'a fresh goal' } })
    await waitFor(() => expect(screen.queryByTestId('autonudge-ignored-fields')).toBeNull())
  })

  it('will not overwrite a redacted goal without an explicit confirmation', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] plus' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    // The first Save must NOT have WRITTEN: the act is irreversible and the server cannot
    // return the original. Asserted on the PATCH -- the popover also reads while open.
    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')
    expect(
      fetchMock.mock.calls.filter(c => (c[1] as { method?: string } | undefined)?.method === 'PATCH'),
    ).toHaveLength(0)
    expect(confirm.textContent ?? '').toMatch(/overwrite/i)

    await waitFor(() => expect(confirm.getAttribute('aria-disabled')).toBe('false'))
    fireEvent.click(confirm)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(patchBody(fetchMock).message).toContain('plus')
  })

  it('does not gate Save when the goal was not edited', async () => {
    // NEGATIVE CONTROL: an unconditional confirm step passes the arm above and fails here,
    // and would block a user who only changed interval/cycles.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull()
    expect(patchBody(fetchMock).message).toBeUndefined()
  })

  it('does not carry an armed confirmation across a close and reopen', async () => {
    // The [open] seed effect reset only `error`, so the armed confirm survived a
    // dismiss -- the next Save then wrote immediately with no fresh confirmation.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const loop = { ...BASE, message_redacted: true } as AutoNudgeLoop
    const { rerender } = renderPopover(loop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] x' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    await screen.findByTestId('autonudge-confirm-overwrite')

    rerenderOpen(rerender, loop, false)
    rerenderOpen(rerender, loop, true)

    await waitFor(() => expect(screen.getByRole('textbox', { name: /goal|describe/i })).toBeTruthy())
    expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull()
  })

  it('disarms the confirmation when the edit is reverted', async () => {
    // Reachable WITHOUT closing: arm the confirm, then restore the original text. A
    // "Replace goal with masked text" label on a settings-only save is now wrong.
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: `${BASE.message} and more` } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    await screen.findByTestId('autonudge-confirm-overwrite')

    fireEvent.change(area, { target: { value: BASE.message } })
    await waitFor(() =>
      expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull(),
    )
    expect(screen.getByRole('button', { name: /save/i })).toBeTruthy()
  })

  it('keeps the empty-goal guard when the armed confirmation lapses', async () => {
    // The armed button used `disabled={saving}`, dropping the `!message.trim()` half,
    // so clearing the textarea and confirming would submit a blank goal.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'something else entirely' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    await screen.findByTestId('autonudge-confirm-overwrite')

    // Editing now CLEARS the stale confirmation, so the armed button is gone rather
    // than merely inert -- a blank goal has no submit path at all.
    fireEvent.change(area, { target: { value: '   ' } })
    expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull()
    expect((screen.getByRole('button', { name: /save/i }) as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it('announces the confirmation step to a screen reader', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] y' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')

    expect(screen.getByRole('button', { name: /overwrite/i })).toBe(confirm)
    expect(confirm.getAttribute('role')).toBeNull()

    expect(screen.getByTestId('autonudge-confirm-question').getAttribute('role')).toBe('status')
    expect(screen.getByTestId('autonudge-confirm-question').textContent ?? '').toMatch(/overwrite/i)
  })

  it('declining keeps the stored goal but still persists the other settings', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const onOpenChange = vi.fn()
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop, onOpenChange)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] w' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    const decline = await screen.findByTestId('autonudge-decline-overwrite')
    fireEvent.click(decline)

    // Keeping the goal answers the GOAL question only. Dismissing without a write made a
    // changed interval vanish with no notice, which read as a save that happened.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const body = patchBody(fetchMock)
    expect(body.message).toBeUndefined()
    expect(body.idle_secs).toBeDefined()
    // The popover must STAY OPEN: closing reseeds the textarea from the served
    // projection, so the typed wording would be unrecoverable.
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('does not carry an armed confirmation across an edit of the goal text', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    const served = BASE.message as string

    // Arm the gate, then revert to the served text so the gate's own condition lapses.
    fireEvent.change(area, { target: { value: served + ' first' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    expect(await screen.findByTestId('autonudge-confirm-overwrite')).toBeTruthy()
    fireEvent.change(area, { target: { value: served } })
    expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull()

    // Editing again must NOT resurrect the earlier confirmation: it answered the FIRST
    // text, so a pre-armed gate lets one keypress commit a decision never made here.
    fireEvent.change(area, { target: { value: served + ' second' } })
    expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull()
    expect((screen.getByRole('button', { name: /save/i }) as HTMLButtonElement).disabled).toBe(
      false,
    )
  })

  it('replaces Save with a distinct confirm that needs no timed arm', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] z' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    // Deliberateness comes from the confirm/decline PAIR, not from a delay. The arm timer
    // also drove a decay timer whose focus return stole focus mid-edit, so both are gone.
    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')
    expect(confirm.getAttribute('aria-disabled')).toBe('false')
    expect(screen.getByTestId('autonudge-decline-overwrite')).toBeTruthy()
  })

  it('keeps Save mounted but inert while confirming, so a click-through cannot overwrite', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] q' } })
    const save = screen.getByRole('button', { name: /save/i })
    fireEvent.click(save)

    // The confirm must NOT occupy Save's position: swapping it in there let the second
    // half of a double-click land on it, and remounting Save dropped focus to <body>.
    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')
    expect(confirm).not.toBe(save)
    expect(save.isConnected).toBe(true)
    expect((save as HTMLButtonElement).disabled).toBe(true)

    // A click continuing toward Save lands on the inert Save, never on the confirm.
    fireEvent.click(save)
    expect(
      fetchMock.mock.calls.filter(c => (c[1] as { method?: string } | undefined)?.method === 'PATCH'),
    ).toHaveLength(0)
  })

  it('offers a decline beside the destructive confirm', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] plus' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    const decline = await screen.findByTestId('autonudge-decline-overwrite')
    fireEvent.click(decline)

    expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull()
    expect(screen.getByRole('button', { name: /save/i })).toBeTruthy()
    // The invariant is that the STORED GOAL is never overwritten, not that nothing is
    // written: the keep-goal path saves other settings, so assert no ``message`` is sent.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    for (const call of fetchMock.mock.calls.filter(
      c => (c[1] as { method?: string } | undefined)?.method === 'PATCH',
    )) {
      expect(JSON.parse((call[1] as { body: string }).body).message).toBeUndefined()
    }
  })

  it('ignores a HELD Enter on the confirm that just took focus', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] plus' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')
    await waitFor(() => expect(confirm.getAttribute('aria-disabled')).toBe('false'))

    // A repeat keydown is what an Enter held down since the first Save delivers.
    const repeated = fireEvent.keyDown(confirm, { key: 'Enter', repeat: true })
    expect(repeated).toBe(false)
    expect(
      fetchMock.mock.calls.filter(c => (c[1] as { method?: string } | undefined)?.method === 'PATCH'),
    ).toHaveLength(0)

    // NEGATIVE CONTROL: a fresh press must still go through.
    fireEvent.keyDown(confirm, { key: 'Enter', repeat: false })
    fireEvent.click(confirm)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  })

  it('offers the safe path in the redaction notice', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const notice = await screen.findByTestId('autonudge-redacted-notice')
    expect(notice.textContent ?? '').toMatch(/leave the text untouched/i)
  })

  it('guards decline against a repeating Enter so typed work is not discarded', async () => {
    // UX (Fable 5): decline is FOCUSED when the confirm arms, so an Enter held down from
    // Save would otherwise dismiss the gate before the user has read the question.
    const onOpenChange = vi.fn()
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop, onOpenChange)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    const typed = 'deploy using [REDACTED: aws-access-key-id] plus my own note'
    fireEvent.change(area, { target: { value: typed } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    const decline = await screen.findByTestId('autonudge-decline-overwrite')
    expect(document.activeElement).toBe(decline)

    const repeated = fireEvent.keyDown(decline, { key: 'Enter', repeat: true })
    expect(repeated).toBe(false)
    expect((area as HTMLTextAreaElement).value).toBe(typed)

    // NEGATIVE CONTROL: a deliberate fresh press is not swallowed, so the guard is
    // narrowed to key REPEAT rather than disabling the button outright.
    expect(fireEvent.keyDown(decline, { key: 'Enter', repeat: false })).toBe(true)
    // A swallowed repeat must not have closed the popover either.
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('preserves the typed goal on decline instead of discarding it', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      // Keeping the original goal still saves the other settings, so the response
      // replaces the served row. The goal was untouched, so it is still reported redacted.
      json: async () => ({ ok: true, loop: { ...BASE, message_redacted: true } }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const onOpenChange = vi.fn()
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop, onOpenChange)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    const typed = 'deploy using [REDACTED: aws-access-key-id] plus my own note'
    fireEvent.change(area, { target: { value: typed } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    fireEvent.click(await screen.findByTestId('autonudge-decline-overwrite'))

    // UX (Fable 5): decline ran setMessage(loop.message), so one press on the button it
    // auto-focuses wiped work that no draft covers while a loop exists.
    expect((area as HTMLTextAreaElement).value).toBe(typed)
    expect(screen.queryByTestId('autonudge-decline-overwrite')).toBeNull()
    // Preserving the text is meaningless if the popover closed: reopening reseeds the
    // textarea from the served projection, so the typed wording is gone.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(onOpenChange).not.toHaveBeenCalledWith(false)

    // DISMISSED, not satisfied: a further Save must raise the gate again. Wait out the
    // settings write first -- Save is disabled in flight, so an immediate press is a no-op.
    await waitFor(() =>
      expect((screen.getByRole('button', { name: /save/i }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    )
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    expect(await screen.findByTestId('autonudge-confirm-overwrite')).toBeTruthy()
    // What must hold is that no request ever carried ``message``: the stored goal
    // survives both presses.
    for (const call of fetchMock.mock.calls.filter(
      c => (c[1] as { method?: string } | undefined)?.method === 'PATCH',
    )) {
      expect(JSON.parse((call[1] as { body: string }).body).message).toBeUndefined()
    }
  })

  it('asks a question and focuses the safe choice when the confirm row mounts', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] w' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    // Echoing the button label read to a screen reader as though the overwrite had
    // already happened, so the announced line must be a QUESTION about a pending choice.
    const question = await screen.findByTestId('autonudge-confirm-question')
    expect(screen.getAllByRole('status')).toContain(question)
    expect(question.textContent ?? '').toMatch(/\?$/)
    expect(question.textContent ?? '').not.toBe(
      screen.getByTestId('autonudge-confirm-overwrite').textContent,
    )

    // Disabling Save drops focus to <body>; it must land on the non-destructive button.
    const decline = screen.getByTestId('autonudge-decline-overwrite')
    await waitFor(() => expect(document.activeElement).toBe(decline))
    expect(document.activeElement).not.toBe(document.body)
  })

  it('a live goal update cannot make a settings-only save overwrite the stored goal', async () => {
    // The patch inferred "the user edited the goal" by comparing the textarea against
    // `loop.message`, which a live update replaces underneath an open popover.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const { rerender } = renderPopover(BASE)
    rerenderOpen(
      rerender,
      { ...BASE, message: 'a newer goal set from another client' } as AutoNudgeLoop,
      true,
    )
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(patchBody(fetchMock).message).toBeUndefined()
  })

  it('stacks the confirmation controls at every width, not just a narrow viewport', async () => {
    // Two unwrapped buttons in a row overflowed a 320px viewport and clipped the actions.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'a deliberate replacement goal' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    const declineBtn = await screen.findByTestId('autonudge-decline-overwrite')
    const controls = declineBtn.parentElement
    expect(controls?.className).toContain('flex-col')
    // The breakpoint keyed on the VIEWPORT while this row lives in a fixed 420px popover,
    // so a wide screen re-entered the row layout and wrapped the longer labels.
    expect(controls?.className).not.toContain('sm:flex-row')
  })

  it('a live update to a clean newer goal cannot disarm the gate mid-edit', async () => {
    // `message_redacted` was read LIVE, so a websocket replacing `loop` with a clean newer
    // goal turned the gate off while the stale typed text was still PATCHed over it.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const { rerender } = renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'my own replacement goal' } })
    rerenderOpen(
      rerender,
      { ...BASE, message: 'a newer clean goal', message_redacted: false } as AutoNudgeLoop,
      true,
    )
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await screen.findByTestId('autonudge-confirm-overwrite')
    expect(fetchMock.mock.calls.find(c => (c[1] as { method?: string })?.method === 'PATCH')).toBeUndefined()
  })

  it('a live update landing before the first keystroke cannot become its own baseline', async () => {
    // The baseline was latched at the FIRST KEYSTROKE, so a websocket update arriving between
    // render and that keystroke was recorded as the value served, hiding its own overwrite.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const { rerender } = renderPopover(BASE)
    rerenderOpen(
      rerender,
      { ...BASE, message: 'a newer goal from another client' } as AutoNudgeLoop,
      true,
    )
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'my stale replacement' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await screen.findByTestId('autonudge-confirm-overwrite')
    expect(fetchMock.mock.calls.find(c => (c[1] as { method?: string })?.method === 'PATCH')).toBeUndefined()
  })

  it('announces the masked-goal notice to assistive tech like its siblings', async () => {
    // A WS update that masks an open popover's goal was silent: this notice was the only
    // one of the three without a live region, so a screen reader never heard the change.
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const notice = await screen.findByTestId('autonudge-redacted-notice')
    expect(notice.getAttribute('role')).toBe('status')
  })

  it('does not gate a settings-only save on a masked goal nobody edited', async () => {
    // The redacted arm never required an edit, so a WS frame changing the stored goal under
    // an untouched textarea armed a gate whose confirm could destroy nothing.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const { rerender } = renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    rerenderOpen(
      rerender,
      { ...BASE, message_redacted: true, message: 'changed elsewhere' } as AutoNudgeLoop,
      true,
    )
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(screen.queryByTestId('autonudge-confirm-overwrite')).toBeNull()
    expect(patchBody(fetchMock).message).toBeUndefined()
  })

  it('shows the newer goal the moved arm asks the user to discard', async () => {
    // The question named a goal the UI held but never rendered, so confirming destroyed
    // text the user had no way to read.
    const { rerender } = renderPopover(BASE)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'my own edit' } })
    rerenderOpen(rerender, { ...BASE, message: 'a newer goal from elsewhere' } as AutoNudgeLoop, true)
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await screen.findByTestId('autonudge-confirm-overwrite')
    const preview = screen.getByTestId('autonudge-moved-goal-preview')
    expect(preview.textContent ?? '').toContain('a newer goal from elsewhere')
  })

  it('names the newer goal on the confirm button in the moved arm', async () => {
    // Both arms shared "Overwrite original goal", naming the opposite object in the moved
    // arm, where it reads as an offer to restore rather than to discard.
    const { rerender } = renderPopover(BASE)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'my own edit' } })
    rerenderOpen(rerender, { ...BASE, message: 'a newer goal' } as AutoNudgeLoop, true)
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')
    expect(confirm.textContent ?? '').toMatch(/newer/i)
    expect(confirm.textContent ?? '').not.toMatch(/original/i)
  })

  it('prefers the moved arm when the goal is BOTH redacted and moved', async () => {
    // Precedence, not presentation: the redacted arm won this ternary and its preview was
    // gated off, so confirming destroyed a newer goal behind the wrong question entirely.
    const { rerender } = renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'my own edit' } })
    rerenderOpen(
      rerender,
      { ...BASE, message_redacted: true, message: 'a newer goal from elsewhere' } as AutoNudgeLoop,
      true
    )
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')
    const preview = screen.getByTestId('autonudge-moved-goal-preview')
    expect(preview.textContent ?? '').toContain('a newer goal from elsewhere')
    expect(confirm.textContent ?? '').toMatch(/newer/i)
    expect(screen.getByTestId('autonudge-confirm-question').textContent ?? '').toMatch(/newer/i)
  })
})

