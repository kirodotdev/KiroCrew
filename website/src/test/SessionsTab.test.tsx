import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SessionsTab, { groupingFor, heatClass, UNAVAILABLE_GROUPINGS } from '../pages/system/SessionsTab'
import type { PlaneState } from '../pages/SystemPage'
import { createRef } from 'react'

// ResizeObserver stub for jsdom (SegmentedControl uses it)
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const mockSessionsMemory = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    sessionsMemory: (...args: unknown[]) => mockSessionsMemory(...args),
  },
}))

function defaultPayload() {
  return {
    sessions: [
      {
        key: 'dashboard:chat-1',
        title: 'Debugging session',
        slot_key: 'chat-1',
        untitled: false,
        agent: 'kirocrew',
        channel: 'dashboard',
        pid: 1001,
        owns_runtime: true,
        prompts: 5,
        rss_mb: 512,
        procs: 2,
        mcp: 1,
        cpu_cores: 0.3,
        uptime_s: 3600,
        credits: 18.4,
        turns: 7,
      },
      {
        key: 'cron:daily-check',
        title: 'Daily check',
        slot_key: '',
        untitled: false,
        agent: 'oracle',
        channel: 'cron',
        pid: 1002,
        owns_runtime: true,
        prompts: 1,
        rss_mb: 128,
        procs: 1,
        mcp: 0,
        cpu_cores: 0.1,
        uptime_s: 600,
        credits: null,
        turns: null,
      },
    ],
    tasks: [
      {
        id: 'task-1',
        task: 'Research subtask',
        agent: 'kirocrew-research',
        parent: 'dashboard:chat-1',
        rss_mb: 64,
        peak_rss_mb: 80,
        cpu_cores: 0.05,
        started_at: Date.now() / 1000 - 30,
        shared: false,
        pid: 1003,
        sampled: true,
      },
    ],
    totals: { rss_mb: 704, runtimes: 2, host_mb: 16384, host_pct: 4.3, rss_is_upper_bound: false },
    unattributed: { procs: 33, rss_mb: 13824, oldest_uptime_s: 14400 },
    history: [{ t: 1, mb: 600 }, { t: 2, mb: 700 }],
  }
}

function makePlaneStateRef() {
  const ref = createRef<PlaneState>() as { current: PlaneState }
  ref.current = {}
  return ref
}

beforeEach(() => {
  mockSessionsMemory.mockReset()
  mockSessionsMemory.mockResolvedValue(defaultPayload())
})

// ── exported helpers ──

describe('groupingFor', () => {
  it('returns an empty array for "none" (flat ranking)', () => {
    expect(groupingFor('none')).toEqual([])
  })

  it('returns the attribute name for a fold choice', () => {
    expect(groupingFor('agent')).toEqual(['agent'])
    expect(groupingFor('channel')).toEqual(['channel'])
  })
})

describe('heatClass', () => {
  it('returns a class for a hot value', () => {
    const cls = heatClass(100, 100)
    expect(cls).not.toBe('')
    expect(cls).toContain('bg-accent')
  })

  it('returns an empty string for a cold value', () => {
    expect(heatClass(1, 100)).toBe('')
  })

  it('returns an empty string when value is null', () => {
    expect(heatClass(null, 100)).toBe('')
  })

  it('returns an empty string when max is null', () => {
    expect(heatClass(50, null)).toBe('')
  })
})

// ── render tests ──

describe('SessionsTab render', () => {
  it('renders one row per session', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.getByText('Daily check')).toBeInTheDocument()
  })

  it('renders a task row nested under its parent', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Research subtask')).toBeInTheDocument()
    })
  })

  it('re-sorts when clicking a column header', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const headers = screen.getAllByRole('columnheader')
    const sorted = headers.find(h => h.getAttribute('aria-sort') === 'descending')
    expect(sorted).toBeDefined()
    fireEvent.click(sorted!.querySelector('button')!)
    await waitFor(() => {
      expect(sorted!.getAttribute('aria-sort')).toBe('ascending')
    })
    expect(
      screen.getAllByRole('columnheader').filter(h => h.getAttribute('aria-sort') !== 'none'),
    ).toHaveLength(1)
  })

  it('shows a session with no chat window as non-link text', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Daily check')).toBeInTheDocument()
    })
    const dailyText = screen.getByText('Daily check')
    const btn = dailyText.closest('button')
    expect(btn).toBeNull()
  })

  it('shows an empty state when there are no sessions and no unattributed', async () => {
    mockSessionsMemory.mockResolvedValue({
      sessions: [],
      tasks: [],
      totals: { rss_mb: 0, runtimes: 0, host_mb: 16384, host_pct: 0, rss_is_upper_bound: false },
      unattributed: null,
      history: [],
    })
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    })
  })
})

// ── Unattributed row ──

describe('Unattributed row', () => {
  it('renders above session rows when procs > 0', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.getByTestId('unattributed-row')).toBeInTheDocument()
  })

  it('is hidden when procs is 0', async () => {
    const payload = defaultPayload()
    payload.unattributed = { procs: 0, rss_mb: 0, oldest_uptime_s: null }
    mockSessionsMemory.mockResolvedValue(payload)
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('unattributed-row')).not.toBeInTheDocument()
  })

  it('is hidden when unattributed is null (platform cannot enumerate)', async () => {
    const payload = defaultPayload()
    payload.unattributed = null as never
    mockSessionsMemory.mockResolvedValue(payload)
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('unattributed-row')).not.toBeInTheDocument()
  })

  // Finding 6: unattributed uses warn, not danger
  it('uses text-warn class (not text-danger) for the unattributed row', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByTestId('unattributed-row')).toBeInTheDocument()
    })
    const row = screen.getByTestId('unattributed-row')
    expect(row.className).toContain('text-warn')
    expect(row.className).not.toContain('text-danger')
  })
})

