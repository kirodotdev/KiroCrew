import { useState } from 'react'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { stubStripHeights } from './stripHeights'
import { SlotProvider } from '../providers/SlotContext'
import type { LegacyGoalLoop } from '../monitoring/automation'
import ChatInput from '../components/ChatInput'
import {
  defaultFireLocal,
  toFireTime,
} from '../components/ScheduleLaterPopover'
import { fmtDateTime, fmtDateTimeNumeric } from '../i18n/format'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import type { SessionRef } from '../utils/sessionRefs'

const scheduled = (over: Partial<LegacyGoalLoop> = {}): LegacyGoalLoop => ({
  kind: 'legacy_goal_loop',
  id: 'scheduled-1',
  slotKey: 'chat-1',
  message: 'follow up with the release owner',
  idleSecs: 60,
  maxCycles: 1,
  cycleCount: 0,
  active: true,
  lastFireAt: 0,
  nextDueAt: 2_000_000_000,
  scheduledMessage: true,
  scheduledAt: 2_000_000_000,
  stoppedReason: '',
  ...over,
})

function rawLoop(at: number, message: string) {
  return {
    id: 'scheduled-1',
    slot_key: 'chat-1',
    message,
    idle_secs: 60,
    max_cycles: 1,
    cycle_count: 0,
    active: true,
    last_fire_ts: 0,
    next_due_ts: at,
    scheduled_message: true,
    scheduled_at: at,
    stopped_reason: '',
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  stubStripHeights()
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ScheduleLaterPopover time conversion', () => {
  const now = Date.parse('2026-03-05T12:00:00')

  function inZone<T>(tz: string, fn: () => T): T {
    const previous = process.env.TZ
    process.env.TZ = tz
    try {
      return fn()
    } finally {
      process.env.TZ = previous
    }
  }

  it('reads local wall clock as epoch seconds', () => {
    expect(toFireTime('2026-03-05T13:30', now)).toBe(
      Math.floor(Date.parse('2026-03-05T13:30') / 1000),
    )
  })

  it('rejects a nonexistent DST minute without rejecting a repeated minute', () => {
    inZone('America/Los_Angeles', () => {
      const springNow = Date.parse('2026-03-08T01:00:00-08:00')
      expect(toFireTime('2026-03-08T02:30', springNow)).toBeNull()
      expect(toFireTime('2026-03-08T03:30', springNow)).toBe(
        Math.floor(Date.parse('2026-03-08T03:30:00-07:00') / 1000),
      )

      const fallNow = Date.parse('2026-11-01T00:30:00-07:00')
      expect(toFireTime('2026-11-01T01:30', fallNow)).toBe(
        Math.floor(Date.parse('2026-11-01T01:30') / 1000),
      )
    })
  })

  it('refuses empty, malformed, current, and past values', () => {
    expect(toFireTime('', now)).toBeNull()
    expect(toFireTime('not-a-date', now)).toBeNull()
    expect(toFireTime('2026-03-05T12:00', now)).toBeNull()
    expect(toFireTime('2026-03-05T11:59', now)).toBeNull()
  })

  it('defaults to a usable future quarter hour', () => {
    const value = defaultFireLocal(Date.parse('2026-03-05T12:07:30'))
    expect(new Date(Date.parse(value)).getMinutes() % 15).toBe(0)
    expect(toFireTime(value, Date.parse('2026-03-05T12:07:30'))).not.toBeNull()
  })

  it('explains why a past picker value cannot be scheduled', async () => {
    renderWithProviders(<Host />)

    await openSendLater()
    fireEvent.change(screen.getByTestId('schedule-later-at'), {
      target: { value: '2020-01-01T00:00' },
    })

    expect(screen.getByText('Pick a time in the future')).toBeInTheDocument()
    expect(screen.getByTestId('schedule-later-confirm')).toBeDisabled()
  })
})

function CachedSchedulingHost({
  authoritative = null,
}: {
  authoritative?: LegacyGoalLoop | null
}) {
  const submitted = 'send exactly this release reminder'
  const [value, setValue] = useState(submitted)
  const [cached, setCached] = useState<LegacyGoalLoop | null>(null)
  const [automationOpen, setAutomationOpen] = useState(false)
  const [connected, setConnected] = useState(true)
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        onUploadFiles={vi.fn()}
        automation={authoritative ?? cached}
        automationOpen={automationOpen}
        onAutomationClick={setAutomationOpen}
        automationCreationReady
        onAutomationChange={(next) => {
          setCached(next?.kind === 'legacy_goal_loop' ? next : null)
          setConnected(false)
        }}
        connected={connected}
      />
    </SlotProvider>
  )
}

