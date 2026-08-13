import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import HookSkillsDropdown from './HookSkillsDropdown'
import React from 'react'

// Mock createPortal to render children inline (happy-dom doesn't support portals)
vi.mock('react-dom', async (importOriginal) => {
  const mod = await importOriginal<typeof import('react-dom')>()
  return { ...mod, createPortal: (children: React.ReactNode) => children }
})

const mockSkills = [
  { key: 'kirocrew-dev/prepare-pr', name: 'prepare-pr', description: 'PR workflow' },
  { key: 'dev-fleet/pod-e2e', name: 'pod-e2e', description: 'E2E tests' },
]

const mockByKey = new Map(mockSkills.map(s => [s.key, s]))

function createMockAnchorRef() {
  const el = document.createElement('button')
  el.getBoundingClientRect = () => ({ top: 0, left: 0, bottom: 32, right: 100, width: 100, height: 32, x: 0, y: 0, toJSON: () => ({}) })
  document.body.appendChild(el)
  return { current: el }
}

describe('HookSkillsDropdown', () => {
  const baseProps = {
    anchorRef: createMockAnchorRef(),
    dropdownRef: { current: null } as React.RefObject<HTMLDivElement | null>,
    inputRef: { current: null } as React.RefObject<HTMLInputElement | null>,
    filter: '',
    setFilter: vi.fn(),
    onClose: vi.fn(),
    selected: ['kirocrew-dev/prepare-pr'],
    filtered: [mockSkills[1]],
    byKey: mockByKey,
    onAdd: vi.fn(),
    onRemove: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the filter input', () => {
    renderWithProviders(<HookSkillsDropdown {...baseProps} />)
    expect(screen.getByPlaceholderText(/filter/i)).toBeInTheDocument()
  })

  it('renders selected skills with remove action', () => {
    renderWithProviders(<HookSkillsDropdown {...baseProps} />)
    expect(screen.getByRole('button', { name: /remove.*prepare-pr/i })).toBeInTheDocument()
  })

  it('calls onRemove when remove button is clicked', () => {
    renderWithProviders(<HookSkillsDropdown {...baseProps} />)
    fireEvent.click(screen.getByRole('button', { name: /remove.*prepare-pr/i }))
    expect(baseProps.onRemove).toHaveBeenCalledWith('kirocrew-dev/prepare-pr')
  })

  it('renders available candidates to add', () => {
    renderWithProviders(<HookSkillsDropdown {...baseProps} />)
    expect(screen.getByText('pod-e2e')).toBeInTheDocument()
  })

  it('calls onAdd when candidate is clicked', () => {
    renderWithProviders(<HookSkillsDropdown {...baseProps} />)
    fireEvent.click(screen.getByText('pod-e2e'))
    expect(baseProps.onAdd).toHaveBeenCalledWith('dev-fleet/pod-e2e')
  })

  it('calls onClose on Escape key', () => {
    renderWithProviders(<HookSkillsDropdown {...baseProps} />)
    const dropdown = screen.getByPlaceholderText(/filter/i).closest('div')!
    fireEvent.keyDown(dropdown, { key: 'Escape' })
    expect(baseProps.onClose).toHaveBeenCalled()
  })

  it('shows no-matching message when both selected and filtered are empty', () => {
    renderWithProviders(
      <HookSkillsDropdown {...baseProps} selected={[]} filtered={[]} />,
    )
    expect(screen.getByText(/no matching/i)).toBeInTheDocument()
  })

  it('calls setFilter when input value changes', () => {
    renderWithProviders(<HookSkillsDropdown {...baseProps} />)
    fireEvent.change(screen.getByPlaceholderText(/filter/i), { target: { value: 'pod' } })
    expect(baseProps.setFilter).toHaveBeenCalledWith('pod')
  })

  it('returns null when anchorRef.current is null', () => {
    const { container } = renderWithProviders(
      <HookSkillsDropdown {...baseProps} anchorRef={{ current: null }} />,
    )
    expect(container.innerHTML).toBe('')
  })
})
