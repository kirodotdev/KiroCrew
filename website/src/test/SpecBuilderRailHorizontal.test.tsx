// The collapsed rail has two orientations: a strip down the left edge on a
// desktop, and a bar across the TOP while narrow, where its ~48px of width is a
// tenth of the reading column. This covers the horizontal branch -- the bar's
// identity control, its action, and the agent-activity indicator -- and pins the
// two decisions that are easy to lose: the bar spends at most two actions (so it
// does not violate max-two-buttons-per-row), and Settings therefore stays in the
// expanded rail rather than riding along.
import { describe, it, expect, vi } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import SpecRail from '../apps/spec-builder/components/SpecRail'
import type { SpecSummary } from '../apps/spec-builder/api'

const spec = (over: Partial<SpecSummary> = {}): SpecSummary => ({
  name: 'dark-mode',
  phase: 'design',
  running: false,
  ...over,
} as SpecSummary)

function renderBar(over: Partial<React.ComponentProps<typeof SpecRail>> = {}) {
  const onExpand = vi.fn()
  const onNew = vi.fn()
  const onSettings = vi.fn()
  const r = render(
    <SpecRail
      specs={[spec()]}
      sel={null}
      setSel={() => {}}
      onNew={onNew}
      onSettings={onSettings}
      width={48}
      collapsed
      horizontal
      onExpand={onExpand}
      {...over}
    />,
  )
  return { ...r, onExpand, onNew, onSettings }
}

describe('Spec Builder collapsed rail, horizontal orientation', () => {
  it('lays out as a row and names the app', () => {
    const { container, getByText } = renderBar()
    const aside = container.querySelector('aside')
    expect(aside, 'expected an aside root').not.toBeNull()
    // A row across the top, full width -- not a narrow strip.
    expect(aside!.className).toMatch(/w-full/)
    expect(aside!.className).toMatch(/flex items-center/)
    expect(getByText('Spec Builder')).toBeTruthy()
    cleanup()
  })

  it('reopens the rail from the identity control', () => {
    const { getByLabelText, onExpand } = renderBar()
    // The identity half doubles as the expand control, the way the vertical card
    // does -- so the whole label is a target, not just an icon.
    fireEvent.click(getByLabelText(/show spec list/i))
    expect(onExpand).toHaveBeenCalledTimes(1)
    cleanup()
  })

  it('keeps the new-spec action but NOT settings', () => {
    const { getByLabelText, queryByLabelText, onNew } = renderBar()
    fireEvent.click(getByLabelText(/new spec/i))
    expect(onNew).toHaveBeenCalledTimes(1)
    // AUTOSDE's max-two-buttons-per-row is blocking and this bar is a NEW row:
    // expand and new spend its two. Settings stays in the expanded rail's footer,
    // one tap away through expand.
    expect(queryByLabelText(/spec builder settings/i)).toBeNull()
    cleanup()
  })

  it('shows the agent-activity indicator only while a spec is running', () => {
    const idle = renderBar()
    expect(idle.queryByLabelText(/an agent is working/i)).toBeNull()
    cleanup()
    const busy = renderBar({ specs: [spec({ running: true })] })
    expect(busy.queryByLabelText(/an agent is working/i)).not.toBeNull()
    cleanup()
  })

  it('still renders the vertical strip when not horizontal', () => {
    // The desktop orientation must survive: same collapsed state, different axis.
    const { container } = renderBar({ horizontal: false })
    const aside = container.querySelector('aside')
    expect(aside!.className).toMatch(/flex-col/)
    expect(aside!.className).not.toMatch(/w-full/)
    cleanup()
  })
})

describe('Spec Builder expanded rail', () => {
  it('keeps archived specs selectable and shows their title', () => {
    const setSel = vi.fn()
    renderBar({
      collapsed: false,
      horizontal: false,
      width: 260,
      setSel,
      specs: [
        spec(),
        spec({ name: 'archived-dark-mode', title: 'Archived dark mode', archived: true }),
      ],
    })

    expect(screen.getByText('Archived')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Archived dark mode'))
    expect(setSel).toHaveBeenCalledWith('archived-dark-mode')
    cleanup()
  })
})