function Host({
  initial = 'follow up with the release owner',
  automation = null,
  onAutomationChange = vi.fn(),
  pasteBlocks = [],
  onPasteBlocksChange = vi.fn(),
  pendingFiles = [],
  pendingDirs = [],
  pendingSessions = [],
  collapsible = false,
  creationReady = true,
  slotId = 'chat-1',
  disabled = false,
}: {
  initial?: string
  automation?: LegacyGoalLoop | null
  onAutomationChange?: ReturnType<typeof vi.fn>
  pasteBlocks?: PasteBlock[]
  onPasteBlocksChange?: ReturnType<typeof vi.fn>
  pendingFiles?: string[]
  pendingDirs?: string[]
  pendingSessions?: SessionRef[]
  collapsible?: boolean
  creationReady?: boolean
  slotId?: string
  disabled?: boolean
}) {
  const [value, setValue] = useState(initial)
  const [automationOpen, setAutomationOpen] = useState(false)
  return (
    <SlotProvider slotId={slotId}>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        onUploadFiles={vi.fn()}
        pasteBlocks={pasteBlocks}
        onPasteBlocksChange={onPasteBlocksChange}
        pendingFiles={pendingFiles}
        pendingDirs={pendingDirs}
        pendingSessions={pendingSessions}
        collapsible={collapsible}
        disabled={disabled}
        automation={automation}
        automationOpen={automationOpen}
        onAutomationClick={setAutomationOpen}
        automationCreationReady={creationReady}
        onAutomationChange={onAutomationChange}
      />
    </SlotProvider>
  )
}

async function openSendLater() {
  fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
  const row = await screen.findByTestId('plus-menu-send-later')
  fireEvent.click(row)
  return screen.findByTestId('schedule-later-popover')
}

