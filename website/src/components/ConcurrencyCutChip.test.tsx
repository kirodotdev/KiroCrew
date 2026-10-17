// ConcurrencyCutChip — the chat header's chip while the adaptive controller
// holds a concurrency cut, the per-session marker beside it, and the phone top
// bar's menu row and trigger dot. All render the gateway's own `throttle`
// verdict from GET /api/spawn/adaptive; nothing here decides on its own whether
// a cap is low.
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { act } from 'react'
import { useLocation } from 'react-router-dom'
import { createConnectedTestStore, createTestStore, renderWithProviders } from '../test/helpers'
import ChatHeaderChips from './ChatHeaderChips'
import ConcurrencyCutChip, {
  CAPACITY_CARD_PATH,
  ConcurrencyCutDot,
  ConcurrencyCutMenuItem,
  SessionQueuedMarker,
  THROTTLE_REFETCH_MS,
  THROTTLE_RETRY_MS,
  throttleChipKey,
  throttleChipLabel,
  throttleShortCap,
  type CutMenuItemComponent,
} from './ConcurrencyCutChip'
import { api } from '../api/client'
import { sseSubagentQueued } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
// `/all` registers every catalog, so a test can switch to bn; `../i18n` alone
// registers English, and an unregistered language renders Latin digits.
import { i18next } from '../i18n/all'
import { i18nT } from '../i18n/t'
import { CATALOGS } from '../i18n/catalogs'
import type { SpawnAdaptiveResponse, ThrottleView } from '../api/tasks'
import type { ChatSlot, SessionLink } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, spawnAdaptive: vi.fn() } }
})

const spawnAdaptive = vi.mocked(api.spawnAdaptive)

const execCut: ThrottleView = {
  cut: true,
  exec: { cap: 2, configured: 8, cut: true },
  gate: { cap: 4, configured: 4, cut: false, paused: false },
  queued: 5,
}
const gateOnly: ThrottleView = {
  cut: true,
  exec: { cap: 8, configured: 8, cut: false },
  gate: { cap: 1, configured: 4, cut: true, paused: false },
  queued: 0,
}
const EXEC = { cap: 2, configured: 8 }

/** A chip sentence as plain text, tags stripped, the way a reader hears it. */
const plain = (key: string, values: Record<string, number> = {}) =>
  i18nT(`components.concurrencyCutChip.${key}`, values).replace(/<\/?[01]>/g, '')
/** One tagged part of a chip sentence: `<0>` is the lead, `<1>` the detail. */
const part = (key: string, name: 'lead' | 'detail', values: Record<string, number> = {}) => {
  const tag = name === 'lead' ? '0' : '1'
  return new RegExp(`<${tag}>(.*)</${tag}>`).exec(i18nT(`components.concurrencyCutChip.${key}`, values))![1]
}
const markerText = (queued: number) => i18nT('components.concurrencyCutChip.session_queued', { queued })

function answer(throttle: ThrottleView | undefined) {
  const body: SpawnAdaptiveResponse = { adaptive: {}, ...(throttle ? { throttle } : {}) }
  spawnAdaptive.mockResolvedValue(body)
}

const findChip = () => screen.findByTestId('concurrency-cut-chip')

afterEach(() => { vi.clearAllMocks() })

