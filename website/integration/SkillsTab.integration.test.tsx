import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SkillsTab from '../src/pages/overview/SkillsTab'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

// Mock framer-motion AnimatePresence to skip exit animations in jsdom.
vi.mock('framer-motion', async () => {
  const actual = await vi.importActual<typeof import('framer-motion')>('framer-motion')
  return {
    ...actual,
    AnimatePresence: ({ children }: any) => <>{children}</>,
  }
})

// Mock window.confirm for delete operations
const mockConfirm = vi.fn()
Object.defineProperty(window, 'confirm', {
  writable: true,
  value: mockConfirm,
})

/** The skills list is a master-detail layout: a list of skill rows on the
 *  left, a directory browser + edit affordances on the right.  The first
 *  skill is auto-selected on load. */
describe('SkillsTab Integration Tests', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockConfirm.mockReturnValue(true)
  })

  // ── Loading & Display ──────────────────────────────────────────────

  it('shows skeleton loading state initially', async () => {
    server.use(
      http.get('/api/skills', async () => {
        await new Promise(r => setTimeout(r, 200))
        return HttpResponse.json([])
      })
    )
    renderWithProviders(<SkillsTab />)
    const skeletons = document.querySelectorAll('.animate-pulse')
    expect(skeletons.length).toBeGreaterThan(0)
  })

  it('loads and displays skills as rows on mount', async () => {
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      const list = screen.getByRole('listbox', { name: 'Skills' })
      expect(within(list).getByText('Amazon Writing')).toBeInTheDocument()
      expect(within(list).getByText('Code Search')).toBeInTheDocument()
    })
  })

  it('displays skill row badges correctly', async () => {
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText('Amazon Writing')).toBeInTheDocument()
    })

    // amazon-writing is always-on → "auto" badge; code-search → "on-demand".
    expect(screen.getByText('auto')).toBeInTheDocument()
    expect(screen.getByText('on-demand')).toBeInTheDocument()
  })

  it('shows skill key as monospace subtitle on rows', async () => {
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText('amazon-writing')).toBeInTheDocument()
      expect(screen.getByText('code-search')).toBeInTheDocument()
    })
  })

  it('shows skill count in section header', async () => {
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText(/Skills \(2\)/)).toBeInTheDocument()
    })
  })

  // ── Detail Pane ────────────────────────────────────────────────────

  it('auto-selects the first skill and shows its frontmatter', async () => {
    renderWithProviders(<SkillsTab />)

    // amazon-writing is first → its directory browser + frontmatter strip render
    // without any click.  Triggers come from the async SKILL.md fetch, so wait
    // for that section specifically.
    await waitFor(() => {
      const strip = screen.getByTestId('frontmatter-strip')
      expect(within(strip).getByText('Triggers')).toBeInTheDocument()
    }, { timeout: 3000 })

    const strip = screen.getByTestId('frontmatter-strip')
    expect(within(strip).getByText('Description')).toBeInTheDocument()
    expect(within(strip).getByText('docs')).toBeInTheDocument()
    expect(within(strip).getByText('narrative')).toBeInTheDocument()
  })

  it('switches the detail pane when another skill row is clicked', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText('Code Search')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Code Search'))

    // Code Search has no triggers → the Triggers section is omitted, but the
    // strip still renders for the description.
    await waitFor(() => {
      expect(screen.getByTestId('frontmatter-strip')).toBeInTheDocument()
    }, { timeout: 3000 })

    const strip = screen.getByTestId('frontmatter-strip')
    expect(within(strip).queryByText('Triggers')).not.toBeInTheDocument()
  })

  it('shows Edit and Delete buttons in the detail header for kirocrew skills', async () => {
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      const editBtn = screen.getByRole('button', { name: /edit/i })
      expect(editBtn).toBeInTheDocument()
      expect(editBtn).not.toBeDisabled()
      expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument()
    }, { timeout: 3000 })
  })

  // ── Edit Skill ─────────────────────────────────────────────────────

  it('enters edit mode with structured form fields', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    const editBtn = await screen.findByRole('button', { name: /edit/i }, { timeout: 3000 })
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    await user.click(editBtn)

    // Instructions is unique to edit mode.
    await waitFor(() => {
      expect(screen.getByText('Instructions')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: /save/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument()
  })

  it('saves edited skill content', async () => {
    const updateHandler = vi.fn()
    server.use(
      http.put('/api/skills/:key', async ({ params, request }) => {
        const body = await request.json()
        updateHandler(params.key, body)
        return HttpResponse.json({ ok: true })
      })
    )

    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    const editBtn = await screen.findByRole('button', { name: /edit/i }, { timeout: 3000 })
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    await user.click(editBtn)

    await waitFor(() => expect(screen.getByRole('button', { name: /save/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(updateHandler).toHaveBeenCalledTimes(1))
    // Exits edit mode.
    await waitFor(() => expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument())
  })

  it('cancels skill editing', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    const editBtn = await screen.findByRole('button', { name: /edit/i }, { timeout: 3000 })
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    await user.click(editBtn)

    await waitFor(() => expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /cancel/i }))

    // Edit button reappears; Save is gone.
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    })
  })

  // ── Delete Skill ───────────────────────────────────────────────────

  it('deletes a skill with confirmation', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    // Select Code Search, then delete it.
    await waitFor(() => expect(screen.getByText('Code Search')).toBeInTheDocument())
    await user.click(screen.getByText('Code Search'))

    const delBtn = await screen.findByRole('button', { name: /delete/i }, { timeout: 3000 })
    await user.click(delBtn)

    expect(mockConfirm).toHaveBeenCalledWith(expect.stringContaining('code-search'))
  })

  // ── Create Skill ───────────────────────────────────────────────────

  it('shows Create New Skill button', async () => {
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      const btn = screen.getByRole('button', { name: /create new skill/i })
      expect(btn).toBeInTheDocument()
      expect(btn).not.toBeDisabled()
    })
  })

  // ── Filter & Search ────────────────────────────────────────────────

  it('filters skills by search term', async () => {
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText('Code Search')).toBeInTheDocument()
    })

    const filterInput = screen.getByPlaceholderText(/filter skills/i)
    fireEvent.change(filterInput, { target: { value: 'writing' } })

    // "Amazon Writing" appears in both the list row and the auto-selected
    // detail header, so scope the row assertion to the listbox.
    const list = screen.getByRole('listbox', { name: 'Skills' })
    expect(within(list).getByText('Amazon Writing')).toBeInTheDocument()
    expect(within(list).queryByText('Code Search')).not.toBeInTheDocument()
  })

  it('clears search filter with clear button', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText('Code Search')).toBeInTheDocument()
    })

    const list = screen.getByRole('listbox', { name: 'Skills' })
    const filterInput = screen.getByPlaceholderText(/filter skills/i)
    fireEvent.change(filterInput, { target: { value: 'writing' } })
    expect(within(list).queryByText('Code Search')).not.toBeInTheDocument()

    const clearBtn = screen.getByRole('button', { name: /clear search/i })
    await user.click(clearBtn)

    await waitFor(() => {
      expect(within(list).getByText('Code Search')).toBeInTheDocument()
    })
  })

  // ── Refresh ────────────────────────────────────────────────────────

  it('refreshes skills list', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText('Amazon Writing')).toBeInTheDocument()
    })

    const refreshBtn = screen.getByRole('button', { name: /refresh skills/i })
    await user.click(refreshBtn)

    await waitFor(() => {
      const list = screen.getByRole('listbox', { name: 'Skills' })
      expect(within(list).getByText('Amazon Writing')).toBeInTheDocument()
    })
  })

  // ── AIM Skills Section ─────────────────────────────────────────────

  it('shows AIM packages section header', async () => {
    server.use(
      http.get('/api/skills', () => {
        return HttpResponse.json([
          { name: 'amazon-writing', key: 'amazon-writing', description: 'Amazon writing guidelines', always: true, source: 'kirocrew', dir: '/path' },
          { name: 'code-search', key: 'code-search', description: 'Search code', always: false, source: 'kirocrew', dir: '/path' },
          { name: 'aim-benchmark', key: 'aim-benchmark', description: 'AIM benchmark tool', always: false, source: 'package', dir: '/path' },
        ])
      })
    )

    renderWithProviders(<SkillsTab />)

    // The section header is derived from the provider's pluginRegistryName
    // (derived from pluginRegistryName, e.g. 'Packages' → 'PACKAGES').
    //
    await waitFor(() => {
      expect(screen.getByText(/PACKAGES/)).toBeInTheDocument()
    })
    expect(screen.getByText('Aim Benchmark')).toBeInTheDocument()
  })

  it('does not show Edit/Delete for AIM skills', async () => {
    server.use(
      http.get('/api/skills', () => {
        return HttpResponse.json([
          { name: 'aim-tool', key: 'aim-tool', description: 'AIM tool', always: false, source: 'package', dir: '/path' },
        ])
      })
    )

    renderWithProviders(<SkillsTab />)

    // aim-tool auto-selected; read-only sources have no Edit/Delete.
    await waitFor(() => expect(screen.getByText('Aim Tool')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument()
  })

  // ── Empty State ────────────────────────────────────────────────────

  it('shows empty state when no skills are installed', async () => {
    server.use(
      http.get('/api/skills', () => {
        return HttpResponse.json([])
      })
    )

    renderWithProviders(<SkillsTab />)

    await waitFor(() => {
      expect(screen.getByText(/no skills yet/i)).toBeInTheDocument()
    })
  })

  // ── Raw Edit Mode Toggle ───────────────────────────────────────────

  it('toggles between structured and raw edit mode', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsTab />)

    const editBtn = await screen.findByRole('button', { name: /edit/i }, { timeout: 3000 })
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    await user.click(editBtn)

    await waitFor(() => expect(screen.getByText('Instructions')).toBeInTheDocument())

    const rawToggle = screen.getByText(/edit raw markdown/i)
    await user.click(rawToggle)

    await waitFor(() => expect(screen.getByText(/raw yaml/i)).toBeInTheDocument())

    const structuredToggle = screen.getByText(/switch to structured editor/i)
    await user.click(structuredToggle)

    await waitFor(() => expect(screen.getByText('Instructions')).toBeInTheDocument())
  })
})
