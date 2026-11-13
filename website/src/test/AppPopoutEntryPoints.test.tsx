// The two app pop-out controls: the rail row's action in the main window
// (pop the app out, or focus it while it is out) and the pop-out window's
// title bar (the app's name and Return).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const popouts = vi.hoisted(() => {
  const listeners = new Set<() => void>()
  const state = { set: new Set<string>() }
  return {
    state,
    listeners,
    setPopped(routes: string[]) { state.set = new Set(routes); listeners.forEach(l => l()) },
    openPopout: vi.fn(),
    focusPopout: vi.fn(),
    returnSelfToMain: vi.fn(),
  }
})
vi.mock('../utils/appPopout', () => ({
  subscribe: (l: () => void) => { popouts.listeners.add(l); return () => popouts.listeners.delete(l) },
  getSnapshot: () => popouts.state.set,
  openPopout: popouts.openPopout,
  focusPopout: popouts.focusPopout,
  returnSelfToMain: popouts.returnSelfToMain,
}))
// Pin a small registry so "is this an app route" does not depend on which
// pages the bundle ships.
vi.mock('../apps/builtinRegistry', () => ({
  hasBuiltinComponent: (route: string) => route === '/dev-fleet' || route === '/aws-control',
}))

import { AppRowPopoutAction } from '../shell/nav/appRowPopoutAction'
import AppPopoutTitleBar from '../components/AppPopoutTitleBar'

/** The row's app name, as the rail passes it. */
const LABEL: Record<string, string> = { '/dev-fleet': 'Dev Fleet', '/aws-control': 'AWS Control', '/apps/my-tool': 'My Tool' }
const POP_OUT = (label: string) => `Pop out ${label} to its own window`
const FOCUS = (label: string) => `${label} is open in its own window — focus it`
const RETURN = 'Return to main window and close this popout'

beforeEach(() => {
  popouts.setPopped([])
  popouts.openPopout.mockClear()
  popouts.focusPopout.mockClear()
  popouts.returnSelfToMain.mockClear()
})

/** The action as the rail mounts it: beside a `role="button"` row, inside a
 *  wrapper that stands in for the sortable row's dnd-kit listeners. */
function renderInRow(route: string, active: boolean, at = '/chat') {
  const wrapper = {
    onPointerDown: vi.fn(), onMouseDown: vi.fn(), onTouchStart: vi.fn(), onKeyDown: vi.fn(), onClick: vi.fn(),
  }
  const rowClick = vi.fn()
  renderWithProviders(
    <div
      data-testid="wrapper"
      role="presentation"
      onPointerDown={wrapper.onPointerDown}
      onMouseDown={wrapper.onMouseDown}
      onTouchStart={wrapper.onTouchStart}
      onKeyDown={wrapper.onKeyDown}
      onClick={wrapper.onClick}
    >
      <div className="relative group/approw">
        <div role="button" tabIndex={0} data-testid="row" onClick={rowClick} onKeyDown={rowClick}>row</div>
        <AppRowPopoutAction route={route} label={LABEL[route] ?? route} active={active} />
      </div>
    </div>,
    { route: at },
  )
  return { wrapper, rowClick }
}

