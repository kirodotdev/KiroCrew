import { describe, it, expect, vi, beforeEach } from 'vitest'
import { cleanup, screen, fireEvent, waitFor } from '@testing-library/react'
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
    totals: { rss_mb: 704, runtimes: 2, host_mb: 16384, host_pct: 4.3, rss_is_upper_bound: false, lineage_over_cap: false, lineage_cap: 4096 },
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

  it('shows an empty state when there are no sessions', async () => {
    mockSessionsMemory.mockResolvedValue({
      sessions: [],
      tasks: [],
      totals: { rss_mb: 0, runtimes: 0, host_mb: 16384, host_pct: 0, rss_is_upper_bound: false, lineage_over_cap: false, lineage_cap: 4096 },
      history: [],
    })
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    })
  })
})

// ── Session lineage: who opened whom ──

describe('Created-by citation', () => {
  function withLineage() {
    const payload = defaultPayload()
    const base = payload.sessions[0]
    payload.sessions.push(
      {
        ...base,
        key: 'dashboard:chat-2',
        title: 'Worker opened by debugging',
        slot_key: 'chat-2',
        pid: 1004,
        parent: { slot: 'chat-1', key: 'dashboard:chat-1' },
      },
      {
        ...base,
        key: 'dashboard:chat-3',
        title: 'Worker whose conductor closed',
        slot_key: 'chat-3',
        pid: 1005,
        parent: { slot: 'chat-99-gone', key: null },
      },
    )
    return payload
  }

  it('names the creator in visible text on a created row that is not nested under it', async () => {
    // Visible text, not a title attribute: a keyboard or touch reader never sees
    // a native tooltip, and on the top-level row whose creator is not running
    // this is the only place the citation can be read at all.
    mockSessionsMemory.mockResolvedValue(withLineage())
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Worker whose conductor closed')).toBeInTheDocument()
    })
    // The orphan names the slot its crew log cited; nothing else knows the creator.
    expect(screen.getByText('Created by chat-99-gone (not running, so shown top-level)')).toBeInTheDocument()
    // Rows start expanded, so the nested row is on screen under its creator,
    // whose expander says what it hides: that place IS the citation, so the
    // nested row does not repeat its creator's name beside its own.
    expect(screen.getByRole('button', { name: /Collapse sessions under Debugging session/ })).toBeInTheDocument()
    expect(screen.getByText('Worker opened by debugging')).toBeInTheDocument()
    expect(screen.queryByText('Created by Debugging session')).toBeNull()
    expect(screen.getAllByText(/^Created by /)).toHaveLength(1)
    // Nothing cites the tooltip: no name cell carries a "Created by" title.
    expect(document.querySelector('[title*="Created by"]')).toBeNull()
  })

  it('keeps the orphan citation under a fold, where the parent row is a group row', async () => {
    // Under Group by, a top-level row's parent row is the synthetic group row,
    // so nesting cannot be read off the row tree; the row's own `nested` flag
    // from buildTree is what decides, and the orphan still says who opened it.
    mockSessionsMemory.mockResolvedValue(withLineage())
    const ref = makePlaneStateRef()
    ref.current = { sessions: { sorting: [], groupBy: 'agent', filter: '', visibility: {} } }
    renderWithProviders(<SessionsTab planeStateRef={ref} />)
    await waitFor(() => {
      expect(screen.getByText('Worker whose conductor closed')).toBeInTheDocument()
    })
    expect(screen.getByText('Created by chat-99-gone (not running, so shown top-level)')).toBeInTheDocument()
    expect(screen.getAllByText(/^Created by /)).toHaveLength(1)
  })

  it('counts the rows a folded session hides beside its name', async () => {
    mockSessionsMemory.mockResolvedValue(withLineage())
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Worker opened by debugging')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /Collapse sessions under Debugging session/ }))
    await waitFor(() => {
      expect(screen.queryByText('Worker opened by debugging')).toBeNull()
    })
    // The worker and the task under Debugging session: two hidden rows, said
    // beside the folded name so the parent's own figures are not read as a sum.
    // The unit is in the visible text: "2 hidden rows" counts sessions and
    // tasks, which is not what the footer's "nested" (sessions only) or the
    // group-row count (direct children) counts, and a bare numeral beside
    // those reads as any of them.
    const debugging = screen.getByText('Debugging session').closest('td')!
    // ...and what they hold, bound to them in the words: the worker (512 MB)
    // and the task (64 MB), so neither the parent's own 512.0MB nor the badge
    // is read as the other.
    expect(debugging.textContent).toContain('576.0MB in 2 hidden rows')
  })

  it('draws one guide line per ancestor level, so depth reads off lines and not indent alone', async () => {
    // Sorting orders each parent's children by the sorted column, so a depth-1
    // row can render below a depth-2 one and, with indent as the only cue, read
    // as deeper still. The lines under the ancestors' expanders say what a row
    // hangs from, whatever sits above it on screen.
    const payload = withLineage()
    payload.sessions.push({
      ...payload.sessions[0],
      key: 'dashboard:chat-4',
      title: 'Worker of the worker',
      slot_key: 'chat-4',
      pid: 1006,
      parent: { slot: 'chat-2', key: 'dashboard:chat-2' },
    })
    mockSessionsMemory.mockResolvedValue(payload)
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Worker of the worker')).toBeInTheDocument()
    })
    const guides = (name: string) =>
      screen.getByText(name).closest('td')!.querySelectorAll('[data-depth-guide]').length
    expect(guides('Debugging session')).toBe(0)
    expect(guides('Worker opened by debugging')).toBe(1)
    expect(guides('Worker of the worker')).toBe(2)
    // The orphan is top-level: its citation, not a line, says who opened it.
    expect(guides('Worker whose conductor closed')).toBe(0)
  })

  it('says nothing about a creator on a session nobody created', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.queryByText(/^Created by /)).toBeNull()
  })

  it('shows no session-logs stat while the store fits the scan', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.queryByText('Stored session logs')).toBeNull()
  })

  it('says the store holds more session logs than the scan admits, as N+, when it does', async () => {
    // The backend caps how many session logs one scan reads and counts the
    // rest; a session whose log went unread shows no creator, which would read
    // exactly like one nobody created. The count is the difference.
    const payload = defaultPayload()
    mockSessionsMemory.mockResolvedValue({
      ...payload,
      totals: { ...payload.totals, lineage_over_cap: true },
    })
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Stored session logs')).toBeInTheDocument()
    })
    const value = screen.getByText('4,096+')
    expect(value).toBeInTheDocument()
    // Informational, not an alarm: the unread logs change nothing on this page,
    // so the count is not the page's warn colour. Its "?" hint leads with what
    // the count means and what to check.
    expect(value.className).not.toContain('text-warn')
    // The hint names the cap it explains ("more than 4,096 exist"), since it
    // opens away from the value and a "this many" would point at nothing.
    expect(document.querySelector('[title*="more than 4,096 exist"]')).not.toBeNull()
    expect(document.querySelector('[title*="this many"]')).toBeNull()
    // The remedy says where it is typed: a reader who does not know that
    // cannot use it.
    expect(document.querySelector('[title*="in a terminal run: kirocrew config set"]')).not.toBeNull()
  })

  it('says on the Memory header that a parent figure does not include its nested rows', async () => {
    // Nesting invites reading the parent's figure as a sum of the rows under
    // it; the column's own "?" hint is where that reading is corrected.
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(document.querySelector('[title*="does not include the sessions or tasks nested under it"]')).not.toBeNull()
    // ...and the one row kind that DOES total: a group's header under Group by.
    expect(document.querySelector('[title*="a group\'s header row totals its members"]')).not.toBeNull()
  })

  it('marks task rows with their kind, since indent alone no longer says task', async () => {
    // With created sessions nesting too, an indented row may be either kind;
    // the task rows say so in muted text before their name.
    mockSessionsMemory.mockResolvedValue(withLineage())
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Worker opened by debugging')).toBeInTheDocument()
    })
    const markers = screen.getAllByText('task', { exact: true })
    expect(markers.length).toBeGreaterThan(0)
    // A session row carries no such marker.
    const worker = screen.getByText('Worker opened by debugging').closest('td')!
    expect(worker.textContent).not.toMatch(/^task/)
  })

  it('says how many sessions are top-level once one nests under another', async () => {
    // "Sessions 3" over a table that shows two top-level rows reads as a
    // contradiction; the value reconciles the two counts only when they differ.
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    expect(screen.queryByText(/top-level/)).toBeNull()
    cleanup()

    mockSessionsMemory.mockResolvedValue(withLineage())
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Worker whose conductor closed')).toBeInTheDocument()
    })
    // Four sessions, one nested under its creator: three top-level.
    expect(screen.getByText('4 (3 top-level, 1 nested)')).toBeInTheDocument()
    // The value names the nested count itself, so "Task sessions" beside it is
    // not read as the other half of the sum.
    // ...and says outright that the task sessions are a separate count, with
    // their number, so "Task sessions" beside it is not read as part of the 4.
    expect(document.querySelector('[title*="task sessions are a separate count, not part of this one"]')).not.toBeNull()
    expect(document.querySelector('[title*="A task row is a session run as a task"]')).not.toBeNull()
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
    expect(document.activeElement).toBe(colsBtn)
  })

  it('closes on outside press', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    // Open the picker
    const colsBtn = screen.getAllByRole('button').find(b => b.getAttribute('aria-haspopup') !== null)!
    fireEvent.click(colsBtn)
    // Await the panel, then flush a macrotask: Radix attaches its outside-press
    // listener in a setTimeout(0) after open, so a synchronous press would land
    // before the listener exists and be silently ignored.
    await screen.findByRole('dialog')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(colsBtn.getAttribute('aria-expanded')).toBe('true')
    // A real outside press is pointerdown followed by click; Radix defers the
    // dismissal of a button-0 press until the click lands.
    fireEvent.pointerDown(document.body)
    fireEvent.click(document.body)
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
    expect(colsBtn.getAttribute('aria-haspopup')).toBe('dialog')
  })
})