describe('throttleChipKey', () => {
  it('names nothing without a cut', () => {
    expect(throttleChipKey(undefined)).toBeNull()
    expect(throttleChipKey({ cut: false })).toBeNull()
    expect(throttleChipLabel({ cut: false })).toBeNull()
    expect(throttleShortCap({ cut: false })).toBeNull()
  })

  it('names the reduced execution cap and its queue', () => {
    expect(throttleChipKey(execCut)).toEqual({
      key: 'components.concurrencyCutChip.exec_reduced_queued', values: { cap: '2', configured: '8', queued: '5' },
    })
    expect(throttleChipKey({ ...execCut, queued: 0 })?.key).toBe('components.concurrencyCutChip.exec_reduced')
    expect(throttleChipKey(execCut, { withQueue: false })?.key).toBe('components.concurrencyCutChip.exec_reduced')
    expect(throttleChipLabel(execCut)).toBe(plain('exec_reduced_queued', { ...EXEC, queued: 5 }))
    expect(throttleShortCap(execCut)).toBe('2/8')
  })

  it('names new agents starting when only the spawn gate is cut', () => {
    expect(throttleChipKey(gateOnly)).toEqual({
      key: 'components.concurrencyCutChip.gate_reduced', values: { cap: '1', configured: '4' },
    })
    expect(throttleShortCap(gateOnly)).toBe('1/4')
    const paused = { ...gateOnly, gate: { ...gateOnly.gate!, paused: true } }
    expect(throttleChipKey(paused)?.key).toBe('components.concurrencyCutChip.gate_paused')
    // A paused gate has no pair of numbers to shorten to.
    expect(throttleShortCap(paused)).toBeNull()
  })

  it('names nothing for a cut whose numbers did not arrive', () => {
    expect(throttleChipKey({ cut: true, exec: { cap: null, configured: 8, cut: true } })).toBeNull()
  })

  it('leads every sentence with the outcome', () => {
    for (const key of ['exec_reduced', 'exec_reduced_queued', 'gate_reduced', 'gate_paused']) {
      expect(part(key, 'lead', { ...EXEC, queued: 1 })).toBe(part('exec_reduced', 'lead', EXEC))
    }
  })

  it('says the failed read in terms of queued agents and the gate cut as a limit on new agents', () => {
    expect(i18nT('components.concurrencyCutChip.load_failed')).toBe('Could not check for queued agents')
    expect(part('gate_reduced', 'detail', { cap: 1, configured: 4 })).toBe('new agents start 1 at a time, down from 4')
  })

  it('keeps the visible reassurance line word for word inside the hover hint, in every catalog', () => {
    for (const [lang, { translation }] of Object.entries(CATALOGS)) {
      if (lang === 'en-XA') continue
      const block = (translation.components as Record<string, Record<string, string>>).concurrencyCutChip
      expect(block.queue_kept, lang).toBeTruthy()
      expect(block.hint, lang).toContain(block.queue_kept)
    }
  })

  describe('locale numerals', () => {
    afterEach(async () => { await i18next.changeLanguage('en') })
    const LATIN = /[0-9]/
    const BENGALI = /[\u09e6-\u09ef]/

    it('formats every sentence number in the locale numerals the narrow form uses', async () => {
      await i18next.changeLanguage('bn')
      const big = { ...execCut, exec: { cap: 2, configured: 18, cut: true }, queued: 15 }
      for (const label of [throttleChipLabel(big), throttleChipLabel(big, { withQueue: false }), throttleChipLabel(gateOnly)]) {
        expect(label).toMatch(BENGALI)
        expect(label).not.toMatch(LATIN)
      }
      expect(throttleShortCap(big)).toMatch(BENGALI)
      expect(throttleShortCap(big)).not.toMatch(LATIN)
    })

    it('renders the wide chip and the session marker in one numeral system', async () => {
      await i18next.changeLanguage('bn')
      answer(execCut)
      const store = createTestStore()
      store.dispatch(sseSubagentQueued({ slot: 's1', queued: 3 }))
      renderWithProviders(<><ConcurrencyCutChip /><SessionQueuedMarker slotKey="s1" /></>, { store })
      const link = await findChip()
      const detail = link.querySelector('[data-chip-part="detail"]')!.textContent!
      const short = link.querySelector('[data-chip-part="short-cap"]')!.textContent!
      const marker = (await screen.findByTestId('session-queued-marker')).textContent!
      for (const text of [detail, short, marker]) {
        expect(text).toMatch(BENGALI)
        expect(text).not.toMatch(LATIN)
      }
    })
  })
})