describe('ChatInput Send later', () => {
  it('shows a visible clock immediately beside Send and opens the same picker', async () => {
    renderWithProviders(<Host />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    const send = screen.getByRole('button', { name: 'Send' })
    expect(clock).toBeEnabled()
    expect(clock.querySelector('.lucide-clock')).toBeTruthy()
    expect(clock.parentElement).toBe(send.parentElement)
    expect(clock.nextElementSibling).toBe(send)

    fireEvent.click(clock)
    expect(await screen.findByTestId('schedule-later-popover')).toBeInTheDocument()
  })

  it('keeps the visible clock discoverable while explaining why it is disabled', () => {
    renderWithProviders(<Host initial="" />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeDisabled()
    expect(clock).toHaveAttribute('title', 'Type a message first.')
  })

  it.each(['macOS', 'Windows'])('keeps Send later enabled on %s', async (platform) => {
    const view = renderWithProviders(<Host />)
    view.queryClient.setQueryData(['kiro-prerequisite'], { platform })

    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    const sendLater = await screen.findByTestId('plus-menu-send-later')
    expect(sendLater).toBeEnabled()
    expect(sendLater).toHaveTextContent('Delivers this draft at a time you pick')
  })

  it('keeps a static description on the enabled row', async () => {
    renderWithProviders(<Host />)

    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    const row = await screen.findByTestId('plus-menu-send-later')

    expect(row).toBeEnabled()
    expect(row).toHaveTextContent('Delivers this draft at a time you pick')
  })

  it('closes skill suggestions when the plus menu takes focus', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/slash-commands' || url.startsWith('/api/skills')) {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(null, { status: 204 }))
    }))
    renderWithProviders(<Host initial="" />)

    const input = screen.getByLabelText('Message input')
    fireEvent.change(input, { target: { value: 'use $missing' } })
    expect(await screen.findByRole('listbox')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(await screen.findByTestId('plus-menu-send-later')).toBeInTheDocument()
  })

  it('creates a one-shot AutoNudge record with the current draft', async () => {
    const at = Math.floor(Date.now() / 1000) + 3600
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(
        new Response(JSON.stringify({ ok: true, loop: rawLoop(at, 'follow up with the release owner') }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    const onAutomationChange = vi.fn()
    renderWithProviders(<Host onAutomationChange={onAutomationChange} />)

    await openSendLater()
    const local = new Date(at * 1000 - new Date(at * 1000).getTimezoneOffset() * 60000)
      .toISOString().slice(0, 16)
    fireEvent.change(screen.getByTestId('schedule-later-at'), { target: { value: local } })
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))

    await waitFor(() => expect(onAutomationChange).toHaveBeenCalledTimes(1))
    const scheduledCall = fetchMock.mock.calls.find(([url]) => url === '/api/autonudge')
    expect(scheduledCall).toBeDefined()
    const [, options] = scheduledCall!
    expect(JSON.parse(options.body)).toEqual({
      slot_key: 'chat-1',
      message: 'follow up with the release owner',
      at: toFireTime(local),
    })
    expect(screen.getByLabelText('Message input')).toHaveValue('')
  })

  it('preserves exact composer bytes while validating trimmed non-emptiness', async () => {
    const exact = '  /command @notes/release.md $deploy-skill  \n'
    const at = Math.floor(Date.now() / 1000) + 3600
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify({
        ok: true,
        loop: rawLoop(at, exact),
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const onAutomationChange = vi.fn()
    renderWithProviders(<Host initial={exact} onAutomationChange={onAutomationChange} />)

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))

    await waitFor(() => expect(onAutomationChange).toHaveBeenCalledTimes(1))
    const scheduledCall = fetchMock.mock.calls.find(([url]) => url === '/api/autonudge')
    expect(JSON.parse(scheduledCall![1].body).message).toBe(exact)
    expect(onAutomationChange.mock.calls[0][0].message).toBe(exact)
    expect(screen.getByLabelText('Message input')).toHaveValue('')
  })

  it('expands only referenced paste tokens and clears their draft after success', async () => {
    const at = Math.floor(Date.now() / 1000) + 3600
    const paste: PasteBlock = {
      id: 'paste-1',
      seq: 1,
      lines: 4,
      content: 'first line\nsecond line\nthird line\nfourth line',
    }
    const unreferenced: PasteBlock = {
      id: 'paste-2',
      seq: 2,
      lines: 4,
      content: 'must not be sent\nsecond\nthird\nfourth',
    }
    const draft = `before ${formatToken(paste)} after`
    const expanded = `before ${paste.content} after`
    let resolvePost!: (response: Response) => void
    const postResponse = new Promise<Response>(resolve => { resolvePost = resolve })
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (String(input) === '/api/autonudge') return postResponse
      return Promise.resolve(new Response(null, { status: 204 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const onAutomationChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <Host
        initial={draft}
        pasteBlocks={[paste, unreferenced]}
        onPasteBlocksChange={onPasteBlocksChange}
        onAutomationChange={onAutomationChange}
      />,
    )
    onPasteBlocksChange.mockClear()

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/autonudge',
      expect.objectContaining({ method: 'POST' }),
    ))

    const scheduledCall = fetchMock.mock.calls.find(([url]) => url === '/api/autonudge')
    expect(JSON.parse(scheduledCall![1].body).message).toBe(expanded)
    expect(screen.getByLabelText('Message input')).toHaveValue(draft)
    expect(onPasteBlocksChange).not.toHaveBeenCalled()

    await act(async () => {
      resolvePost(new Response(JSON.stringify({ loop: rawLoop(at, expanded) }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      await postResponse
    })

    await waitFor(() => expect(onAutomationChange).toHaveBeenCalledTimes(1))
    expect(onAutomationChange.mock.calls[0][0].message).toBe(expanded)
    expect(screen.getByLabelText('Message input')).toHaveValue('')
    expect(onPasteBlocksChange).toHaveBeenCalledWith([])
  })

  it('does not clear a rebound paste sidecar after an older schedule succeeds', async () => {
    const at = Math.floor(Date.now() / 1000) + 3600
    const original: PasteBlock = {
      id: 'paste-original',
      seq: 1,
      lines: 4,
      content: 'original\nsecond\nthird\nfourth',
    }
    const rebound: PasteBlock = {
      id: 'paste-rebound',
      seq: 1,
      lines: 4,
      content: 'new content\nsecond\nthird\nfourth',
    }
    const draft = formatToken(original)
    const originalBlocks = [original]
    const reboundBlocks = [rebound]
    let resolvePost!: (response: Response) => void
    const postResponse = new Promise<Response>(resolve => { resolvePost = resolve })
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (String(input) === '/api/autonudge') return postResponse
      return Promise.resolve(new Response(null, { status: 204 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const originalPasteChange = vi.fn()
    const reboundPasteChange = vi.fn()
    const originalAutomationChange = vi.fn()
    const reboundAutomationChange = vi.fn()
    const view = renderWithProviders(
      <Host
        initial={draft}
        pasteBlocks={originalBlocks}
        onPasteBlocksChange={originalPasteChange}
        onAutomationChange={originalAutomationChange}
      />,
    )

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/autonudge',
      expect.objectContaining({ method: 'POST' }),
    ))
    view.rerender(
      <Host
        initial={draft}
        pasteBlocks={reboundBlocks}
        onPasteBlocksChange={reboundPasteChange}
        onAutomationChange={reboundAutomationChange}
      />,
    )

    await act(async () => {
      resolvePost(new Response(JSON.stringify({ loop: rawLoop(at, original.content) }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      await postResponse
    })

    await waitFor(() => expect(reboundAutomationChange).toHaveBeenCalledTimes(1))
    expect(screen.getByLabelText('Message input')).toHaveValue(draft)
    expect(originalPasteChange).not.toHaveBeenCalled()
    expect(reboundPasteChange).not.toHaveBeenCalled()
    expect(originalAutomationChange).not.toHaveBeenCalled()
  })

  it('does not apply a completed schedule request to a different active slot', async () => {
    const at = Math.floor(Date.now() / 1000) + 3600
    let resolvePost!: (response: Response) => void
    const postResponse = new Promise<Response>(resolve => { resolvePost = resolve })
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (String(input) === '/api/autonudge') return postResponse
      return Promise.resolve(new Response(null, { status: 204 }))
    }))
    const firstChange = vi.fn()
    const secondChange = vi.fn()
    const view = renderWithProviders(<Host onAutomationChange={firstChange} />)

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))
    view.rerender(<Host slotId="chat-2" onAutomationChange={secondChange} />)
    await act(async () => {
      resolvePost(new Response(JSON.stringify({ loop: rawLoop(at, 'follow up with the release owner') }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      await postResponse
    })

    expect(firstChange).not.toHaveBeenCalled()
    expect(secondChange).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Message input')).toHaveValue('follow up with the release owner')
  })

  it('reconciles the draft after a WebSocket schedule update wins the HTTP race', async () => {
    const at = Math.floor(Date.now() / 1000) + 3600
    let resolvePost!: (response: Response) => void
    const postResponse = new Promise<Response>(resolve => { resolvePost = resolve })
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (String(input) === '/api/autonudge') return postResponse
      return Promise.resolve(new Response(null, { status: 204 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const firstChange = vi.fn()
    const newerChange = vi.fn()
    const view = renderWithProviders(<Host onAutomationChange={firstChange} />)

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/autonudge',
      expect.objectContaining({ method: 'POST' }),
    ))
    view.rerender(
      <Host
        automation={scheduled({ id: 'newer-record', message: 'newer scheduled message' })}
        onAutomationChange={newerChange}
      />,
    )
    await act(async () => {
      resolvePost(new Response(JSON.stringify({ loop: rawLoop(at, 'follow up with the release owner') }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      await postResponse
    })

    expect(firstChange).not.toHaveBeenCalled()
    expect(newerChange).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Message input')).toHaveValue('')
    expect(screen.getByTestId('scheduled-message-banner')).toHaveTextContent(
      'newer scheduled message',
    )
  })

  it('keeps the submitted text across a metadata-only response and disconnect', async () => {
    const submitted = 'send exactly this release reminder'
    const composerMutation = 'keep this newer composer draft'
    const authoritative = 'protected server text wins later'
    const at = Math.floor(Date.now() / 1000) + 3600
    let resolvePost!: (response: Response) => void
    const postResponse = new Promise<Response>((resolve) => { resolvePost = resolve })
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (String(input) === '/api/autonudge') return postResponse
      return Promise.resolve(new Response(null, { status: 204 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = renderWithProviders(<CachedSchedulingHost />)

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/autonudge',
      expect.objectContaining({ method: 'POST' }),
    ))
    fireEvent.change(screen.getByLabelText('Message input'), {
      target: { value: composerMutation },
    })
    resolvePost(new Response(JSON.stringify({ ok: true, loop: rawLoop(at, '') }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))

    const banner = await screen.findByTestId('scheduled-message-banner')
    expect(banner).toHaveTextContent(submitted)
    expect(banner).not.toHaveTextContent(composerMutation)
    expect(screen.getByLabelText('Message input')).toHaveValue(composerMutation)

    fireEvent.click(screen.getByTestId('scheduled-message-edit'))
    expect(await screen.findByRole('textbox', { name: 'Message' })).toHaveValue(submitted)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    view.rerender(<CachedSchedulingHost authoritative={scheduled({ message: authoritative })} />)
    expect(screen.getByTestId('scheduled-message-banner')).toHaveTextContent(authoritative)
    fireEvent.click(screen.getByTestId('scheduled-message-edit'))
    expect(await screen.findByRole('textbox', { name: 'Message' })).toHaveValue(authoritative)
  })

  it('keeps the draft and shows the server conflict when a raced arm loses', async () => {
    /* The arm route answers every refusal with `autonudge_not_armed`; the slot
       conflict is its 409 shape. This is the exact payload the backend sends. */
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(
        new Response(JSON.stringify({
          error: 'session already has an automation',
          code: 'autonudge_not_armed',
        }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    }))
    renderWithProviders(<Host />)

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))

    expect(await screen.findByText(
      'A message is already scheduled. Unschedule it or edit it before scheduling another.',
    )).toBeInTheDocument()
    expect(screen.getByLabelText('Message input')).toHaveValue('follow up with the release owner')
  })

  it('does not call a non-409 autonudge_not_armed refusal a conflict', async () => {
    /* Opposite failure mode: the same code at 400 is a validation refusal, not a
       slot conflict, so the generic keep-your-draft copy is the honest one. */
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(
        new Response(JSON.stringify({
          error: 'banner too long',
          code: 'autonudge_not_armed',
        }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    }))
    renderWithProviders(<Host />)

    await openSendLater()
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))

    const notice = await screen.findByTestId('schedule-error')
    expect(notice).toHaveTextContent(
      "Couldn't schedule the message. Your draft was kept — try again.",
    )
    expect(notice).not.toHaveTextContent('already scheduled')
    expect(screen.getByLabelText('Message input')).toHaveValue('follow up with the release owner')
  })

  it('disables Send later while a goal already owns the session and explains why', async () => {
    renderWithProviders(<Host automation={scheduled({ scheduledAt: undefined, maxCycles: 5 })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    const sendLater = await screen.findByTestId('plus-menu-send-later')
    expect(sendLater).toBeDisabled()
    expect(sendLater).toHaveAttribute('title', 'Goal active (cycle 0)')
    expect(screen.getByText('Goal active (cycle 0)')).toBeInTheDocument()
  })

  it('describes an existing scheduled message without calling it an active goal', async () => {
    renderWithProviders(<Host automation={scheduled()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))

    const sendLater = await screen.findByTestId('plus-menu-send-later')
    expect(sendLater).toBeDisabled()
    expect(sendLater).toHaveAttribute('title', 'A message is already scheduled for this session.')
    expect(sendLater).toHaveTextContent('A message is already scheduled for this session.')
    expect(sendLater).not.toHaveTextContent('Goal active')
  })

  it('describes a scheduled message that has started sending, not an active goal', async () => {
    // Dispatch moves cycle_count off 0 while the record keeps its scheduled
    // identity; the server now refuses cancel/edit as scheduled_message_in_flight.
    renderWithProviders(<Host automation={scheduled({ cycleCount: 1 })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))

    const sendLater = await screen.findByTestId('plus-menu-send-later')
    expect(sendLater).toBeDisabled()
    expect(sendLater).toHaveAttribute(
      'title',
      'This message is already sending. Wait for delivery to finish.',
    )
    expect(sendLater).toHaveTextContent('This message is already sending. Wait for delivery to finish.')
    expect(sendLater).not.toHaveTextContent('Goal active')
    expect(sendLater).not.toHaveTextContent('already scheduled for this session')
    // The pending banner offers Unschedule, which the server would refuse now.
    expect(screen.queryByTestId('scheduled-message-banner')).toBeNull()
  })

  it('keeps the goal-loop reason for a cycling goal that is not a scheduled message', async () => {
    renderWithProviders(
      <Host automation={scheduled({ scheduledMessage: undefined, scheduledAt: undefined, maxCycles: 5, cycleCount: 1 })} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))

    const sendLater = await screen.findByTestId('plus-menu-send-later')
    expect(sendLater).toBeDisabled()
    expect(sendLater).toHaveAttribute('title', 'Goal active (cycle 1)')
    expect(sendLater).not.toHaveTextContent('already sending')
  })

  it('shows and cancels the pending scheduled message', async () => {
    const onAutomationChange = vi.fn()
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify({
        ok: true,
        message: 'authoritative restored draft',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWithProviders(
      <Host initial="" automation={scheduled()} onAutomationChange={onAutomationChange} />,
    )

    const banner = screen.getByTestId('scheduled-message-banner')
    expect(banner).toBeInTheDocument()
    expect(banner.textContent?.indexOf('Scheduled message')).toBeGreaterThanOrEqual(0)
    fireEvent.click(screen.getByTestId('scheduled-message-cancel'))

    await waitFor(() => expect(onAutomationChange).toHaveBeenCalledWith(null))
    expect(screen.getByLabelText('Message input')).toHaveValue('authoritative restored draft')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/autonudge/scheduled-1?intent=stop',
      { method: 'DELETE' },
    )
  })

  it('preserves Markdown hard-break spaces while restoring an unscheduled message', async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify({
        ok: true,
        message: 'authoritative restored draft',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWithProviders(
      <Host initial={'keep this hard break  \n'} automation={scheduled()} />,
    )

    fireEvent.click(screen.getByTestId('scheduled-message-cancel'))

    await waitFor(() => expect(screen.getByLabelText('Message input')).toHaveValue(
      'keep this hard break  \n\nauthoritative restored draft',
    ))
  })

  it('restores the draft after a WebSocket unschedule update wins the HTTP race', async () => {
    let resolveDelete!: (response: Response) => void
    const deleteResponse = new Promise<Response>(resolve => { resolveDelete = resolve })
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (String(input) === '/api/autonudge/scheduled-1?intent=stop') return deleteResponse
      return Promise.resolve(new Response(null, { status: 204 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const beforeWebSocketChange = vi.fn()
    const afterWebSocketChange = vi.fn()
    const view = renderWithProviders(
      <Host initial="" automation={scheduled()} onAutomationChange={beforeWebSocketChange} />,
    )

    fireEvent.click(screen.getByTestId('scheduled-message-cancel'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/autonudge/scheduled-1?intent=stop',
      { method: 'DELETE' },
    ))
    view.rerender(
      <Host initial="" automation={null} onAutomationChange={afterWebSocketChange} />,
    )
    await act(async () => {
      resolveDelete(new Response(JSON.stringify({
        ok: true,
        message: 'authoritative restored draft',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      await deleteResponse
    })

    expect(beforeWebSocketChange).not.toHaveBeenCalled()
    expect(afterWebSocketChange).not.toHaveBeenCalled()
    expect(screen.queryByTestId('scheduled-message-banner')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Message input')).toHaveValue('authoritative restored draft')
  })
})


/* The one-shot POST carries `{slot_key, message, at}` and nothing else. Every
   staged source the ordinary send serializes BESIDE the text would therefore be
   left behind, so each one must make Send later unavailable, with the same
   reason on every surface, and release it again once the draft is text only.
   Paste tokens are deliberately absent from this table: they expand INTO the
   text, so a collapsed paste is not an attachment. */
describe('ChatInput Send later with staged attachments', () => {
  const TEXT_ONLY = 'Scheduled messages send text only. Remove the attached files, folders, or session references first.'
  const sessionRef: SessionRef = { key: 'chat-9', title: 'Release notes', messages: 12 }
  const sources = [
    ['a pending file', { pendingFiles: ['/tmp/upload/screenshot.png'] }],
    ['a folder token', { initial: 'review @website/docs/ please', pendingDirs: ['website/docs/'] }],
    ['a session reference', { pendingSessions: [sessionRef] }],
    ['every source at once', {
      initial: 'review @website/docs/ please',
      pendingFiles: ['/tmp/upload/screenshot.png'],
      pendingDirs: ['website/docs/'],
      pendingSessions: [sessionRef],
    }],
  ] as const

  it.each(sources)('disables the plus-menu row and the clock for %s', async (_label, props) => {
    renderWithProviders(<Host {...props} />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeDisabled()
    expect(clock).toHaveAttribute('title', TEXT_ONLY)

    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    const row = await screen.findByTestId('plus-menu-send-later')
    expect(row).toBeDisabled()
    expect(row).toHaveAttribute('title', TEXT_ONLY)
    expect(row).toHaveTextContent(TEXT_ONLY)
    expect(row).not.toHaveTextContent('Delivers this draft at a time you pick')
  })

  it('names the attachment, not the missing text, when a file is staged on an empty draft', () => {
    // Typing cannot clear this blocker, so the row must not send the user to type.
    renderWithProviders(<Host initial="" pendingFiles={['/tmp/upload/screenshot.png']} />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeDisabled()
    expect(clock).toHaveAttribute('title', TEXT_ONLY)
    expect(clock).not.toHaveAttribute('title', 'Type a message first.')
  })

  it('keeps text-only drafts schedulable, including a collapsed paste token', async () => {
    const paste: PasteBlock = {
      id: 'paste-1',
      seq: 1,
      lines: 4,
      content: 'first line\nsecond line\nthird line\nfourth line',
    }
    renderWithProviders(<Host initial={`before ${formatToken(paste)} after`} pasteBlocks={[paste]} />)

    expect(screen.getByRole('button', { name: 'Send later…' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    const row = await screen.findByTestId('plus-menu-send-later')
    expect(row).toBeEnabled()
    expect(row).toHaveTextContent('Delivers this draft at a time you pick')
  })

  it.each(sources)('re-enables Send later once %s is removed', async (_label, props) => {
    const view = renderWithProviders(<Host {...props} />)
    expect(screen.getByRole('button', { name: 'Send later…' })).toBeDisabled()

    view.rerender(<Host initial={'initial' in props ? props.initial : undefined} />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeEnabled()
    expect(clock).toHaveAttribute('title', 'Send later…')
    fireEvent.click(clock)
    expect(await screen.findByTestId('schedule-later-popover')).toBeInTheDocument()
  })

  it('closes an open picker and refuses to POST when a file lands mid-pick', async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(null, { status: 204 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = renderWithProviders(<Host />)

    await openSendLater()
    view.rerender(<Host pendingFiles={['/tmp/upload/screenshot.png']} />)

    expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send later…' })).toBeDisabled()
    expect(fetchMock.mock.calls.find(([url]) => url === '/api/autonudge')).toBeUndefined()
    expect(screen.getByLabelText('Message input')).toHaveValue('follow up with the release owner')
  })
})

describe('ChatInput Send later while the composer is disabled', () => {
  // `scheduleComposer` already refused a disabled composer; the reason chain
  // did not, so every Send later control stayed enabled, the picker opened,
  // and Schedule returned with no request and no error.
  const STOPPING = 'Stopping…'
  const TEXT_ONLY = 'Scheduled messages send text only. Remove the attached files, folders, or session references first.'

  it('disables the clock and the plus-menu row with the stopping reason', async () => {
    renderWithProviders(<Host disabled />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeDisabled()
    expect(clock).toHaveAttribute('title', STOPPING)
    fireEvent.click(clock)
    expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    const row = await screen.findByTestId('plus-menu-send-later')
    expect(row).toBeDisabled()
    expect(row).toHaveAttribute('title', STOPPING)
    expect(row).toHaveTextContent(STOPPING)
    expect(row).not.toHaveTextContent('Delivers this draft at a time you pick')
    fireEvent.click(row)
    expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument()
  })

  it('keeps the attachment reason ahead of the stopping reason', () => {
    renderWithProviders(<Host disabled pendingFiles={['/tmp/upload/screenshot.png']} />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeDisabled()
    expect(clock).toHaveAttribute('title', TEXT_ONLY)
  })

  it('names the stopping composer, not the missing text, on an empty draft', () => {
    // The editor is disabled too, so sending the user to type would be a dead end.
    renderWithProviders(<Host disabled initial="" />)

    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeDisabled()
    expect(clock).toHaveAttribute('title', STOPPING)
    expect(clock).not.toHaveAttribute('title', 'Type a message first.')
  })

  it('closes an open picker and refuses to POST when the composer is disabled mid-pick', async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(null, { status: 204 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = renderWithProviders(<Host />)

    await openSendLater()
    view.rerender(<Host disabled />)

    expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument()
    const clock = screen.getByRole('button', { name: 'Send later…' })
    expect(clock).toBeDisabled()
    expect(clock).toHaveAttribute('title', STOPPING)
    expect(fetchMock.mock.calls.find(([url]) => url === '/api/autonudge')).toBeUndefined()
    expect(screen.getByLabelText('Message input')).toHaveValue('follow up with the release owner')
  })
})

it('opens the scheduled-message editor from the banner', async () => {
  renderWithProviders(<Host automation={scheduled()} />)

  fireEvent.click(screen.getByTestId('scheduled-message-edit'))

  expect(await screen.findByRole('textbox', { name: 'Message' })).toHaveValue(
    'follow up with the release owner',
  )
  expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue(
    'follow up with the release owner',
  )
  expect(screen.getByLabelText('Send at')).toBeInTheDocument()
})

it('expands a collapsed composer before opening the scheduled-message editor', async () => {
  localStorage.setItem('mc-composer-collapsed', '1')
  renderWithProviders(<Host automation={scheduled()} collapsible />)

  expect(screen.queryByLabelText('Message input')).not.toBeInTheDocument()
  fireEvent.click(screen.getByTestId('scheduled-message-edit'))

  await new Promise(requestAnimationFrame)
  expect(screen.getByLabelText('Message input')).toBeInTheDocument()
  await waitFor(() => expect(
    screen.getByRole('textbox', { name: /^Message$/ }),
  ).toHaveValue('follow up with the release owner'))
})


it('uses the dedicated Schedule message confirmation label and a readable picker width', async () => {
  renderWithProviders(<Host />)

  const popover = await openSendLater()
  expect(screen.getByTestId('schedule-later-confirm')).toHaveTextContent('Schedule message')
  expect(popover.className).toContain('w-[min(320px,calc(100vw-16px))]')
  expect(screen.getByTestId('schedule-later-at')).toHaveClass('w-full', 'min-w-0')
  expect(screen.getByTestId('schedule-later-at')).toHaveAccessibleName('Send at')
})

it('keeps the full scheduled message while clamping the banner to two lines', () => {
  const message = 'Follow up with the release owner about the complete launch checklist.'
  renderWithProviders(<Host automation={scheduled({ message })} />)

  const banner = screen.getByTestId('scheduled-message-banner')
  expect(banner).toHaveTextContent('Scheduled message')
  expect(banner).toHaveTextContent(message)
  expect(screen.getByText(message)).toHaveClass('line-clamp-2')
  expect(banner.querySelector('[title]')).toBeNull()
})

it('shows the scheduled time at the picker\'s minute precision, in the active locale', () => {
  const at = 2_000_000_000
  renderWithProviders(<Host automation={scheduled({ scheduledAt: at })} />)

  const banner = screen.getByTestId('scheduled-message-banner')
  /* The picker is a minute field, so the `:00` the numeric width appends is a
     value the reader never chose. Both assertions go through the locale seam
     rather than a literal, so the pin holds in every shipped language. */
  expect(banner).toHaveTextContent(fmtDateTime(at))
  expect(banner).not.toHaveTextContent(fmtDateTimeNumeric(at))
  expect(banner.textContent).not.toMatch(/\d:\d\d:\d\d/)
})

it('shows a visible Edit affordance on the banner beside Unschedule', () => {
  renderWithProviders(<Host automation={scheduled()} />)

  const edit = screen.getByTestId('scheduled-message-edit')
  const affordance = screen.getByTestId('scheduled-message-edit-affordance')
  // The affordance lives INSIDE the press target: the whole banner text still
  // opens the editor, and the row stays at two actions (edit, unschedule).
  expect(edit).toContainElement(affordance)
  expect(affordance).toHaveTextContent('Edit')
  expect(affordance.className).toContain('border')
  expect(affordance.querySelector('.lucide-pen-line')).toBeTruthy()
  expect(edit).toHaveAccessibleName(/Edit$/)
  expect(edit.className).toContain('hover:bg-bg-hover')
  const banner = screen.getByTestId('scheduled-message-banner')
  expect(banner.querySelectorAll('[role="button"], button')).toHaveLength(2)
})

it('uses one monitor recovery sentence when Send later is disabled', async () => {
  renderWithProviders(<Host automation={{
    kind: 'monitor',
    id: 'monitor-1',
    slotKey: 'chat-1',
    active: true,
  } as never} />)

  fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
  const sendLater = await screen.findByTestId('plus-menu-send-later')
  expect(sendLater).toHaveTextContent('Stop the monitor to schedule a message.')
})

it('explains a not-ready session in scheduling terms, not monitor terms', async () => {
  renderWithProviders(<Host creationReady={false} />)

  fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
  const sendLater = await screen.findByTestId('plus-menu-send-later')
  const reason = "Couldn't load this session's state yet, so a message can't be scheduled. Try again in a moment."
  expect(sendLater).toBeDisabled()
  expect(sendLater).toHaveAttribute('title', reason)
  expect(sendLater).toHaveTextContent(reason)
  expect(sendLater).not.toHaveTextContent('monitor')
})
