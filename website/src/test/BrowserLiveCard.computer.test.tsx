import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import '../i18n/all'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import TurnBlock from '../pages/chat/TurnBlock'
import BrowserLiveStage, { turnChoices } from '../pages/chat/BrowserLiveStage'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import { browserLiveStore } from '../hooks/useBrowserLive'
import { browserStageStore } from '../hooks/useBrowserLive.stage'
import { computerStepLabel, computerToolName, readComputerFrame } from '../hooks/useBrowserLive.computer'

/**
 * Computer use in the chat's live card: the `computer_use_frame` event becomes a
 * desktop record in the live store, and TurnBlock mounts the same card over it
 * after the turn's last computer tool row. No takeover, nothing written to the
 * browser relay, and the browser's record is left alone.
 */

const S = 'slot:abc'
const JPEG_B64 = btoa(String.fromCharCode(0xff, 0xd8, 0xff, 0xd9))
const cuFrame = (over: Record<string, unknown> = {}) => ({ data: JPEG_B64, format: 'jpeg', width: 640, height: 400, session_key: S, app: 'Finder', ...over })
const browserEv = (over: Record<string, unknown> = {}) => ({ session_key: S, turn_id: 't1', state: 'live', url: 'https://example.com/', title: 'Example', browser: 'kiro_chromium', headless: false, steps: [], ...over })

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let writes: Required<Pick<Writes, 'ack' | 'visibility' | 'claim'>>
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

beforeEach(() => {
  browserLiveStore.__resetForTests()
  writes = { ack: vi.fn(async () => ({})), visibility: vi.fn(async () => ({})), claim: vi.fn(async () => ({})) }
  restore = browserLiveStore.__setWritesForTests(writes)
  // happy-dom has no 2D canvas; the painter is exercised by its own tests.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.restoreAllMocks()
})

describe('readComputerFrame', () => {
  it('turns a checked event into the card frame shape', () => {
    const read = readComputerFrame(cuFrame(), 7, 1000)
    expect(read?.app).toBe('Finder')
    expect(read?.frame.header).toEqual({ type: 'browser_frame', session_key: S, stream: 'computer', seq: 7, w: 640, h: 400, mask: [] })
    expect(Array.from(read?.frame.jpeg ?? [])).toEqual([0xff, 0xd8, 0xff, 0xd9])
    expect(read?.frame.receivedAt).toBe(1000)
  })

  it.each([
    ['no data', { data: '' }],
    ['not base64', { data: 'ab:c' }],
    ['a broken quad', { data: 'QUJDRA=' }],
    ['another encoding', { format: 'png' }],
    ['no width', { width: undefined }],
    ['a fractional height', { height: 1.5 }],
    ['an unsafe session key', { session_key: 'a b' }],
  ])('drops a frame with %s', (_name, over) => {
    expect(readComputerFrame(cuFrame(over), 1)).toBeNull()
  })

  it('keeps the frame and drops an app name outside its charset', () => {
    expect(readComputerFrame(cuFrame({ app: '<script>' }), 1)?.app).toBe('')
  })
})

describe('computer tool rows', () => {
  it('name the tool from the trusted name, else the pill', () => {
    expect(computerToolName({ role: 'tool', content: '🔧 x', cls: '', meta: { tool_name: 'kirocrew-computer___computer_click' } })).toBe('computer_click')
    expect(computerToolName({ role: 'tool', content: '🔧 @kirocrew-computer/computer_get_state', cls: '' })).toBe('computer_get_state')
    expect(computerToolName({ role: 'tool', content: '🔧 shell', cls: '' })).toBe('')
    expect(computerToolName({ role: 'assistant', content: '🔧 computer_click', cls: '' })).toBe('')
    expect(computerStepLabel('computer_type_text')).toBe('type text')
  })
})

describe('the desktop record', () => {
  it('lives beside the browser record: neither ends the other', () => {
    act(() => browserLiveStore.applyEvent(browserEv()))
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    act(() => browserLiveStore.applyEvent(browserEv({ turn_id: 't2' })))
    const turns = browserLiveStore.sessionTurns(S)
    const desktop = turns.find(t => t.kind === 'computer')
    expect(desktop?.state).toBe('live')
    expect(desktop?.hasFrame).toBe(true)
    expect(desktop?.title).toBe('Finder')
    expect(browserLiveStore.current(S)?.turnId).toBe('t2')
    expect(writes.ack).not.toHaveBeenCalled()
  })
})

describe('replay budgets and the turn picker', () => {
  it('keep desktop rings apart from browser rings', () => {
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    for (const id of ['b1', 'b2', 'b3']) act(() => browserLiveStore.applyEvent(browserEv({ turn_id: id })))
    const desktop = browserLiveStore.sessionTurns(S).find(t => t.kind === 'computer')
    expect(desktop?.ring.length).toBe(1)
  })

  it('list desktop records only in a desktop picker, and never in a browser one', () => {
    act(() => browserLiveStore.applyEvent(browserEv()))
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    const local = browserLiveStore.sessionTurns(S)
    const server = [{ turnId: 'srv', startedTs: 1, endedTs: 2, live: false, frameCount: 1, eventCount: 0, startUrl: 'https://a.test/' }]
    expect(turnChoices(server, local).map(c => c.turnId).sort()).toEqual(['srv', 't1'])
    expect(turnChoices(server, local, true).map(c => [c.turnId, c.host])).toEqual([['computer:1', 'Finder']])
  })
})

