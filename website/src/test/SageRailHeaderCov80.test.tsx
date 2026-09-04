import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SageContextValue } from '../apps/code-review-sage/context'
import type { MainView } from '../apps/code-review-sage/lib/types'

/**
 * The rail header: the app's identity row and its ONLY navigation, rendered in
 * BOTH shapes of the rail (the full-width panel and the collapsed phone bar).
 *
 * Rendered here rather than asserted over source, because the behaviour that
 * matters is a decision made at click time — what a tap on the ALREADY-ACTIVE
 * section does. `codeReviewSageRailNarrow.test.ts` pins the shell's wiring from
 * the source text (which element carries which declaration, something jsdom
 * cannot show); this file pins what the component actually does when used.
 */
const sage: Record<string, unknown> = {}

vi.mock('../apps/code-review-sage/context', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/code-review-sage/context')>()
  return { ...actual, useSage: () => sage as unknown as SageContextValue }
})

import RailHeader, {
  sectionHasList, sectionLabel,
} from '../apps/code-review-sage/components/RailHeader'

function mount(props: Parameters<typeof RailHeader>[0] = {}) {
  return render(<RailHeader {...props} />)
}

beforeEach(() => {
  Object.keys(sage).forEach(k => delete sage[k])
  Object.assign(sage, { mainView: 'reviews' as MainView, setMainView: vi.fn() })
})

const setMainView = () => sage.setMainView as ReturnType<typeof vi.fn>

describe('Code Review Sage rail header', () => {
  it('renders all three sections as named controls in both shapes', () => {
    const { unmount } = mount()
    for (const name of ['Reviews', 'Learning', 'Settings']) {
      expect(screen.getByRole('button', { name }), `${name} missing on the wide rail`).toBeTruthy()
    }
    unmount()
    mount({ narrow: true })
    for (const name of ['Reviews', 'Learning', 'Settings']) {
      expect(screen.getByRole('button', { name }), `${name} missing on the bar`).toBeTruthy()
    }
  })

  it('marks the active section for assistive tech, and only that one', () => {
    Object.assign(sage, { mainView: 'learning' as MainView })
    mount()
    expect(screen.getByRole('button', { name: 'Learning' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByRole('button', { name: 'Reviews' }).getAttribute('aria-current')).toBeNull()
  })

  it('drops the version while narrow and keeps it on the wide rail', () => {
    const { unmount } = mount()
    // Decoration, so it is the first thing to go once the row is a phone-width
    // bar competing with a back control and three touch targets.
    expect(screen.getByText('v2.0'), 'expected the version on the wide rail').toBeTruthy()
    unmount()
    mount({ narrow: true })
    expect(screen.queryByText('v2.0'), 'expected no version on the bar').toBeNull()
  })

  it('renders the shell-supplied leading control', () => {
    mount({ leading: <button type="button">Back to list</button> })
    expect(screen.getByRole('button', { name: 'Back to list' })).toBeTruthy()
  })

  it('navigates when an INACTIVE section is tapped', async () => {
    const onReselect = vi.fn()
    mount({ narrow: true, onReselect })
    await userEvent.click(screen.getByRole('button', { name: 'Learning' }))
    expect(setMainView()).toHaveBeenCalledWith('learning')
    expect(onReselect, 'a different section is navigation, not a re-tap').not.toHaveBeenCalled()
  })

  it('hands a tap on the ACTIVE section to the shell instead of re-setting the view', async () => {
    const onReselect = vi.fn()
    mount({ narrow: true, onReselect })
    await userEvent.click(screen.getByRole('button', { name: 'Reviews' }))
    // The defect this replaced: setMainView('reviews') while already on reviews
    // changes no state, so React rendered nothing and the tap was silently dead.
    expect(onReselect).toHaveBeenCalledTimes(1)
    expect(setMainView(), 'must not re-set the view it is already on').not.toHaveBeenCalled()
  })

  it('is inert on an active re-tap when the shell offers no meaning for it', async () => {
    // The wide rail passes no `onReselect`, and Settings deliberately withholds
    // it (its rail holds no list to open) — the tap must be a no-op, not a crash.
    mount()
    await userEvent.click(screen.getByRole('button', { name: 'Reviews' }))
    expect(setMainView()).not.toHaveBeenCalled()
  })

  describe('the section table', () => {
    it('names every section', () => {
      expect(sectionLabel('reviews')).toBe('Reviews')
      expect(sectionLabel('learning')).toBe('Learning')
      expect(sectionLabel('settings')).toBe('Settings')
    })

    it('answers which sections have a list to return to', () => {
      expect(sectionHasList('reviews')).toBe(true)
      expect(sectionHasList('learning')).toBe(true)
      // Settings' rail body is an empty spacer, so "back to the list" there would
      // open a panel holding nothing — both routes into the rail read this.
      expect(sectionHasList('settings')).toBe(false)
    })

    it('fails closed on a view the table does not know', () => {
      // A view removed or renamed without updating the table must not yield a
      // back control pointing nowhere, nor a blank label rendered as a control.
      const unknown = 'archive' as MainView
      expect(sectionLabel(unknown)).toBe('')
      expect(sectionHasList(unknown)).toBe(false)
    })
  })
})