describe('AppRowPopoutAction', () => {
  it('on a row that is not the app on screen, opens the app at its route', () => {
    renderInRow('/dev-fleet', false, '/chat?sid=s1')
    fireEvent.click(screen.getByRole('button', { name: POP_OUT('Dev Fleet') }))
    expect(popouts.openPopout).toHaveBeenCalledWith('/dev-fleet', '/dev-fleet')
    expect(popouts.focusPopout).not.toHaveBeenCalled()
  })

  it('on the active row, opens the app at the page and query on screen', () => {
    renderInRow('/aws-control', true, '/aws-control/usage?tab=x')
    fireEvent.click(screen.getByRole('button', { name: POP_OUT('AWS Control') }))
    expect(popouts.openPopout).toHaveBeenCalledWith('/aws-control', '/aws-control/usage?tab=x')
  })

  it('opens an installed app by its /apps/<name> route', () => {
    renderInRow('/apps/my-tool', false)
    fireEvent.click(screen.getByRole('button', { name: POP_OUT('My Tool') }))
    expect(popouts.openPopout).toHaveBeenCalledWith('/apps/my-tool', '/apps/my-tool')
  })

  it('shows only on row hover or keyboard focus while the app is in this window, and always on a touch screen', () => {
    renderInRow('/dev-fleet', false)
    const btn = screen.getByRole('button', { name: POP_OUT('Dev Fleet') })
    expect(btn.querySelector('[data-icon="pop-out"]')).not.toBeNull()
    expect(btn).toHaveClass('opacity-0', 'group-hover/approw:opacity-100', 'focus-visible:opacity-100')
    // `hover:` is gated on `(hover: hover)`, so without this a touch screen
    // could never reach the control (`hoverVariantPolicy.test.ts` scans for it).
    expect(btn).toHaveClass('[@media(hover:none)]:opacity-100')
  })

  it('while the app is out, is an always-shown accent mark that focuses its window', () => {
    renderInRow('/dev-fleet', true, '/dev-fleet')
    act(() => popouts.setPopped(['/dev-fleet']))
    expect(screen.queryByRole('button', { name: POP_OUT('Dev Fleet') })).toBeNull()
    const mark = screen.getByRole('button', { name: FOCUS('Dev Fleet') })
    expect(mark).toHaveClass('opacity-100', 'text-accent')
    expect(mark).not.toHaveClass('opacity-0')
    // The state is shown by shape, not by colour alone: its own glyph.
    expect(mark.querySelector('[data-icon="popped-out"]')).not.toBeNull()
    expect(mark.querySelector('[data-icon="pop-out"]')).toBeNull()
    fireEvent.click(mark)
    expect(popouts.focusPopout).toHaveBeenCalledWith('/dev-fleet')
    expect(popouts.openPopout).not.toHaveBeenCalled()
  })

  it('reads only its OWN route out of the live set', () => {
    renderInRow('/dev-fleet', false)
    act(() => popouts.setPopped(['/aws-control']))
    expect(screen.getByRole('button', { name: POP_OUT('Dev Fleet') })).toBeInTheDocument()
  })

  it('says which app it is for, with one string per state for the hint and the accessible name', () => {
    renderInRow('/dev-fleet', true, '/dev-fleet')
    const btn = screen.getByRole('button', { name: POP_OUT('Dev Fleet') })
    // A tab stop shows the hint at once.
    act(() => { btn.focus() })
    expect(screen.getByRole('tooltip')).toHaveTextContent(POP_OUT('Dev Fleet'))
    act(() => popouts.setPopped(['/dev-fleet']))
    expect(btn).toHaveAccessibleName(FOCUS('Dev Fleet'))
    expect(screen.getByRole('tooltip')).toHaveTextContent(FOCUS('Dev Fleet'))
  })

  it('keeps a press from the sortable wrapper and a click from the row', () => {
    const { wrapper, rowClick } = renderInRow('/dev-fleet', false)
    const btn = screen.getByRole('button', { name: POP_OUT('Dev Fleet') })
    fireEvent.pointerDown(btn)
    fireEvent.mouseDown(btn)
    fireEvent.touchStart(btn)
    fireEvent.click(btn)
    expect(wrapper.onPointerDown).not.toHaveBeenCalled()
    expect(wrapper.onMouseDown).not.toHaveBeenCalled()
    expect(wrapper.onTouchStart).not.toHaveBeenCalled()
    expect(wrapper.onClick).not.toHaveBeenCalled()
    expect(rowClick).not.toHaveBeenCalled()
    expect(popouts.openPopout).toHaveBeenCalledTimes(1)
    // Control: the same events on the row do reach the wrapper, so the
    // assertions above are about the button and not a dead harness.
    fireEvent.mouseDown(screen.getByTestId('row'))
    expect(wrapper.onMouseDown).toHaveBeenCalledTimes(1)
  })

  it('lets a key pressed on it reach the document, where the shell’s shortcuts listen', () => {
    renderInRow('/dev-fleet', false)
    const btn = screen.getByRole('button', { name: POP_OUT('Dev Fleet') })
    const seen: string[] = []
    const onKey = (e: KeyboardEvent) => { seen.push(e.key) }
    document.addEventListener('keydown', onKey)
    try {
      fireEvent.keyDown(btn, { key: 'Enter', code: 'Enter' })
      fireEvent.keyDown(btn, { key: '1', code: 'Digit1', altKey: true })
    } finally {
      document.removeEventListener('keydown', onKey)
    }
    expect(seen).toEqual(['Enter', '1'])
  })

  it('is a sibling of the row, not inside it', () => {
    renderInRow('/dev-fleet', false)
    const btn = screen.getByRole('button', { name: POP_OUT('Dev Fleet') })
    const row = screen.getByTestId('row')
    expect(row.contains(btn)).toBe(false)
    expect(btn.parentElement).toBe(row.parentElement)
  })

  it.each(['/chat', '/apps', '/apps/library', '/not-an-app'])('renders nothing for %s, which is not an app', (route) => {
    renderInRow(route, false)
    expect(screen.queryByTestId('app-row-popout-action')).toBeNull()
  })
})

describe('AppPopoutTitleBar', () => {
  it('names the app and offers Return, which goes back to the main window and closes this one', () => {
    renderWithProviders(<AppPopoutTitleBar label="Dev Fleet" />)
    const bar = screen.getByTestId('app-popout-title-bar')
    expect(bar).toHaveTextContent('Dev Fleet')
    // It does not navigate the main window, so it promises no more than that.
    const ret = screen.getByRole('button', { name: RETURN })
    expect(ret).toHaveTextContent('Return')
    expect(ret).toHaveAttribute('title', RETURN)
    fireEvent.click(ret)
    expect(popouts.returnSelfToMain).toHaveBeenCalledTimes(1)
  })

  it('is a 32px row whose trailing group clears the focus-mode caption controls', () => {
    renderWithProviders(<AppPopoutTitleBar label="Dev Fleet" />)
    const bar = screen.getByTestId('app-popout-title-bar')
    expect(bar).toHaveClass('h-8', 'shrink-0', 'border-b')
    const ret = screen.getByRole('button', { name: RETURN })
    expect(ret.closest('.focus-caption-reserve')).not.toBeNull()
    expect(bar.contains(ret.closest('.focus-caption-reserve'))).toBe(true)
  })

  it('keeps a long name readable when it is cut off', () => {
    renderWithProviders(<AppPopoutTitleBar label="A very long app name" />)
    expect(screen.getByTitle('A very long app name')).toHaveClass('truncate')
  })
})