describe('ConcurrencyCutChip', () => {
  it('renders an empty live region while no cut is in force', async () => {
    answer({ cut: false })
    renderWithProviders(<ConcurrencyCutChip />)
    await waitFor(() => expect(spawnAdaptive).toHaveBeenCalled())
    const region = screen.getByRole('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
    expect(region.textContent).toBe('')
    expect(screen.queryByTestId('concurrency-cut-chip')).toBeNull()
  })

  it('renders nothing for an older gateway that sends no throttle reading', async () => {
    answer(undefined)
    renderWithProviders(<ConcurrencyCutChip />)
    await waitFor(() => expect(spawnAdaptive).toHaveBeenCalled())
    expect(screen.getByRole('status').textContent).toBe('')
    expect(screen.queryByTestId('concurrency-cut-chip')).toBeNull()
  })

  it('shows the cut as a link to the capacity card and announces the cut alone', async () => {
    answer(execCut)
    renderWithProviders(<ConcurrencyCutChip />)
    const hint = i18nT('components.concurrencyCutChip.hint')
    const link = await findChip()
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe(CAPACITY_CARD_PATH)
    expect(link.getAttribute('title')).toBe(hint)
    expect(link.querySelector('[data-chip-part="lead"]')?.textContent).toBe(part('exec_reduced', 'lead', EXEC))
    expect(link.querySelector('[data-chip-part="detail"]')?.textContent)
      .toBe(part('exec_reduced_queued', 'detail', { ...EXEC, queued: 5 }))
    // The sentence stays the link's name; the hint is a visually hidden
    // description, not an aria-label that would replace the name.
    expect(link.getAttribute('aria-label')).toBeNull()
    expect(link).toHaveAccessibleName(expect.stringContaining(plain('exec_reduced_queued', { ...EXEC, queued: 5 })))
    expect([...link.querySelectorAll('.sr-only')].map(n => n.textContent)).toContain(hint)
    // A visible cue that the chip opens something.
    expect(link.querySelector('svg.lucide-chevron-right')).not.toBeNull()
    // The live region names the cut without the queued count, which moves on
    // every read and would otherwise be re-announced each time.
    expect(screen.getByRole('status').textContent).toBe(plain('exec_reduced', EXEC))
  })

  it('steps down to the lead and the cap, then the cap alone, as the header row narrows', async () => {
    answer(execCut)
    renderWithProviders(<ConcurrencyCutChip />)
    const link = await findChip()
    const sentence = link.querySelector('[data-chip-part="sentence"]')!
    const short = link.querySelector('[data-chip-part="short"]')!
    expect(sentence.className).toContain('hidden @[36rem]/chathead:inline')
    expect(short.className).toContain('@[36rem]/chathead:hidden')
    expect(link.querySelector('[data-chip-part="short-lead"]')?.className).toContain('hidden @[28rem]/chathead:inline')
    expect(link.querySelector('[data-chip-part="short-cap"]')?.textContent).toBe('2/8')
    // The narrow forms are drawn for the eye only; the sentence stays readable.
    expect(short.querySelector('[aria-hidden]')).not.toBeNull()
    expect(short.querySelector('.sr-only')?.textContent).toBe(plain('exec_reduced_queued', { ...EXEC, queued: 5 }))
  })

  it('steps down sooner on a row the channel-link chip also holds', async () => {
    answer(execCut)
    renderWithProviders(<ConcurrencyCutChip crowded />)
    const link = await findChip()
    expect(link.querySelector('[data-chip-part="sentence"]')?.className).toContain('hidden @[48rem]/chathead:inline')
    expect(link.querySelector('[data-chip-part="short-lead"]')?.className).toContain('hidden @[40rem]/chathead:inline')
    // Narrowest of all, the gauge alone: the sentence stays the link's name.
    expect(link.querySelector('[data-chip-part="short-cap"]')?.className).toContain('hidden @[32rem]/chathead:inline')
    expect(link.querySelector('svg.lucide-chevron-right')?.getAttribute('class')).toContain('hidden @[32rem]/chathead:inline')
    expect(link).toHaveAccessibleName(expect.stringContaining(plain('exec_reduced_queued', { ...EXEC, queued: 5 })))
  })

  it('keeps the whole sentence at every width when there is no cap to shorten to', async () => {
    answer({ ...gateOnly, gate: { ...gateOnly.gate!, paused: true } })
    renderWithProviders(<ConcurrencyCutChip />)
    const link = await findChip()
    expect(link.querySelector('[data-chip-part="short"]')).toBeNull()
    expect(link.querySelector('[data-chip-part="sentence"]')?.className).not.toContain('hidden')
  })

  describe('a failed read', () => {
    beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }) })
    afterEach(() => { vi.useRealTimers() })

    it('is reported once its retry fails too, with no cut on screen', async () => {
      spawnAdaptive.mockRejectedValue(new Error('adaptive read failed'))
      renderWithProviders(<ConcurrencyCutChip />, { store: createConnectedTestStore() })
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(1))
      // One miss is not reported: a gateway restart would flash it everywhere.
      expect(screen.queryByTestId('concurrency-cut-error')).toBeNull()
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_RETRY_MS))
      const notice = await screen.findByTestId('concurrency-cut-error')
      expect(spawnAdaptive).toHaveBeenCalledTimes(2)
      expect(notice.textContent).toContain(i18nT('components.concurrencyCutChip.load_failed'))
      expect(screen.queryByTestId('concurrency-cut-chip')).toBeNull()
    })

    it('replaces a cut from the last good reading once a refresh and its retry fail', async () => {
      answer(execCut)
      renderWithProviders(<ConcurrencyCutChip />, { store: createConnectedTestStore() })
      await findChip()
      spawnAdaptive.mockRejectedValue(new Error('adaptive read failed'))
      // The next refresh fails: one miss keeps the last good reading.
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_REFETCH_MS))
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(2))
      expect(screen.getByTestId('concurrency-cut-chip')).toBeTruthy()
      expect(screen.queryByTestId('concurrency-cut-error')).toBeNull()
      // Its retry fails too: the notice takes the chip's place.
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_RETRY_MS))
      const notice = await screen.findByTestId('concurrency-cut-error')
      expect(spawnAdaptive).toHaveBeenCalledTimes(3)
      expect(notice.textContent).toContain(i18nT('components.concurrencyCutChip.load_failed'))
      expect(screen.queryByTestId('concurrency-cut-chip')).toBeNull()
      expect(screen.getByRole('status').textContent).toBe('')
    })

    it('is left to the offline banner while disconnected', async () => {
      spawnAdaptive.mockRejectedValue(new Error('adaptive read failed'))
      renderWithProviders(<ConcurrencyCutChip />, { store: createTestStore() })
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(1))
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_RETRY_MS))
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(2))
      expect(screen.queryByTestId('concurrency-cut-error')).toBeNull()
    })
  })
})