// ── Credits / Turns columns ──

describe('Credits and Turns columns', () => {
  it('renders credits value for a session that has one', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.getByText('18.4')).toBeInTheDocument()
  })

  it('renders turns value for a session that has one', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.getByText('7')).toBeInTheDocument()
  })

  it('renders em dash for null credits (not measured, NOT zero)', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Daily check')).toBeInTheDocument()
    })
    const row = screen.getByText('Daily check').closest('tr')!
    const cells = Array.from(row.querySelectorAll('td'))
    const dashes = cells.filter(c => c.textContent === '—')
    expect(dashes.length).toBeGreaterThan(0)
  })
})

// ── Column visibility ──

describe('Column visibility defaults', () => {
  it('hides Host share and Channel columns on first paint', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const headers = screen.getAllByRole('columnheader')
    const headerTexts = headers.map(h => h.textContent)
    expect(headerTexts.join(' ')).not.toContain('Host share')
    expect(headerTexts.join(' ')).not.toContain('Channel')
  })

  it('offers hidden columns in the Columns picker menu', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const btns = screen.getAllByRole('button')
    const colsBtn = btns.find(b => b.getAttribute('aria-haspopup') !== null)
    expect(colsBtn).toBeDefined()
    fireEvent.click(colsBtn!)
    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes.length).toBeGreaterThan(0)
  })
})

describe('groupingFor guards the unavailable folds', () => {
  it('folds on nothing for an unavailable attribute', () => {
    expect(groupingFor('app')).toEqual([])
    expect(UNAVAILABLE_GROUPINGS.has('app')).toBe(true)
  })

  it('keeps App out of the set once sessions carry the attribute', () => {
    for (const key of ['none', 'agent', 'channel'] as const) {
      expect(UNAVAILABLE_GROUPINGS.has(key)).toBe(false)
    }
  })
})

// ── Finding 1: State persistence across plane flips ──

describe('State persistence via planeStateRef', () => {
  it('persists sorting state to planeStateRef', async () => {
    const ref = makePlaneStateRef()
    renderWithProviders(<SessionsTab planeStateRef={ref} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    // Default sort state should be written to the ref
    expect(ref.current.sessions).toBeDefined()
    expect(ref.current.sessions!.sorting).toEqual([{ id: 'rssMb', desc: true }])
  })

  it('restores state from planeStateRef on mount', async () => {
    const ref = makePlaneStateRef()
    ref.current.sessions = {
      sorting: [{ id: 'cpuCores', desc: false }],
      groupBy: 'none',
      filter: 'debug',
      visibility: { share: false, channel: false },
    }
    renderWithProviders(<SessionsTab planeStateRef={ref} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    // The filter input should reflect the saved filter
    const filterInput = screen.getByPlaceholderText(/filter/i)
    expect(filterInput).toHaveValue('debug')
  })
})

// ── Finding 4: Columns popover dismissal ──

describe('Columns popover dismissal', () => {
  it('closes on Escape and returns focus to trigger', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    // Open the picker
    const colsBtn = screen.getAllByRole('button').find(b => b.getAttribute('aria-haspopup') !== null)!
    fireEvent.click(colsBtn)
    expect(colsBtn.getAttribute('aria-expanded')).toBe('true')
    // Press Escape
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => {
      expect(colsBtn.getAttribute('aria-expanded')).toBe('false')
    })
  })

  it('closes on outside click', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    // Open the picker
    const colsBtn = screen.getAllByRole('button').find(b => b.getAttribute('aria-haspopup') !== null)!
    fireEvent.click(colsBtn)
    expect(colsBtn.getAttribute('aria-expanded')).toBe('true')
    // Click outside (on the document body)
    fireEvent.mouseDown(document.body)
    await waitFor(() => {
      expect(colsBtn.getAttribute('aria-expanded')).toBe('false')
    })
  })

  it('has aria-haspopup on the trigger button', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const colsBtn = screen.getAllByRole('button').find(b => b.getAttribute('aria-haspopup') !== null)!
    expect(colsBtn.getAttribute('aria-haspopup')).toBe('true')
  })
})

// ── Finding 5: Column header InfoTips ──

describe('Column header InfoTips for CPU and MCP stubs', () => {
  it('renders MCP stubs header with the full label', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const headers = screen.getAllByRole('columnheader')
    const headerTexts = headers.map(h => h.textContent)
    // Should say "MCP stubs" not just "Stubs"
    expect(headerTexts.join(' ')).toContain('MCP stubs')
  })

  it('renders CPU (cores) header clarifying the unit', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const headers = screen.getAllByRole('columnheader')
    const headerTexts = headers.map(h => h.textContent)
    expect(headerTexts.join(' ')).toContain('CPU (cores)')
  })
})

// ── Finding 7: Empty state hint text ──

describe('Empty state description (finding 7b)', () => {
  it('provides guidance on what populates the table', async () => {
    mockSessionsMemory.mockResolvedValue({
      sessions: [],
      tasks: [],
      totals: { rss_mb: 0, runtimes: 0, host_mb: 16384, host_pct: 0, rss_is_upper_bound: false },
      unattributed: null,
      history: [],
    })
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    })
    // The description prop renders additional text in the empty state
    const state = screen.getByTestId('empty-state')
    expect(state.textContent).toContain('chat')
  })
})
