/**
 * The collapsed rail's logo is the only control that expands the sidebar, and
 * it carries no text. A first-time user could not tell it was a button
 * (#25743), so while the rail is collapsed it shows the same instant hover /
 * focus label the collapsed nav rows use, worded with the expand string its
 * aria-label already carries. Expanded, the wordmark and collapse arrow are
 * visible, so no label appears.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { RailBrandToggle } from '../shell/nav/railChrome'
import { i18nT } from '../i18n/t'

afterEach(cleanup)

function renderToggle(effectiveCollapsed: boolean) {
  render(<RailBrandToggle effectiveCollapsed={effectiveCollapsed} toggleNav={() => {}} avatar="" branding={undefined as never} botName="Kiro Crew" />)
  return screen.getByRole('button', { name: i18nT(effectiveCollapsed ? 'app.expand_sidebar' : 'app.collapse_sidebar') })
}

/** Let the label's next-frame fade-in run. */
async function nextFrame() {
  await act(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())))
}

describe('RailBrandToggle expand label (#25743)', () => {
  it('collapsed: hovering the logo shows the expand label', async () => {
    const button = renderToggle(true)
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    fireEvent.mouseEnter(button)
    await nextFrame()
    const tip = screen.getByTestId('rail-expand-tip')
    expect(tip).toHaveTextContent(i18nT('app.expand_sidebar'))
    expect(tip.className).toContain('opacity-100')
  })

  it('collapsed: keyboard focus shows the label and blur hides it', async () => {
    const button = renderToggle(true)
    fireEvent.focus(button)
    await nextFrame()
    expect(screen.getByTestId('rail-expand-tip').className).toContain('opacity-100')
    fireEvent.blur(button)
    expect(screen.getByTestId('rail-expand-tip').className).toContain('opacity-0')
  })

  it('collapsed: clicking dismisses the label at once', async () => {
    const button = renderToggle(true)
    fireEvent.mouseEnter(button)
    await nextFrame()
    expect(screen.getByTestId('rail-expand-tip')).toBeInTheDocument()
    fireEvent.click(button)
    expect(screen.queryByTestId('rail-expand-tip')).toBeNull()
  })

  it('expanded: hover and focus show no label', async () => {
    const button = renderToggle(false)
    fireEvent.mouseEnter(button)
    fireEvent.focus(button)
    await nextFrame()
    expect(screen.queryByTestId('rail-expand-tip')).toBeNull()
  })
})