describe('SessionQueuedMarker', () => {
  // The chip renders beside the marker so a test can wait for the reading to
  // land before it asserts that the marker stayed silent.
  const header = (slotKey: string) => (
    <>
      <ConcurrencyCutChip />
      <SessionQueuedMarker slotKey={slotKey} />
    </>
  )

  it('marks the session whose subagent starts wait while the execution cap is cut', async () => {
    answer(execCut)
    const store = createTestStore()
    store.dispatch(sseSubagentQueued({ slot: 's1', queued: 3 }))
    renderWithProviders(header('s1'), { store })
    const el = await screen.findByTestId('session-queued-marker')
    const hint = i18nT('components.concurrencyCutChip.session_queued_hint')
    expect(el.textContent).toContain(markerText(3))
    expect(el.getAttribute('title')).toBe(hint)
    expect(el.querySelector('.sr-only')?.textContent).toBe(hint)
    // A narrow header row folds it away; wider ones show it.
    expect(el.className).toContain('hidden')
    expect(el.className).toContain('@[44rem]/chathead:inline-flex')
    act(() => { store.dispatch(sseSubagentQueued({ slot: 's1', queued: 0 })) })
    expect(screen.queryByTestId('session-queued-marker')).toBeNull()
  })

  it('stays silent for a session with nothing queued', async () => {
    answer(execCut)
    const store = createTestStore()
    store.dispatch(sseSubagentQueued({ slot: 'other', queued: 2 }))
    renderWithProviders(header('s1'), { store })
    await findChip()
    expect(screen.queryByTestId('session-queued-marker')).toBeNull()
  })

  it('stays silent when only the spawn gate is cut', async () => {
    answer(gateOnly)
    const store = createTestStore()
    store.dispatch(sseSubagentQueued({ slot: 's1', queued: 3 }))
    renderWithProviders(header('s1'), { store })
    await findChip()
    expect(screen.queryByTestId('session-queued-marker')).toBeNull()
  })

  it('leaves a memory wait to its own wording', async () => {
    answer(execCut)
    const store = createTestStore()
    store.dispatch(sseSubagentQueued({ slot: 's1', queued: 3, reason: 'low_memory', available_gb: 1, required_gb: 2 }))
    renderWithProviders(header('s1'), { store })
    await findChip()
    expect(screen.queryByTestId('session-queued-marker')).toBeNull()
  })
})

