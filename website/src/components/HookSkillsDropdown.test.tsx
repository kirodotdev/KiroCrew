import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
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

function createMockAnchorRef(rect?: Partial<DOMRect>) {
  const el = document.createElement('button')
  const r = { top: 0, left: 0, bottom: 32, right: 100, width: 100, height: 32, x: 0, y: 0, ...rect }
  el.getBoundingClientRect = () => ({ ...r, toJSON: () => ({}) }) as DOMRect
  document.body.appendChild(el)
  // Expose a setter so a test can move the trigger and fire scroll/resize.
  return { ref: { current: el }, move: (next: Partial<DOMRect>) => Object.assign(r, next), el }
}

describe('HookSkillsDropdown', () => {
  const baseProps = {
    anchorRef: createMockAnchorRef().ref,
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

  it('positions the menu from the trigger rect', () => {
    const { ref } = createMockAnchorRef({ top: 100, left: 40, bottom: 132 })
    renderWithProviders(<HookSkillsDropdown {...baseProps} anchorRef={ref} />)
    const menu = screen.getByPlaceholderText(/filter/i).closest('div.fixed') as HTMLElement
    expect(menu.style.top).toBe('136px') // bottom(132) + 4
    expect(menu.style.left).toBe('40px')
  })

  it('repositions the menu when the trigger moves on scroll', () => {
    const { ref, move } = createMockAnchorRef({ top: 100, left: 40, bottom: 132 })
    renderWithProviders(<HookSkillsDropdown {...baseProps} anchorRef={ref} />)
    const menu = screen.getByPlaceholderText(/filter/i).closest('div.fixed') as HTMLElement
    expect(menu.style.top).toBe('136px')
    // Simulate a scroll that shifts the trigger up by 50px.
    move({ top: 50, bottom: 82, left: 40 })
    act(() => { window.dispatchEvent(new Event('scroll')) })
    expect(menu.style.top).toBe('86px')
    expect(menu.style.left).toBe('40px')
  })

  it('repositions the menu on resize', () => {
    const { ref, move } = createMockAnchorRef({ left: 40, bottom: 132 })
    renderWithProviders(<HookSkillsDropdown {...baseProps} anchorRef={ref} />)
    const menu = screen.getByPlaceholderText(/filter/i).closest('div.fixed') as HTMLElement
    move({ left: 200, bottom: 132 })
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(menu.style.left).toBe('200px')
  })

  it('repositions the menu when selected chips move the trigger', () => {
    const { ref, move } = createMockAnchorRef({ left: 40, bottom: 132 })
    const { rerender } = renderWithProviders(
      <HookSkillsDropdown {...baseProps} anchorRef={ref} />,
    )
    const menu = screen.getByPlaceholderText(/filter/i).closest('div.fixed') as HTMLElement

    move({ left: 40, bottom: 180 })
    rerender(<HookSkillsDropdown {...baseProps} anchorRef={ref} selected={[]} />)

    expect(menu.style.top).toBe('184px')
  })

  it('does NOT restore focus to a trigger detached from the document on Escape', () => {
    const { ref, el } = createMockAnchorRef()
    const focusSpy = vi.spyOn(el, 'focus')
    renderWithProviders(<HookSkillsDropdown {...baseProps} anchorRef={ref} />)
    // Detach the trigger while the menu is open (e.g. the form unmounted).
    el.remove()
    const menu = screen.getByPlaceholderText(/filter/i).closest('div')!
    fireEvent.keyDown(menu, { key: 'Escape' })
    expect(baseProps.onClose).toHaveBeenCalled()
    expect(focusSpy).not.toHaveBeenCalled()
  })

  it('does NOT restore focus to a hidden trigger (offsetParent null) on Escape', () => {
    const { ref, el } = createMockAnchorRef()
    // happy-dom reports offsetParent as null unless laid out; a real hidden
    // element does too. Pin it explicitly so the guard is exercised.
    Object.defineProperty(el, 'offsetParent', { configurable: true, get: () => null })
    const focusSpy = vi.spyOn(el, 'focus')
    renderWithProviders(<HookSkillsDropdown {...baseProps} anchorRef={ref} />)
    const menu = screen.getByPlaceholderText(/filter/i).closest('div')!
    fireEvent.keyDown(menu, { key: 'Escape' })
    expect(baseProps.onClose).toHaveBeenCalled()
    expect(focusSpy).not.toHaveBeenCalled()
  })

  it('restores focus to a connected, focusable trigger on Escape', () => {
    const { ref, el } = createMockAnchorRef()
    Object.defineProperty(el, 'offsetParent', { configurable: true, get: () => document.body })
    const focusSpy = vi.spyOn(el, 'focus')
    renderWithProviders(<HookSkillsDropdown {...baseProps} anchorRef={ref} />)
    const menu = screen.getByPlaceholderText(/filter/i).closest('div')!
    fireEvent.keyDown(menu, { key: 'Escape' })
    expect(focusSpy).toHaveBeenCalled()
  })

  it('removes scroll and resize listeners on unmount', () => {
    const { ref } = createMockAnchorRef()
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const { unmount } = renderWithProviders(<HookSkillsDropdown {...baseProps} anchorRef={ref} />)
    unmount()
    const events = removeSpy.mock.calls.map(c => c[0])
    expect(events).toContain('scroll')
    expect(events).toContain('resize')
    removeSpy.mockRestore()
  })
})