describe('TurnBlock mounts the desktop card', () => {
  const row = (id: string, tool: string): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `🔧 ${tool}`, cls: '', ts: String(Math.floor(Date.now() / 1000) + Number(id)), meta: { tool_call_id: `tc-${id}`, tool_name: tool } }, idx: Number(id) }) // flake-ok: the settled-turn binding compares the row clock with the record's client clock (±120 s)
  const turn = (items: TurnItem[], complete = false): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })
  const renderItem = (it: TurnItem) => <div data-testid={`row-${it.kind === 'single' ? (it.msg.meta?.tool_call_id as string) ?? it.msg.role : 'group'}`} />
  const rows = [row('1', 'computer_get_state'), row('2', 'computer_click'), row('3', 'shell')]

  it('after the last computer row, with the calls as steps and no takeover', () => {
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    const { container } = render(<TurnBlock turn={turn(rows)} renderItem={renderItem} browserLiveSessionKey={S} />)
    const card = screen.getByTestId('browser-live-card')
    expect(card.getAttribute('data-source')).toBe('computer')
    // Placed after row 2 (the last computer row), before the shell row.
    const order = Array.from(container.querySelectorAll('[data-testid^="row-"], [data-testid="browser-live-card"]')).map(e => e.getAttribute('data-testid'))
    expect(order).toEqual(['row-tc-1', 'row-tc-2', 'browser-live-card', 'row-tc-3'])
    expect(screen.queryByTestId('browser-live-take-over')).toBeNull()
    expect(screen.getByText('Finder')).toBeTruthy()
    const steps = screen.getByRole('list', { name: 'Steps' })
    expect(steps.textContent).toContain('get state')
    expect(steps.querySelector('[aria-current="step"]')?.textContent).toContain('click')
    expect(writes.visibility).not.toHaveBeenCalled()
  })

  it('folds to its done line when the turn ends, with Replay pinned to its own record', () => {
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    const { rerender } = render(<TurnBlock turn={turn(rows)} renderItem={renderItem} browserLiveSessionKey={S} />)
    rerender(<TurnBlock turn={turn(rows, true)} renderItem={renderItem} browserLiveSessionKey={S} />)
    expect(screen.getByTestId('browser-live-done')).toBeTruthy()
    expect(screen.getByText('Used Finder')).toBeTruthy()
    expect(screen.queryByTestId('browser-live-open-live')).toBeNull()
    const open = vi.spyOn(browserStageStore, 'open').mockImplementation(() => {})
    act(() => screen.getByTestId('browser-live-replay').click())
    const desktop = browserLiveStore.sessionTurns(S).find(t => t.kind === 'computer')
    expect(open).toHaveBeenCalledWith(S, expect.objectContaining({ turnId: desktop?.turnId, mode: 'replay' }))
    expect(desktop?.log.map(e => e.label)).toEqual(['get state', 'click'])
  })

  it('a later turn starts its own record instead of drawing into the last one', () => {
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    const first = render(<TurnBlock turn={turn(rows)} renderItem={renderItem} browserLiveSessionKey={S} />)
    // The first turn's card is gone (scrolled off) before its turn ended.
    first.unmount()
    render(<TurnBlock turn={turn([row('11', 'computer_get_state')])} renderItem={renderItem} browserLiveSessionKey={S} />)
    act(() => browserLiveStore.applyComputerFrame(cuFrame({ app: 'Notes' })))
    const desktops = browserLiveStore.sessionTurns(S).filter(t => t.kind === 'computer')
    expect(desktops.map(t => [t.state, t.title])).toEqual([['done', 'Finder'], ['live', 'Notes']])
    expect(screen.getByText('Notes')).toBeTruthy()
  })

  it('the turn end of the session folds the record even with no card on screen, so the next frame starts a new one', () => {
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    act(() => browserLiveStore.endComputerSession(S))
    act(() => browserLiveStore.applyComputerFrame(cuFrame({ app: 'Notes' })))
    const desktops = browserLiveStore.sessionTurns(S).filter(t => t.kind === 'computer')
    expect(desktops.map(t => [t.state, t.title])).toEqual([['done', 'Finder'], ['live', 'Notes']])
    // Then the next turn's card binds to the new record, frame first.
    render(<TurnBlock turn={turn([row('11', 'computer_get_state')])} renderItem={renderItem} browserLiveSessionKey={S} />)
    expect(screen.getByText('Notes')).toBeTruthy()
  })

  it('a secure window sends no frame, so a turn with none shows no card', () => {
    render(<TurnBlock turn={turn(rows)} renderItem={renderItem} browserLiveSessionKey={S} />)
    expect(screen.queryByTestId('browser-live-card')).toBeNull()
  })
})

describe('the big view of a desktop record', () => {
  it('shows it live with no takeover and asks the gateway for no recording', () => {
    const recording = vi.spyOn(api, 'browserLiveRecording')
    vi.spyOn(api, 'browserLiveRecordings').mockResolvedValue({ turns: [] } as never)
    act(() => browserLiveStore.applyComputerFrame(cuFrame()))
    const id = browserLiveStore.sessionTurns(S).find(t => t.kind === 'computer')?.turnId ?? ''
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <BrowserLiveStage sessionKey={S} stage={{ sessionKey: S, turnId: id, mode: 'live', surface: 'panel', nonce: 1 }} active />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('browser-live-stage')).toBeTruthy()
    expect(screen.queryByTestId('browser-live-stage-take-over')).toBeNull()
    expect(recording).not.toHaveBeenCalled()
    expect(writes.visibility).not.toHaveBeenCalled()
  })
})