describe('ChatHeaderChips', () => {
  const linked = (direction: SessionLink['direction']): ChatSlot => ({
    key: 's1', messages: 0, running: false,
    links: [{ channel: 'discord', label: 'Discord DM', target: '…1', direction }],
  } as ChatSlot)

  it('mounts the cut chip and the session marker in the header row', async () => {
    answer(execCut)
    const store = createTestStore()
    store.dispatch(sseSubagentQueued({ slot: 's1', queued: 2 }))
    renderWithProviders(<ChatHeaderChips slotKey="s1" />, { store })
    expect((await findChip()).getAttribute('href')).toBe(CAPACITY_CARD_PATH)
    expect((await screen.findByTestId('session-queued-marker')).textContent).toContain(markerText(2))
  })

  it('crowds the row while a two-way channel link shows its chip', async () => {
    answer(execCut)
    const store = createTestStore()
    store.dispatch(sseSlots([linked('both')]))
    store.dispatch(sseSubagentQueued({ slot: 's1', queued: 2 }))
    renderWithProviders(<ChatHeaderChips slotKey="s1" />, { store })
    const link = await findChip()
    expect(link.querySelector('[data-chip-part="sentence"]')?.className).toContain('@[48rem]/chathead:inline')
    expect((await screen.findByTestId('session-queued-marker')).className).toContain('@[64rem]/chathead:inline-flex')
  })

  it('keeps the roomy steps for a one-way link, which shows no inbound chip', async () => {
    answer(execCut)
    const store = createTestStore()
    store.dispatch(sseSlots([linked('out')]))
    renderWithProviders(<ChatHeaderChips slotKey="s1" />, { store })
    expect((await findChip()).querySelector('[data-chip-part="sentence"]')?.className).toContain('@[36rem]/chathead:inline')
  })
})

