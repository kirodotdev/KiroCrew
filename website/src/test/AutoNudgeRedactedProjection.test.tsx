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

function renderPopover(loop: AutoNudgeLoop) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AutoNudgePopover
        open
        onOpenChange={() => {}}
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

  it('tells the user in prose that the goal was left unchanged, naming no API key', async () => {
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
    expect(text).toMatch(/left unchanged/i)
    // The raw wire key must not reach the user: it is an untranslated token in every
    // non-English catalog and an ambiguous noun phrase in English.
    expect(text).not.toMatch(/\bmessage\b/i)
    expect(text).not.toMatch(/ignored_fields/)
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

  it('keeps the empty-goal guard on the armed confirmation button', async () => {
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
    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')

    fireEvent.change(area, { target: { value: '   ' } })
    expect(confirm.getAttribute('aria-disabled')).toBe('true')
  })

  it('announces the confirmation step to a screen reader', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] y' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    const confirm = await screen.findByTestId('autonudge-confirm-overwrite')

    expect(screen.getByRole('button', { name: /overwrite/i })).toBe(confirm)
    expect(confirm.getAttribute('role')).toBeNull()

    expect(screen.getByRole('status').textContent ?? '').toMatch(/overwrite/i)
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
    expect(
      fetchMock.mock.calls.filter(c => (c[1] as { method?: string } | undefined)?.method === 'PATCH'),
    ).toHaveLength(0)
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
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
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
  })

  it('preserves the typed goal on decline instead of discarding it', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, loop: BASE }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    const typed = 'deploy using [REDACTED: aws-access-key-id] plus my own note'
    fireEvent.change(area, { target: { value: typed } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    fireEvent.click(await screen.findByTestId('autonudge-decline-overwrite'))

    // UX (Fable 5): decline ran setMessage(loop.message), so one press on the button it
    // auto-focuses wiped work that no draft covers while a loop exists.
    expect((area as HTMLTextAreaElement).value).toBe(typed)
    expect(screen.queryByTestId('autonudge-decline-overwrite')).toBeNull()

    // DISMISSED, not satisfied: the text still differs from the served projection, so a
    // further Save must raise the gate again rather than overwrite the stored goal.
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    expect(await screen.findByTestId('autonudge-confirm-overwrite')).toBeTruthy()
    expect(
      fetchMock.mock.calls.filter(
        c => (c[1] as { method?: string } | undefined)?.method === 'PATCH',
      ),
    ).toHaveLength(0)
  })

  it('asks a question and focuses the safe choice when the confirm row mounts', async () => {
    renderPopover({ ...BASE, message_redacted: true } as AutoNudgeLoop)
    const area = screen.getByRole('textbox', { name: /goal|describe/i })
    fireEvent.change(area, { target: { value: 'deploy using [REDACTED: aws-access-key-id] w' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    // Echoing the button label read to a screen reader as though the overwrite had
    // already happened, so the announced line must be a QUESTION about a pending choice.
    const question = await screen.findByTestId('autonudge-confirm-question')
    expect(screen.getByRole('status')).toBe(question)
    expect(question.textContent ?? '').toMatch(/\?$/)
    expect(question.textContent ?? '').not.toBe(
      screen.getByTestId('autonudge-confirm-overwrite').textContent,
    )

    // Disabling Save drops focus to <body>; it must land on the non-destructive button.
    const decline = screen.getByTestId('autonudge-decline-overwrite')
    await waitFor(() => expect(document.activeElement).toBe(decline))
    expect(document.activeElement).not.toBe(document.body)
  })
})

