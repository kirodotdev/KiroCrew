import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, renderHook, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * The memory page's frame and its small parts: the tab a deep link opens, the
 * New memory form (fact and lesson), the phone's ⋯ sheet, and the date range
 * button with the range maths behind it.
 */
const { api } = vi.hoisted(() => ({ api: { vectorSemanticWrite: vi.fn(), createLesson: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
let phone = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => phone }))
const { default: MemoryPageShell } = await import('../pages/overview/memoryPage/MemoryPageShell')
const { DateRangeButton, rangeLabel } = await import('../pages/overview/memoryPage/MemoryDateRange')
const { dayDate, dayKey, parseRange, rangeBounds, useLocalDay } = await import('../pages/overview/memoryPage/memoryDateRanges')
const { fmtDate } = await import('../i18n/format')

type ShellProps = Parameters<typeof MemoryPageShell>[0]
function Shell(props: Partial<ShellProps>) {
  return <MemoryPageShell id="global" label="Global" switcher={<span>switcher</span>} canCreate
    memories={({ selecting }) => <p>{selecting ? 'selecting' : 'list'}</p>}
    panels={{ graph: <p>graph panel</p>, profile: <p>profile panel</p>, recovery: <p>recovery panel</p>, failures: <p>failures panel</p>, settings: <p>settings panel</p> }}
    {...props} />
}
const selectedTab = () => screen.getAllByRole('tab').find(tab => tab.getAttribute('aria-selected') === 'true')?.textContent
const local = (y: number, m: number, d: number) => new Date(y, m - 1, d).toISOString()

beforeEach(() => { vi.clearAllMocks(); phone = false })
afterEach(() => { vi.useRealTimers() })

describe('MemoryPageShell', () => {
  it('follows a new initialTab from a deep link, and keeps a clicked tab on an unrelated rerender', () => {
    const { rerender } = renderWithProviders(<Shell initialTab="memories" />)
    expect(selectedTab()).toBe('Memories')
    rerender(<Shell initialTab="failures" />)
    expect(selectedTab()).toBe('Failures')
    expect(screen.getByText('failures panel')).toBeInTheDocument()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Graph' }), { button: 0, ctrlKey: false })
    expect(selectedTab()).toBe('Graph')
    rerender(<Shell initialTab="failures" copyDisabled />)
    expect(selectedTab()).toBe('Graph')
  })

  it('keeps a fact draft guarded after switching New memory to an empty Lesson', async () => {
    const onDraftChange = vi.fn()
    renderWithProviders(<Shell onDraftChange={onDraftChange} />)
    fireEvent.click(screen.getByRole('button', { name: /New memory/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New memory' })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Memory key' }), { target: { value: 'pref.os' } })
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Lesson' }))
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: 'New memory' })).toBeInTheDocument()
  })

  it('saves a fact from New memory, shows it was saved, and keeps the draft when the write fails', async () => {
    const onCreated = vi.fn()
    const onDraftChange = vi.fn()
    api.vectorSemanticWrite.mockRejectedValueOnce(new Error('store is read-only')).mockResolvedValueOnce({})
    renderWithProviders(<Shell onCreated={onCreated} onDraftChange={onDraftChange} />)
    fireEvent.click(screen.getByRole('button', { name: /New memory/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New memory' })
    const save = within(dialog).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Memory key' }), { target: { value: ' pref.os ' } })
    fireEvent.change(within(dialog).getAllByRole('textbox').at(-1)!, { target: { value: 'linux' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    fireEvent.click(save)
    expect(await within(dialog).findByText(/store is read-only/)).toBeInTheDocument()
    expect(within(dialog).getByRole('combobox', { name: 'Memory key' })).toHaveValue(' pref.os ')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New memory' })).not.toBeInTheDocument())
    expect(api.vectorSemanticWrite).toHaveBeenLastCalledWith('pref.os', 'linux')
    expect(onCreated).toHaveBeenCalledTimes(1)
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
    expect(screen.getByRole('status')).toHaveTextContent('Saved pref.os')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('answers each lesson outcome in the form and closes on a stored lesson', async () => {
    api.createLesson
      .mockResolvedValueOnce({ outcome: 'unchanged' })
      .mockResolvedValueOnce({ outcome: 'deduped', reason: 'same rule' })
      .mockResolvedValueOnce({ outcome: 'rejected', reason: 'too vague' })
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce({ outcome: 'inserted' })
    const onCreated = vi.fn()
    renderWithProviders(<Shell onCreated={onCreated} />)
    fireEvent.click(screen.getByRole('button', { name: /New memory/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New memory' })
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Lesson' }))
    fireEvent.click(within(dialog).getByRole('radio', { name: /Tool/i }))
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'Work in a worktree' } })
    const submit = () => fireEvent.submit(dialog.ownerDocument.getElementById('memory-new')!)
    submit()
    expect(await within(dialog).findByText('This lesson is already stored.')).toBeInTheDocument()
    expect(api.createLesson).toHaveBeenLastCalledWith('Work in a worktree', 'tool')
    submit()
    expect(await within(dialog).findByText(/already covers this \(same rule\)/)).toBeInTheDocument()
    submit()
    expect(await within(dialog).findByText(/Lesson not saved \(too vague\)/)).toBeInTheDocument()
    submit()
    expect(await within(dialog).findByText(/network down/)).toBeInTheDocument()
    submit()
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog', { name: 'New memory' })).not.toBeInTheDocument()
  })

  it('puts Select and Copy in the phone ⋯ sheet, each closing the sheet as it runs', async () => {
    phone = true
    const onCopy = vi.fn()
    renderWithProviders(<Shell initialTab="graph" onCopy={onCopy} />)
    const openSheet = () => { fireEvent.click(screen.getByRole('button', { name: 'More actions' })); return screen.getByRole('dialog', { name: 'Global' }) }
    fireEvent.click(within(openSheet()).getByRole('button', { name: 'Select' }))
    expect(screen.queryByRole('dialog', { name: 'Global' })).not.toBeInTheDocument()
    expect(selectedTab()).toBe('Memories')
    expect(screen.getByText('selecting')).toBeInTheDocument()
    fireEvent.click(within(openSheet()).getByRole('button', { name: /Copy from another memory/ }))
    expect(onCopy).toHaveBeenCalledTimes(1)
    const onPageKey = vi.fn()
    document.addEventListener('keydown', onPageKey)
    fireEvent.keyDown(openSheet(), { key: 'a' })
    document.removeEventListener('keydown', onPageKey)
    expect(onPageKey).not.toHaveBeenCalled()
    fireEvent.click(screen.getAllByRole('button', { name: 'Close' }).at(-1)!)
    expect(screen.queryByRole('dialog', { name: 'Global' })).not.toBeInTheDocument()
  })
})

describe('memory date ranges', () => {
  it('turns quick and custom ranges into server bounds and reads saved ones back', () => {
    const now = new Date(2026, 10, 26, 15, 30)
    expect(dayKey(now)).toBe('2026-11-26')
    expect(dayDate('2026-02-30')).toBeNull()
    expect(dayDate('yesterday')).toBeNull()
    expect(rangeBounds(undefined)).toBeNull()
    expect(rangeBounds({ preset: 'today' }, now)).toEqual({ since: local(2026, 11, 26) })
    expect(rangeBounds({ preset: '7d' }, now)).toEqual({ since: local(2026, 11, 20) })
    expect(rangeBounds({ preset: 'custom', from: '2026-11-01', to: '2026-11-03' })).toEqual({ since: local(2026, 11, 1), until: local(2026, 11, 4) })
    expect(rangeBounds({ preset: 'custom', to: '2026-11-03' })).toEqual({ until: local(2026, 11, 4) })
    expect(rangeBounds({ preset: 'custom', from: '2026-11-05', to: '2026-11-03' })).toBeNull()
    expect(rangeBounds({ preset: 'custom' })).toBeNull()
    expect(parseRange({ preset: '30d', from: 'x' })).toEqual({ preset: '30d' })
    expect(parseRange({ preset: 'custom', from: '2026-11-01', to: 'bad' })).toEqual({ preset: 'custom', from: '2026-11-01' })
    expect(parseRange({ preset: 'custom', from: 'bad' })).toBeUndefined()
    expect(parseRange({ preset: 'week' })).toBeUndefined()
    expect(parseRange('today')).toBeUndefined()
  })

  it('moves today on at local midnight and when a hidden tab comes back', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date(2026, 10, 26, 23, 59, 30))
      const { result } = renderHook(() => useLocalDay())
      expect(result.current).toBe('2026-11-26')
      act(() => { vi.advanceTimersByTime(31_000) })
      expect(result.current).toBe('2026-11-27')
      // A sleeping laptop can miss the timer; showing the tab again checks the day.
      vi.setSystemTime(new Date(2026, 10, 29, 9, 0))
      act(() => { document.dispatchEvent(new Event('visibilitychange')) })
      expect(result.current).toBe('2026-11-29')
    } finally { vi.useRealTimers() }
  })

  it('labels each kind of range', () => {
    const first = fmtDate(new Date(2026, 10, 1))
    const third = fmtDate(new Date(2026, 10, 3))
    expect(rangeLabel({ preset: '7d' })).toBe('Last 7 days')
    expect(rangeLabel({ preset: 'custom', from: '2026-11-01', to: '2026-11-03' })).toBe(`${first} – ${third}`)
    expect(rangeLabel({ preset: 'custom', from: '2026-11-01' })).toBe(`Since ${first}`)
    expect(rangeLabel({ preset: 'custom', to: '2026-11-03' })).toBe(`Until ${third}`)
  })

  it('picks a quick range, clears it, and applies a custom range only once it is valid', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 10, 26, 12))
    const onChange = vi.fn()
    const openMenu = () => fireEvent.pointerDown(screen.getByRole('button', { name: 'Date range' }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    const { rerender } = renderWithProviders(<DateRangeButton onChange={onChange} />)
    expect(screen.getByRole('button', { name: 'Date range' })).toHaveTextContent('Any time')
    openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Last 30 days' }))
    expect(onChange).toHaveBeenLastCalledWith({ preset: '30d' })
    rerender(<DateRangeButton range={{ preset: 'custom', from: '2026-11-01' }} onChange={onChange} />)
    openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Any time' }))
    expect(onChange).toHaveBeenLastCalledWith(undefined)
    openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Custom range…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Custom range' })
    const from = within(dialog).getByLabelText('Start date')
    const to = within(dialog).getByLabelText('End date')
    expect(from).toHaveValue('2026-11-01')
    expect(to).toHaveAttribute('max', '2026-11-26')
    fireEvent.change(to, { target: { value: '2026-10-01' } })
    expect(within(dialog).getByRole('button', { name: 'Apply' })).toBeDisabled()
    fireEvent.change(to, { target: { value: '2026-11-03' } })
    fireEvent.submit(dialog.querySelector('form')!)
    expect(onChange).toHaveBeenLastCalledWith({ preset: 'custom', from: '2026-11-01', to: '2026-11-03' })
    expect(screen.queryByRole('dialog', { name: 'Custom range' })).not.toBeInTheDocument()
    openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Custom range…' }))
    fireEvent.click(within(await screen.findByRole('dialog', { name: 'Custom range' })).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog', { name: 'Custom range' })).not.toBeInTheDocument()
    expect(onChange).toHaveBeenCalledTimes(3)
  })
})