describe('phone top bar', () => {
  /** A plain item standing in for the dropdown's: what is under test is the
   *  wiring, not Radix itself. */
  const Item: CutMenuItemComponent = ({ children, onSelect, className, 'aria-describedby': describedBy }) => (
    <button type="button" className={className} aria-describedby={describedBy} onClick={() => onSelect?.(new Event('select'))}>{children}</button>
  )
  function Where() {
    const loc = useLocation()
    return <span data-testid="where">{loc.pathname + loc.search}</span>
  }

  it('names the cut as a menu row that opens the capacity card', async () => {
    answer(execCut)
    renderWithProviders(<><ConcurrencyCutMenuItem Item={Item} /><Where /></>, { route: '/chat' })
    const row = await screen.findByTestId('concurrency-cut-menu-item')
    expect(screen.getByTestId('concurrency-cut-menu-label').textContent).toBe(plain('exec_reduced_queued', { ...EXEC, queued: 5 }))
    fireEvent.click(row.closest('button')!)
    expect(screen.getByTestId('where').textContent).toBe(CAPACITY_CARD_PATH)
  })

  it('shows the reassurance a touch screen never sees as a hover title as the row\'s visible second line', async () => {
    answer(execCut)
    renderWithProviders(<ConcurrencyCutMenuItem Item={Item} />)
    const hint = await screen.findByTestId('concurrency-cut-menu-hint')
    expect(hint.textContent).toBe(i18nT('components.concurrencyCutChip.queue_kept'))
    expect(i18nT('components.concurrencyCutChip.hint')).toContain(hint.textContent!)
    // Drawn for the eye, not tucked away for screen readers only.
    expect(hint.closest('.sr-only')).toBeNull()
    expect(hint.closest('[aria-hidden]')).toBeNull()
  })

  it('adds no row while no cut is in force', async () => {
    answer({ cut: false })
    renderWithProviders(<ConcurrencyCutMenuItem Item={Item} />)
    await waitFor(() => expect(spawnAdaptive).toHaveBeenCalled())
    expect(screen.queryByTestId('concurrency-cut-menu-item')).toBeNull()
  })

  it('says the state could not be read in the menu when a read keeps failing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      spawnAdaptive.mockRejectedValue(new Error('adaptive read failed'))
      renderWithProviders(<ConcurrencyCutMenuItem Item={Item} />, { store: createConnectedTestStore() })
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(1))
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_RETRY_MS))
      expect(await screen.findByText(i18nT('components.concurrencyCutChip.load_failed'))).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('replaces the cut row in the menu once a refresh and its retry fail', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      answer(execCut)
      renderWithProviders(<><ConcurrencyCutMenuItem Item={Item} /><ConcurrencyCutDot /></>, { store: createConnectedTestStore() })
      await screen.findByTestId('concurrency-cut-menu-item')
      expect(screen.getByTestId('concurrency-cut-dot').getAttribute('data-tone')).toBe('cut')
      spawnAdaptive.mockRejectedValue(new Error('adaptive read failed'))
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_REFETCH_MS))
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(2))
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_RETRY_MS))
      expect(await screen.findByText(i18nT('components.concurrencyCutChip.load_failed'))).toBeTruthy()
      expect(spawnAdaptive).toHaveBeenCalledTimes(3)
      expect(screen.queryByTestId('concurrency-cut-menu-item')).toBeNull()
      // The trigger's dot turns from the cut's warning tone to the error tone.
      const dot = screen.getByTestId('concurrency-cut-dot')
      expect(dot.getAttribute('data-tone')).toBe('error')
      expect(dot.className).toContain('bg-danger')
      expect(dot.className).not.toContain('bg-warn')
      // The hand-off stays a sibling menu item, so it joins the menu's focus.
      expect(document.querySelector('[aria-describedby="concurrency-cut-menu-error"]')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('marks the menu trigger with an error-toned dot when every read fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      spawnAdaptive.mockRejectedValue(new Error('adaptive read failed'))
      renderWithProviders(<ConcurrencyCutDot />, { store: createConnectedTestStore() })
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(1))
      // One miss is not reported, so it marks nothing yet.
      expect(screen.queryByTestId('concurrency-cut-dot')).toBeNull()
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_RETRY_MS))
      const dot = await screen.findByTestId('concurrency-cut-dot')
      expect(dot.getAttribute('data-tone')).toBe('error')
      expect(dot.className).toContain('bg-danger')
      expect(dot.getAttribute('aria-hidden')).not.toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves the trigger unmarked for a failed read while disconnected', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      spawnAdaptive.mockRejectedValue(new Error('adaptive read failed'))
      renderWithProviders(<ConcurrencyCutDot />, { store: createTestStore() })
      await act(() => vi.advanceTimersByTimeAsync(THROTTLE_RETRY_MS))
      await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(2))
      expect(screen.queryByTestId('concurrency-cut-dot')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('marks the menu trigger with a warning-toned dot only while a cut is in force', async () => {
    answer(execCut)
    const { unmount } = renderWithProviders(<ConcurrencyCutDot />)
    const dot = await screen.findByTestId('concurrency-cut-dot')
    expect(dot.getAttribute('aria-hidden')).not.toBeNull()
    expect(dot.getAttribute('data-tone')).toBe('cut')
    expect(dot.className).toContain('bg-warn')
    unmount()
    answer({ cut: false })
    renderWithProviders(<ConcurrencyCutDot />)
    await waitFor(() => expect(spawnAdaptive).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('concurrency-cut-dot')).toBeNull()
  })
})