// ── Issue 2843: focus management and scroll anchoring ──

describe('Columns popover focus and anchoring', () => {
  /** Rect helper: the trigger button anchored at a given viewport top. */
  function rectAt(top: number): DOMRect {
    return {
      x: 500, y: top, top, bottom: top + 24, left: 500, right: 580,
      width: 80, height: 24, toJSON: () => ({}),
    } as DOMRect
  }

  it('moves focus inside the dialog on open', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const colsBtn = screen.getAllByRole('button').find(b => b.getAttribute('aria-haspopup') !== null)!
    fireEvent.click(colsBtn)
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => {
      expect(dialog.contains(document.activeElement)).toBe(true)
    })
  })

  it('repositions the panel when the page scrolls while open', async () => {
    renderWithProviders(<SessionsTab planeStateRef={makePlaneStateRef()} />)
    await waitFor(() => {
      expect(screen.getByText('Debugging session')).toBeInTheDocument()
    })
    const colsBtn = screen.getAllByRole('button').find(b => b.getAttribute('aria-haspopup') !== null)!
    // Anchor the trigger at a known viewport position before opening.
    let top = 100
    colsBtn.getBoundingClientRect = () => rectAt(top)
    fireEvent.click(colsBtn)
    await screen.findByRole('dialog')
    const wrapper = document.querySelector('[data-radix-popper-content-wrapper]') as HTMLElement
    expect(wrapper).not.toBeNull()
    // Wait for the initial position pass to land.
    await waitFor(() => {
      expect(wrapper.style.transform).toContain('translate')
    })
    // Coupling note: this parses Floating UI's transform serialization
    // (translate/translate3d). Assert parseability ONCE here so a dependency
    // bump that changes the format fails loudly, not as a waitFor timeout.
    const parseY = (transform: string) => {
      const m = /translate(?:3d)?\(([^,]+),\s*(-?[\d.]+)px/.exec(transform)
      return m ? Number(m[2]) : null
    }
    const yBefore = parseY(wrapper.style.transform)
    expect(yBefore, `unparseable popper transform: ${wrapper.style.transform}`).not.toBeNull()
    // A 40px scroll moves the anchor up; the panel must follow, not float away.
    // The exact y depends on which side Floating UI placed the panel, so assert
    // the delta: it must track the anchor's 40px shift.
    top = 60
    fireEvent.scroll(window)
    await waitFor(() => {
      expect(parseY(wrapper.style.transform)).toBe(yBefore! - 40)
    })
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
      totals: { rss_mb: 0, runtimes: 0, host_mb: 16384, host_pct: 0, rss_is_upper_bound: false, lineage_over_cap: false, lineage_cap: 4096 },
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
