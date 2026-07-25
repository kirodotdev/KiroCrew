import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import StyledSelect from '../components/StyledSelect'

// 16 themes — same order as the Color Theme dropdown that triggered the bug report.
// Threshold for the filter input is options.length > 5, so 6+ exercises the filter path.
const MANY_OPTIONS = [
  'emerald', 'monokai', 'solarized', 'amber', 'dracula', 'nord',
  'rosepine', 'catppuccin', 'tokyonight', 'gruvbox', 'ice', 'amoled',
  'kiro', 'intellij', 'highcontrast', 'everforest',
]
const FEW_OPTIONS = ['emerald', 'monokai', 'solarized']

describe('StyledSelect', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    delete (window as unknown as { matchMedia?: typeof window.matchMedia }).matchMedia
  })

  /** Stub matchMedia for the three device classes our touch-detection covers. */
  function mockPointerKind(kind: 'touch' | 'mouse' | 'hover-none-only') {
    const matches = (q: string) => {
      if (kind === 'touch') return /pointer:\s*coarse|hover:\s*none/.test(q)
      if (kind === 'hover-none-only') return /hover:\s*none/.test(q)
      return /pointer:\s*fine|hover:\s*hover/.test(q)
    }
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: matches(query),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }))
  }

  function openDropdown(value = 'emerald', options = MANY_OPTIONS) {
    const onChange = vi.fn()
    render(<StyledSelect value={value} options={options} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    return { onChange }
  }

  it('renders trigger with current value', () => {
    render(<StyledSelect value="emerald" options={MANY_OPTIONS} onChange={() => {}} />)
    expect(screen.getByText('emerald')).toBeInTheDocument()
  })

  it('opens listbox on trigger click', () => {
    openDropdown()
    expect(screen.getByRole('listbox')).toBeInTheDocument()
  })

  it('shows filter input when more than 5 options exist', () => {
    openDropdown()
    expect(screen.getByPlaceholderText('Filter…')).toBeInTheDocument()
  })

  it('omits filter input when 5 or fewer options exist', () => {
    openDropdown('emerald', FEW_OPTIONS)
    expect(screen.queryByPlaceholderText('Filter…')).not.toBeInTheDocument()
  })

  describe('mobile dropdown bug — scrolling should remain possible', () => {
    // On touch, the dropdown's auto-focused filter input pops the on-screen
    // keyboard, which fires `window.resize` and closes the dropdown before the
    // user can drag-scroll past the first ~5 options. Pin the contract:
    //   - Touch: no auto-focus on open; resize-with-focus-inside stays open.
    //   - Desktop: auto-focus on open; resize always closes.

    it('does NOT auto-focus the filter input on touch devices', async () => {
      mockPointerKind('touch')
      openDropdown()
      // useFilteredDropdown schedules focus via setTimeout(..., 0); flush it.
      await act(() => new Promise(r => setTimeout(r, 5)))
      const filter = screen.getByPlaceholderText('Filter…')
      expect(document.activeElement).not.toBe(filter)
    })

    it('still auto-focuses the filter input on mouse/keyboard devices', async () => {
      mockPointerKind('mouse')
      openDropdown()
      await act(() => new Promise(r => setTimeout(r, 5)))
      const filter = screen.getByPlaceholderText('Filter…')
      expect(document.activeElement).toBe(filter)
    })

    it('stays open on window resize on touch devices when focus is inside the dropdown', () => {
      mockPointerKind('touch')
      openDropdown()
      expect(screen.getByRole('listbox')).toBeInTheDocument()
      const filter = screen.getByPlaceholderText('Filter…') as HTMLInputElement
      filter.focus()
      fireEvent(window, new Event('resize'))
      expect(screen.queryByRole('listbox')).toBeInTheDocument()
    })

    it('still closes on window resize on desktop even when focus is inside the dropdown', () => {
      // A real layout change must still close the dropdown so it doesn't float
      // at stale coordinates.
      mockPointerKind('mouse')
      openDropdown()
      const filter = screen.getByPlaceholderText('Filter…') as HTMLInputElement
      filter.focus()
      fireEvent(window, new Event('resize'))
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    })

    it('still closes on window resize when focus is outside the dropdown', () => {
      mockPointerKind('mouse')
      openDropdown()
      ;(document.body as HTMLElement).focus()
      fireEvent(window, new Event('resize'))
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    })

    it('treats hover-none-only devices (stylus / accessibility) as touch — auto-focus skipped', async () => {
      mockPointerKind('hover-none-only')
      openDropdown()
      await act(() => new Promise(r => setTimeout(r, 5)))
      const filter = screen.getByPlaceholderText('Filter…')
      expect(document.activeElement).not.toBe(filter)
    })

    it('treats hover-none-only devices as touch — resize keeps dropdown open with focus inside', () => {
      mockPointerKind('hover-none-only')
      openDropdown()
      const filter = screen.getByPlaceholderText('Filter…') as HTMLInputElement
      filter.focus()
      fireEvent(window, new Event('resize'))
      expect(screen.queryByRole('listbox')).toBeInTheDocument()
    })
  })

  describe('regression — already-fixed behaviors that must keep passing', () => {
    it('does NOT close when scrolling inside the dropdown', () => {
      mockPointerKind('mouse')
      openDropdown()
      const listbox = screen.getByRole('listbox')
      const scrollEvent = new Event('scroll', { bubbles: true })
      listbox.dispatchEvent(scrollEvent)
      expect(screen.queryByRole('listbox')).toBeInTheDocument()
    })

    it('closes when the page scrolls outside the dropdown', () => {
      mockPointerKind('mouse')
      openDropdown()
      fireEvent.scroll(document.body)
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    })
  })
})
