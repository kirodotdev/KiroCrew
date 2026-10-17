/** The goal popover sends the stored goal's baseline, so a save over a goal
 *  saved elsewhere is refused instead of overwriting it.
 *
 *  A small fake gateway stands in for the three routes the popover uses: it
 *  serves the slot read with its `goal_token`, compares `expect_fingerprint` on
 *  PATCH the way the store does (absent skips the check; anything else must
 *  equal the current token), re-mints the token on every goal write, and
 *  answers fire with the record. */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import { __resetForTests } from '../utils/goalDrafts'

const SLOT = 'chat-1-100'
const SAVE_AND_NUDGE = 'Save edits and nudge now'
const STALE = 'Not saved: the goal was changed or reloaded since you opened this, and your text is kept. Press “Save edits and nudge now” again to replace the stored goal with your text.'

/** What the websocket frame hands the popover: the record WITHOUT a token. */
const frameLoop = (over: Partial<AutoNudgeLoop> = {}): AutoNudgeLoop => ({
  id: 'l1', slot_key: SLOT, message: 'the stored goal',
  idle_secs: 90, max_cycles: 0, cycle_count: 1, active: true, last_fire_ts: 0,
  next_due_ts: Math.floor(Date.now() / 1000) + 300, ...over,
})

type Call = [string, { method?: string, body?: string }?]

function fakeGateway(initial: { message: string, token: string | undefined }) {
  const store = { ...initial, n: 0 }
  const record = (): AutoNudgeLoop => ({ ...frameLoop({ message: store.message }), ...(store.token ? { goal_token: store.token } : {}) })
  const json = (status: number, body: unknown) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) })
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    const u = String(url)
    if (u === `/api/autonudge/slot/${SLOT}`) return json(200, { enabled: true, loop: record() })
    if (u === '/api/autonudge/l1' && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      if ('expect_fingerprint' in body && (!body.expect_fingerprint || body.expect_fingerprint !== store.token)) {
        return json(409, { error: 'server english', code: 'goal_stale_baseline' })
      }
      if (typeof body.message === 'string') {
        store.message = body.message
        store.token = store.token === undefined ? undefined : `tok-${++store.n}`
      }
      return json(200, { ok: true, loop: record() })
    }
    if (u === '/api/autonudge/l1/fire') return json(200, { ok: true, loop: record() })
    return json(200, { loop: null })
  }) as unknown as typeof fetch)
  return {
    store,
    /** Another window's save: a new goal and a new token, behind this popover's back. */
    otherWindowSaves(message: string) { store.message = message; store.token = `tok-other-${++store.n}` },
  }
}

const calls = () => (fetch as unknown as { mock: { calls: Call[] } }).mock.calls
const patches = () => calls().filter(c => c[1]?.method === 'PATCH').map(c => JSON.parse(c[1]!.body!) as Record<string, unknown>)
const fires = () => calls().filter(c => String(c[0]) === '/api/autonudge/l1/fire')
const slotReads = () => calls().filter(c => String(c[0]) === `/api/autonudge/slot/${SLOT}`)
const goalBox = () => screen.getByRole('textbox', { name: 'Goal description' }) as HTMLTextAreaElement
const saveAndNudge = () => screen.getByRole('button', { name: SAVE_AND_NUDGE })

function renderOpen(loop: AutoNudgeLoop) {
  const Harness = () => {
    const [open, setOpen] = useState(true)
    return (
      <AutoNudgePopover slotKey={SLOT} loop={loop} open={open} onOpenChange={setOpen} onChange={vi.fn()} onFired={vi.fn()} />
    )
  }
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <Harness />
    </QueryClientProvider>,
  )
}

async function editGoalAndSave(text: string) {
  fireEvent.change(goalBox(), { target: { value: text } })
  await act(async () => { fireEvent.click(saveAndNudge()) })
}

describe('AutoNudgePopover goal baseline', () => {
  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('reads the baseline at open even when the record came through a frame, and sends it with a goal save', async () => {
    fakeGateway({ message: 'the stored goal', token: 'tok-0' })
    renderOpen(frameLoop())
    await waitFor(() => expect(slotReads()).toHaveLength(1))
    await editGoalAndSave('my edit')
    expect(patches()).toEqual([{ message: 'my edit', expect_fingerprint: 'tok-0' }])
    expect(fires()).toHaveLength(1)
  })

  it('a goal saved in another window after open is refused, the text kept, nothing fired; the next press replaces it', async () => {
    const gw = fakeGateway({ message: 'the stored goal', token: 'tok-0' })
    renderOpen(frameLoop())
    await waitFor(() => expect(slotReads()).toHaveLength(1))
    gw.otherWindowSaves('the other window goal')
    await editGoalAndSave('my edit')
    expect(screen.getByTestId('auto-nudge-error')).toHaveTextContent(STALE)
    expect(goalBox().value).toBe('my edit')
    expect(fires()).toHaveLength(0)
    expect(gw.store.message).toBe('the other window goal')
    // The refusal re-read what is stored; the reader was told, so a second press wins.
    await waitFor(() => expect(slotReads()).toHaveLength(2))
    await act(async () => { fireEvent.click(saveAndNudge()) })
    expect(gw.store.message).toBe('my edit')
    expect(patches()[1]).toEqual({ message: 'my edit', expect_fingerprint: 'tok-other-1' })
    expect(fires()).toHaveLength(1)
  })

  it('a stored goal that already differs from the seeded text at open refuses the save rather than overwriting it', async () => {
    const gw = fakeGateway({ message: 'a newer stored goal', token: 'tok-0' })
    renderOpen(frameLoop({ message: 'the goal this frame showed' }))
    await waitFor(() => expect(slotReads()).toHaveLength(1))
    await editGoalAndSave('my edit')
    expect(patches()[0]).toEqual({ message: 'my edit', expect_fingerprint: '' })
    expect(screen.getByTestId('auto-nudge-error')).toHaveTextContent(STALE)
    expect(gw.store.message).toBe('a newer stored goal')
  })

  it('a second save after one that landed is fenced by the token that save handed back', async () => {
    const gw = fakeGateway({ message: 'the stored goal', token: 'tok-0' })
    renderOpen(frameLoop())
    await waitFor(() => expect(slotReads()).toHaveLength(1))
    await editGoalAndSave('first edit')
    await editGoalAndSave('second edit')
    expect(patches().map(p => p.expect_fingerprint)).toEqual(['tok-0', 'tok-1'])
    expect(gw.store.message).toBe('second edit')
  })

  it('a cadence-only save carries no baseline: the fence guards the goal text', async () => {
    fakeGateway({ message: 'the stored goal', token: 'tok-0' })
    renderOpen(frameLoop())
    await waitFor(() => expect(slotReads()).toHaveLength(1))
    fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '120' } })
    await act(async () => { fireEvent.click(saveAndNudge()) })
    expect(patches()).toEqual([{ idle_secs: 120 }])
  })

  it('a gateway that serves no token leaves the save as it was (unfenced)', async () => {
    fakeGateway({ message: 'the stored goal', token: undefined })
    renderOpen(frameLoop())
    await waitFor(() => expect(slotReads()).toHaveLength(1))
    await editGoalAndSave('my edit')
    expect(patches()).toEqual([{ message: 'my edit' }])
    expect(fires()).toHaveLength(1)
  })
})
