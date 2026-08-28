import { describe, it, expect, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'
import { useRef } from 'react'
import { menuItemsOf, useMenuKeyboard } from './useMenuKeyboard'

/**
 * The shared `role="menu"` keyboard contract (#6231) — the extraction of
 * `MenuBtn`'s roving-focus keydown logic. These tests pin the seam itself
 * (item discovery, wrap at both ends, Home/End, entry-from-outside, Tab
 * containment per #2533, the IME claim, the editable-outside guard, and the
 * degenerate item lists), so each consuming surface only needs a wiring test.
 * `MenuBtn`'s own DevFleetPage tests double as the extraction's
 * behaviour-preservation proof and are deliberately untouched.
 */

function Harness({
  enabled = true,
  focusFirstOnOpen,
  items = ['one', 'two', 'three'],
  disabled = [] as string[],
}: {
  enabled?: boolean
  focusFirstOnOpen?: boolean
  items?: string[]
  disabled?: string[]
}) {
  const menuRef = useRef<HTMLDivElement>(null)
  useMenuKeyboard({ enabled, containerRef: menuRef, focusFirstOnOpen })
  return (
    <div>
      <button data-testid="outside-btn">outside</button>
      <input data-testid="outside-input" aria-label="outside editor" />
      <div ref={menuRef} role="menu" data-testid="menu" aria-label="test menu">
        {items.map(label => (
          <div
            key={label}
            role="menuitem"
            tabIndex={-1}
            data-testid={`item-${label}`}
            aria-disabled={disabled.includes(label) || undefined}
          >
            {label}
          </div>
        ))}
      </div>
    </div>
  )
}

afterEach(cleanup)

const item = (utils: ReturnType<typeof render>, label: string) =>
  utils.getByTestId(`item-${label}`) as HTMLElement

describe('menuItemsOf', () => {
  it('returns [] for a null container', () => {
    expect(menuItemsOf(null)).toEqual([])
  })

  it('collects menuitem/menuitemradio/menuitemcheckbox/button rows and skips disabled ones', () => {
    // Fixture built with DOM APIs rather than an HTML-string write — the
    // frontend-security rule (AUTOSDE.yaml) bans those unconditionally,
    // tests included.
    const host = document.createElement('div')
    const el = (tag: string, attrs: Record<string, string>, text: string) => {
      const node = document.createElement(tag)
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
      node.textContent = text
      return node
    }
    host.append(
      el('div', { role: 'menuitem' }, 'a'),
      el('button', {}, 'b'),
      el('button', { disabled: '' }, 'c'),
      el('div', { role: 'menuitemradio' }, 'd'),
      el('div', { role: 'menuitemcheckbox' }, 'e'),
      el('div', { role: 'button', 'aria-disabled': 'true' }, 'f'),
      el('div', { role: 'separator' }, ''),
      el('span', {}, 'not an item'),
    )
    expect(menuItemsOf(host).map(item => item.textContent)).toEqual(['a', 'b', 'd', 'e'])
  })
})

describe('useMenuKeyboard', () => {
  it('moves focus onto the first enabled item when enabled flips true', () => {
    const utils = render(<Harness enabled={false} />)
    expect(document.activeElement).toBe(document.body)
    utils.rerender(<Harness enabled={true} />)
    expect(item(utils, 'one')).toHaveFocus()
  })

  it('skips focus entry when focusFirstOnOpen is false', () => {
    const utils = render(<Harness focusFirstOnOpen={false} />)
    expect(item(utils, 'one')).not.toHaveFocus()
    // The contract still works: an arrow ENTERS the list.
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(item(utils, 'one')).toHaveFocus()
  })

  it('ArrowDown/ArrowUp walk the items and wrap at both ends', () => {
    const utils = render(<Harness />)
    const menu = utils.getByTestId('menu')
    expect(item(utils, 'one')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(item(utils, 'two')).toHaveFocus()
    item(utils, 'three').focus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(item(utils, 'one')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(item(utils, 'three')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(item(utils, 'two')).toHaveFocus()
  })

  it('Home/End jump to the boundary items', () => {
    const utils = render(<Harness />)
    const menu = utils.getByTestId('menu')
    fireEvent.keyDown(menu, { key: 'End' })
    expect(item(utils, 'three')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Home' })
    expect(item(utils, 'one')).toHaveFocus()
  })

  it('arrows enter the list when focus is outside it: Down to first, Up to last', () => {
    const utils = render(<Harness focusFirstOnOpen={false} />)
    fireEvent.keyDown(document.body, { key: 'ArrowUp' })
    expect(item(utils, 'three')).toHaveFocus()
    document.body.focus()
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(item(utils, 'one')).toHaveFocus()
  })

  it('lets modified arrows through untouched (OS/browser shortcuts)', () => {
    const utils = render(<Harness />)
    const menu = utils.getByTestId('menu')
    fireEvent.keyDown(menu, { key: 'ArrowDown', metaKey: true })
    fireEvent.keyDown(menu, { key: 'ArrowDown', ctrlKey: true })
    fireEvent.keyDown(menu, { key: 'End', altKey: true })
    expect(item(utils, 'one')).toHaveFocus()
  })

  it('contains Tab/Shift-Tab within the enabled items (#2533)', () => {
    const utils = render(<Harness />)
    const menu = utils.getByTestId('menu')
    item(utils, 'three').focus()
    fireEvent.keyDown(menu, { key: 'Tab' })
    expect(item(utils, 'one')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Tab', shiftKey: true })
    expect(item(utils, 'three')).toHaveFocus()
  })

  it('skips disabled items in the arrow cycle and the Tab boundary', () => {
    const utils = render(<Harness disabled={['two']} />)
    const menu = utils.getByTestId('menu')
    expect(item(utils, 'one')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(item(utils, 'three')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Tab' })
    expect(item(utils, 'one')).toHaveFocus()
  })

  it('a single-item menu keeps focus put on arrows and contains Tab (the Pierre degenerate case)', () => {
    const utils = render(<Harness items={['only']} />)
    const menu = utils.getByTestId('menu')
    expect(item(utils, 'only')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(item(utils, 'only')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(item(utils, 'only')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Tab' })
    expect(item(utils, 'only')).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Tab', shiftKey: true })
    expect(item(utils, 'only')).toHaveFocus()
  })

  it('does nothing (and does not throw) on an empty or all-disabled item list', () => {
    const utils = render(<Harness items={[]} />)
    expect(document.activeElement).toBe(document.body)
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    fireEvent.keyDown(document.body, { key: 'Tab' })
    expect(document.activeElement).toBe(document.body)
    utils.rerender(<Harness items={['a', 'b']} disabled={['a', 'b']} />)
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(document.body)
  })

  it('declines an arrow that belongs to an IME composition', () => {
    // On WebKit the keydown that moves through composition candidates can
    // arrive AFTER compositionend with `isComposing` already false — an
    // unguarded arrow branch would move menu focus mid-composition.
    const utils = render(<Harness />)
    const first = item(utils, 'one')
    expect(first).toHaveFocus()
    fireEvent.compositionStart(first)
    fireEvent.compositionEnd(first)
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    expect(first).toHaveFocus()
  })

  it('leaves keys alone while focus sits in an editable element outside the menu', () => {
    // An open popover must not hijack a caret's arrows (the composer textarea
    // under MicSourceMenu). A non-editable outside element (the trigger
    // button) still routes arrows into the menu.
    const utils = render(<Harness focusFirstOnOpen={false} />)
    const input = utils.getByTestId('outside-input')
    input.focus()
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveFocus()
    const btn = utils.getByTestId('outside-btn')
    btn.focus()
    fireEvent.keyDown(btn, { key: 'ArrowDown' })
    expect(item(utils, 'one')).toHaveFocus()
  })

  it('detaches the listener when disabled', () => {
    const utils = render(<Harness />)
    utils.rerender(<Harness enabled={false} />)
    document.body.focus()
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(document.body)
  })
})
