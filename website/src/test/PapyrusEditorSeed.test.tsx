/**
 * PapyrusEditor hands Pierre a `file` whose `cacheKey` defines the editing
 * SESSION: Pierre rebuilds its TextDocument when the key changes and otherwise
 * keeps the caret where the user left it. `contents` stays live so the rendered
 * rows track the buffer.
 *
 * Getting the session boundary wrong is what made every document open blank.
 * The page sets `path` the instant a file is picked, but clears `readOnly` one
 * render BEFORE it adopts the fetched buffer — so a session keyed on `path`
 * alone captures the empty string and, the path never having changed, never
 * corrects itself.
 *
 * These tests pin the observed render sequence and the caret guarantee.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({
  renders: [] as { name: string; contents: string; cacheKey: string }[],
  emit: { current: null as ((v: string) => void) | null },
}))

vi.mock('../pierre', async importOriginal => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    ...(await importOriginal<Record<string, unknown>>()),
    // forwardRef because PapyrusEditor attaches a PierreEditorHandle ref for
    // jump-to-line; a plain function component warns and drops it.
    PierreEditor: forwardRef<
      { jumpToLine: (line: number) => void; focus: () => void },
      {
        file: { name: string; contents: string; cacheKey: string }
        onChange: (v: string) => void
      }
    >(({ file, onChange }, ref) => {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }))
      hoisted.renders.push({ name: file.name, contents: file.contents, cacheKey: file.cacheKey })
      // Held so a test can emit an edit the way Pierre does, which is the only
      // way to distinguish a user keystroke from externally-arriving content.
      hoisted.emit.current = onChange
      return <div data-testid="pierre-editor" />
    }),
  }
})

import PapyrusEditor from '../apps/papyrus/PapyrusEditor'

const TEX = '\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n'

/** The props the page passes; only these two move during a file open. */
function props(value: string, readOnly: boolean) {
  return {
    path: 'main.tex',
    value,
    readOnly,
    onChange: () => {},
    onSave: () => {},
    diagnostics: [],
  }
}

const last = () => hoisted.renders[hoisted.renders.length - 1]

beforeEach(() => {
  hoisted.renders.length = 0
  cleanup()
})

describe('PapyrusEditor document seeding', () => {
  it('shows content that lands after readOnly has already cleared', () => {
    // The sequence observed in a real browser: readOnly clears at the render
    // BEFORE the buffer is adopted, so this is the case that shipped blank.
    const view = render(<PapyrusEditor {...props('', true)} />)
    view.rerender(<PapyrusEditor {...props('', false)} />)
    expect(last().contents).toBe('')

    view.rerender(<PapyrusEditor {...props(TEX, false)} />)
    expect(last().contents).toBe(TEX)
  })

  it('starts a new editing session when external content arrives', () => {
    const view = render(<PapyrusEditor {...props('', false)} />)
    const before = last().cacheKey

    view.rerender(<PapyrusEditor {...props(TEX, false)} />)
    expect(last().cacheKey).not.toBe(before)
  })

  it('keeps one session while the user types, so the caret survives', () => {
    // The page echoes each keystroke back as `value`. A new cacheKey here would
    // rebuild Pierre's TextDocument and drop the caret mid-word.
    const view = render(<PapyrusEditor {...props(TEX, false)} />)
    const session = last().cacheKey

    for (const next of [TEX + 'a', TEX + 'ab', TEX + 'abc']) {
      hoisted.emit.current?.(next)                            // Pierre reports the edit
      view.rerender(<PapyrusEditor {...props(next, false)} />) // page echoes the buffer
      expect(last().cacheKey, `keystroke "${next.slice(-3)}" restarted the session`)
        .toBe(session)
    }
    expect(last().contents).toBe(TEX + 'abc')
  })

  it('re-seeds when an external change restores text this editor once emitted', () => {
    // The data-loss path: type A, an external reload delivers B, then something
    // restores A (Cancel, refresh, a pull). A still matched the stale
    // "we emitted this" marker, so the reseed was skipped and the editor kept
    // showing B -- which the next save then wrote over A.
    const view = render(<PapyrusEditor {...props(TEX, false)} />)
    const edited = TEX + '\n% mine'

    hoisted.emit.current?.(edited)                             // our edit
    view.rerender(<PapyrusEditor {...props(edited, false)} />)  // page echoes it
    const session = last().cacheKey

    view.rerender(<PapyrusEditor {...props(TEX, false)} />)     // external -> B
    const afterB = last().cacheKey
    expect(afterB).not.toBe(session)

    view.rerender(<PapyrusEditor {...props(edited, false)} />)  // external -> back to A
    // The reseed is what makes the editor DISPLAY A again; `contents` alone
    // follows the prop either way, so the session change is the real assertion.
    expect(last().cacheKey).not.toBe(afterB)
    expect(last().contents).toBe(edited)
  })

  it('tracks the live buffer in contents', () => {
    const view = render(<PapyrusEditor {...props(TEX, false)} />)
    view.rerender(<PapyrusEditor {...props(TEX + '\n% more', false)} />)
    expect(last().contents).toBe(TEX + '\n% more')
  })

  it('re-seeds on a pull that rewrites the same file underneath', () => {
    const view = render(<PapyrusEditor {...props(TEX, false)} />)
    view.rerender(<PapyrusEditor {...props(TEX, true)} />)
    view.rerender(<PapyrusEditor {...props('% pulled', false)} />)
    expect(last().contents).toBe('% pulled')
  })

  it('re-seeds and renames when the open file changes', () => {
    const view = render(<PapyrusEditor {...props(TEX, false)} />)
    view.rerender(
      <PapyrusEditor {...props('% other', false)} path="sections/intro.tex" />,
    )
    expect(last().contents).toBe('% other')
    expect(last().name).toBe('intro.tex')
  })
})

/**
 * The page gates typing with `readOnly` during any window where the shown text
 * is not the selected file's. That only protects anything if the editor HONOURS
 * it — a prop it accepts and ignores would satisfy the page's own tests. Pierre
 * has no read-only mode wired here, so the component honours the flag by not
 * mounting the editor at all.
 */
describe('PapyrusEditor read-only window', () => {
  it('mounts no editor while read-only, and shows the text instead', () => {
    const { container } = render(<PapyrusEditor {...props(TEX, true)} />)
    expect(hoisted.renders).toHaveLength(0)
    expect(container.querySelector('pre')?.textContent).toBe(TEX)
  })

  it('mounts the editor once the flag clears', () => {
    const view = render(<PapyrusEditor {...props(TEX, true)} />)
    expect(view.container.querySelector('[data-testid="pierre-editor"]')).toBeNull()

    view.rerender(<PapyrusEditor {...props(TEX, false)} />)
    expect(view.container.querySelector('[data-testid="pierre-editor"]')).not.toBeNull()
    expect(view.container.querySelector('pre')).toBeNull()
  })
})
