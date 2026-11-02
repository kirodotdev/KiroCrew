import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { HistoryTab } from './HistoryTab'
import { renderWithProviders } from '../../test/helpers'
import * as shopApi from './api'
import { fmtCurrency } from '../../i18n/format'

vi.mock('./api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
}))

const mockGet = vi.mocked(shopApi.get)
const mockPut = vi.mocked(shopApi.put)

const STAMP = '2026-08-10T12:00:00+00:00'

const SESSIONS = [
  {
    id: 'h1',
    date: '2026-08-01',
    problem: 'Need running shoes',
    advice: 'Get a neutral shoe',
    products: [{ name: 'Running Shoe', price: 25 }],
    feedback: {} as Record<string, string>,
    created_at: STAMP,
  },
  {
    id: 'h2',
    date: '2026-08-02',
    problem: 'Lamp for desk',
    advice: 'Use a timer instead',
    products: [],
    feedback: {},
    created_at: STAMP,
  },
]

function seed(sessions = SESSIONS) {
  mockGet.mockResolvedValue({ sessions } as never)
  mockPut.mockResolvedValue(undefined)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('HistoryTab', () => {
  it('shows the loading line while the history query is pending', () => {
    mockGet.mockReturnValue(new Promise(() => {}) as never)
    renderWithProviders(<HistoryTab />)
    expect(screen.getByText('Loading history...')).toBeInTheDocument()
  })

  it('shows the empty state when there are no sessions', async () => {
    seed([])
    renderWithProviders(<HistoryTab />)
    expect(await screen.findByText('No history yet')).toBeInTheDocument()
    expect(mockGet).toHaveBeenCalledWith('/history?limit=20')
  })

  it('renders each session with its price, and the advice-only note for a session with no products', async () => {
    seed()
    renderWithProviders(<HistoryTab />)
    expect(await screen.findByText('Need running shoes')).toBeInTheDocument()
    expect(screen.getByText('Running Shoe')).toBeInTheDocument()
    expect(screen.getByText(fmtCurrency(25))).toBeInTheDocument()
    expect(screen.getByText('No products recommended — advice was the solution')).toBeInTheDocument()
  })

  it('filters sessions by problem, advice or product name and reports no match', async () => {
    // The product name shares no word with the first session's problem or advice,
    // so each query below is matched by exactly one field.
    seed([{ ...SESSIONS[0], products: [{ name: 'Trail Sneaker', price: 25 }] }, SESSIONS[1]])
    renderWithProviders(<HistoryTab />)
    await screen.findByText('Lamp for desk')
    const search = screen.getByPlaceholderText('Search past sessions...')
    // advice only
    fireEvent.change(search, { target: { value: 'neutral' } })
    expect(screen.getByText('Need running shoes')).toBeInTheDocument()
    expect(screen.queryByText('Lamp for desk')).toBeNull()
    // problem only
    fireEvent.change(search, { target: { value: 'Lamp' } })
    expect(screen.queryByText('Need running shoes')).toBeNull()
    expect(screen.getByText('Lamp for desk')).toBeInTheDocument()
    // product name only
    fireEvent.change(search, { target: { value: 'trail' } })
    expect(screen.getByText('Need running shoes')).toBeInTheDocument()
    expect(screen.queryByText('Lamp for desk')).toBeNull()
    fireEvent.change(search, { target: { value: 'zzz' } })
    expect(screen.getByText('No sessions matching "zzz"')).toBeInTheDocument()
  })

  it('records a liked product and refreshes the history afterwards', async () => {
    seed()
    renderWithProviders(<HistoryTab />)
    fireEvent.click(await screen.findByRole('button', { name: 'Mark as liked' }))
    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith('/history/h1/feedback', { product: 'Running Shoe', feedback: 'liked' }),
    )
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2))
  })

  it('shows the saved feedback as a badge that reopens the choices', async () => {
    seed([{ ...SESSIONS[0], feedback: { 'Running Shoe': 'purchased' } }])
    renderWithProviders(<HistoryTab />)
    const badge = await screen.findByRole('button', { name: 'Change feedback' })
    expect(badge).toHaveTextContent('Purchased')
    fireEvent.click(badge)
    fireEvent.click(screen.getByRole('button', { name: 'Mark as skipped' }))
    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith('/history/h1/feedback', { product: 'Running Shoe', feedback: 'skipped' }),
    )
  })

  it('shows a stored feedback value it does not know as the skipped badge', async () => {
    seed([{ ...SESSIONS[0], feedback: { 'Running Shoe': 'mystery' } }])
    renderWithProviders(<HistoryTab />)
    expect(await screen.findByRole('button', { name: 'Change feedback' })).toHaveTextContent('Skipped')
  })

  it('surfaces a rejected feedback write as an error notice', async () => {
    seed()
    mockPut.mockRejectedValue(new Error('http_500'))
    renderWithProviders(<HistoryTab />)
    fireEvent.click(await screen.findByRole('button', { name: 'Mark as purchased' }))
    expect(await screen.findByText('http_500')).toBeInTheDocument()
  })
})
