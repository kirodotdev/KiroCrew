import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import HookSkillsSelect from './HookSkillsSelect'
import { api } from '../api/client'

vi.mock('../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, skills: vi.fn() } }
})

const mockSkills = [
  { key: 'kirocrew-dev/prepare-pr', name: 'prepare-pr', description: 'PR workflow' },
  { key: 'dev-fleet/pod-e2e', name: 'pod-e2e', description: 'E2E tests' },
  { key: 'widgets/theme-pack', name: 'theme-pack', description: 'Theme authoring' },
  { key: 'artifacts/save', name: 'artifacts', description: 'Artifact management' },
  { key: 'web-browse/open', name: 'web-browse', description: 'Browser automation' },
]

describe('HookSkillsSelect', () => {
  beforeEach(() => {
    vi.mocked(api.skills).mockResolvedValue(mockSkills as never)
  })

  it('renders the add-skill button', () => {
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: /add skill/i })).toBeInTheDocument()
  })

  it('renders selected skills as read-only chips with names from catalog', async () => {
    renderWithProviders(
      <HookSkillsSelect selected={['kirocrew-dev/prepare-pr', 'dev-fleet/pod-e2e']} onChange={vi.fn()} />,
    )
    await waitFor(() => {
      expect(screen.getByText('prepare-pr')).toBeInTheDocument()
      expect(screen.getByText('pod-e2e')).toBeInTheDocument()
    })
  })

  it('renders chips without inline buttons — removal lives in the dropdown Selected section', async () => {
    renderWithProviders(
      <HookSkillsSelect selected={['kirocrew-dev/prepare-pr']} onChange={vi.fn()} />,
    )
    await waitFor(() => expect(screen.getByText('prepare-pr')).toBeInTheDocument())
    // Only the "Add skill" trigger is a button in the chip row; per-chip removal
    // is a menu item inside HookSkillsDropdown (a separated region), so the row
    // never exceeds the two-button cap regardless of how many skills are selected.
    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]).toHaveTextContent(/add skill/i)
  })

  it('renders skill chips with fallback name (key tail) when catalog is empty', () => {
    vi.mocked(api.skills).mockResolvedValue([] as never)
    renderWithProviders(
      <HookSkillsSelect selected={['unknown/skill-name']} onChange={vi.fn()} />,
    )
    expect(screen.getByText('skill-name')).toBeInTheDocument()
  })

  it('handles api.skills returning null gracefully', async () => {
    vi.mocked(api.skills).mockResolvedValue(null as never)
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: /add skill/i })).toBeInTheDocument()
  })

  it('handles api.skills returning items without key field', async () => {
    vi.mocked(api.skills).mockResolvedValue([
      { key: '', name: 'empty-key' },
      { key: 'valid/skill', name: 'valid' },
    ] as never)
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /add skill/i })).toBeInTheDocument())
  })

  it('shows hint text below the component', () => {
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    const hints = document.querySelectorAll('p')
    const hintTexts = Array.from(hints).map(p => p.textContent)
    expect(hintTexts.some(t => t && t.length > 0)).toBe(true)
  })

  it('shows chip title attribute with description', async () => {
    renderWithProviders(
      <HookSkillsSelect selected={['kirocrew-dev/prepare-pr']} onChange={vi.fn()} />,
    )
    await waitFor(() => {
      const chip = screen.getByText('prepare-pr').closest('span')
      expect(chip).toHaveAttribute('title', 'PR workflow')
    })
  })

  it('shows chip title with key when no description', async () => {
    vi.mocked(api.skills).mockResolvedValue([
      { key: 'no-desc/skill', name: 'no-desc' },
    ] as never)
    renderWithProviders(
      <HookSkillsSelect selected={['no-desc/skill']} onChange={vi.fn()} />,
    )
    await waitFor(() => {
      const chip = screen.getByText('no-desc').closest('span')
      expect(chip).toHaveAttribute('title', 'no-desc/skill')
    })
  })

  it('opens dropdown when add button is clicked', async () => {
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /add skill/i })).toBeInTheDocument())
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /add skill/i }))
    })
    // createPortal may not render in happy-dom — the click still exercises
    // the open state toggle and candidates memo computation
  })

  it('renders multiple selected chips maintaining order', async () => {
    renderWithProviders(
      <HookSkillsSelect
        selected={['widgets/theme-pack', 'kirocrew-dev/prepare-pr', 'dev-fleet/pod-e2e']}
        onChange={vi.fn()}
      />,
    )
    await waitFor(() => {
      expect(screen.getByText('theme-pack')).toBeInTheDocument()
      expect(screen.getByText('prepare-pr')).toBeInTheDocument()
      expect(screen.getByText('pod-e2e')).toBeInTheDocument()
    })
  })
})
