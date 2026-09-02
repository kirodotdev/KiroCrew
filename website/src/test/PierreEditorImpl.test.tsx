/**
 * `PierreEditorImpl` is the dashboard's only wrapper around Pierre's editable
 * surface (`@pierre/diffs/edit`). Pierre renders through custom elements that
 * never upgrade under happy-dom, so the library is replaced with doubles here
 * — but doubles that keep the one contract the wrapper is built on:
 * `EditProvider` constructs the editor through the `createEditor` factory the
 * wrapper hands it, and the constructed editor announces itself through
 * `editorOptions.onAttach`. Pierre raises that callback from a deferred render
 * pass, so the harness attaches AFTER mount rather than during it.
 *
 * The subject is therefore the wrapper's own logic: which surface it renders
 * for a given `diffBase`, how it resolves options, how it maps our
 * `EditorMarker` onto Pierre's whole-line `Marker`, the imperative
 * `jumpToLine`/`focus` handle, the cursor mirror, and the save chord.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup, fireEvent, act } from '@testing-library/react'
import { createRef, type ComponentProps, type ReactNode } from 'react'
import type { FileContents } from '@pierre/diffs'

const pierre = vi.hoisted(() => {
  const editors: FakeEditor[] = []

  /** The slice of Pierre's `Editor` this wrapper touches, with every call
   *  recorded so a test can assert what the wrapper asked for. */
  class FakeEditor {
    readonly calls: { method: string; args: unknown[] }[] = []
    state: Record<string, unknown> | undefined = { selections: [] }
    options: Record<string, unknown>

    constructor(options: Record<string, unknown>) {
      this.options = options
      editors.push(this)
    }

    getState() {
      this.calls.push({ method: 'getState', args: [] })
      return this.state
    }

    setSelections(selections: unknown) {
      this.calls.push({ method: 'setSelections', args: [selections] })
    }

    setMarkers(markers: unknown) {
      this.calls.push({ method: 'setMarkers', args: [markers] })
    }

    focus(options?: unknown) {
      this.calls.push({ method: 'focus', args: options === undefined ? [] : [options] })
    }

    /** What Pierre does once the surface has an editable file bound. */
    announceAttach() {
      const onAttach = this.options.onAttach as
        | ((editor: unknown, instance: unknown) => void)
        | undefined
      onAttach?.(this, {})
    }

    of(method: string) {
      return this.calls.filter(c => c.method === method)
    }
  }

  return {
    editors,
    FakeEditor,
    factory: { current: null as null | ((o: Record<string, unknown>) => FakeEditor) },
    surfaces: [] as { kind: 'file' | 'diff'; props: Record<string, unknown> }[],
  }
})

vi.mock('@pierre/diffs/edit', () => ({ Editor: pierre.FakeEditor }))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  return {
    /** Mirrors the real provider's shape: an outer scroller wrapping a content
     *  div, so a test can assert which element carries the scroll classes. */
    Virtualizer: ({ className, children }: { className?: string; children?: ReactNode }) => (
      <div className={className} data-testid="pierre-virtualizer"><div>{children}</div></div>
    ),
    EditProvider: ({
      createEditor,
      children,
    }: {
      createEditor: (o: Record<string, unknown>) => unknown
      children?: ReactNode
    }) => {
      pierre.factory.current = createEditor as (o: Record<string, unknown>) => InstanceType<
        typeof pierre.FakeEditor
      >
      return <>{children}</>
    },
    File: (props: Record<string, unknown>) => {
      pierre.surfaces.push({ kind: 'file', props })
      return <div data-testid="pierre-file" />
    },
    MultiFileDiff: (props: Record<string, unknown>) => {
      pierre.surfaces.push({ kind: 'diff', props })
      return <div data-testid="pierre-diff" />
    },
    // Imported by the sibling `PierreImpl` (which this module pulls in for
    // `contentCacheKey`); never rendered here.
    FileDiff: () => null,
    WorkerPoolContext: createContext(null),
  }
})

import { PierreEditorImpl, type PierreEditorHandle } from '../pierre/PierreEditorImpl'
import { contentCacheKey } from '../pierre/PierreImpl'

const FILE: FileContents = {
  name: 'a.ts',
  contents: 'const a = 1\nconst b = 2\nconst c = 3\n',
  cacheKey: 'a.ts:seed',
}

type Props = ComponentProps<typeof PierreEditorImpl>

function mount(extra: Partial<Props> = {}) {
  const ref = createRef<PierreEditorHandle>()
  const onChange = vi.fn()
  const onSave = vi.fn()
  const onCursorChange = vi.fn()
  const props: Props = {
    file: FILE,
    onChange,
    onSave,
    onCursorChange,
    ...extra,
  }
  const view = render(<PierreEditorImpl ref={ref} {...props} />)
  const rerender = (next: Partial<Props> = {}) =>
    view.rerender(<PierreEditorImpl ref={ref} {...props} {...next} />)
  return { view, rerender, ref, onChange, onSave, onCursorChange }
}

const lastSurface = () => pierre.surfaces[pierre.surfaces.length - 1]

const surfaceOptions = () => lastSurface().props.options as Record<string, unknown>

const editorOptions = () =>
  lastSurface().props.editorOptions as {
    onChange?: (file: { name: string; contents: string }) => void
  }

/** Build the editor through the wrapper's own factory, then let it announce
 *  itself — the deferred order Pierre uses. */
function attachEditor() {
  const factory = pierre.factory.current
  expect(factory, 'EditProvider was never handed a createEditor factory').toBeTruthy()
  const editor = factory!(editorOptions() as unknown as Record<string, unknown>)
  act(() => {
    editor.announceAttach()
  })
  return editor
}

const scrollerOf = (view: ReturnType<typeof render>) =>
  view.container.querySelector('.pierre-surface') as HTMLElement

const oneSelection = (line: number, character: number) => ({
  selections: [
    { start: { line, character }, end: { line, character }, direction: 'none' },
  ],
})

beforeEach(() => {
  cleanup()
  pierre.surfaces.length = 0
  pierre.editors.length = 0
  pierre.factory.current = null
  document.documentElement.removeAttribute('data-theme')
})

describe('PierreEditorImpl surface selection', () => {
  it('renders the plain editor when no diff baseline is supplied', () => {
    const { view } = mount()

    expect(lastSurface().kind).toBe('file')
    expect(view.getByTestId('pierre-file')).toBeInTheDocument()
    expect(lastSurface().props.file).toBe(FILE)
    expect(lastSurface().props.edit).toBe(true)
  })

  it('renders the live-diff surface with a null baseline for a brand-new file', () => {
    // `diffBase: null` is a distinct request from omitting it: the whole buffer
    // reads as added, so the surface must still be the diff one.
    const { view } = mount({ diffBase: null })

    expect(lastSurface().kind).toBe('diff')
    expect(view.getByTestId('pierre-diff')).toBeInTheDocument()
    expect(lastSurface().props.oldFile).toBeNull()
    expect(lastSurface().props.newFile).toBe(FILE)
  })

  it('keys the baseline on its own contents, not the filename', () => {
    // Pierre caches highlight results by cacheKey, so a baseline keyed on the
    // name alone would serve the first baseline's tokens after a pull.
    const { rerender } = mount({ diffBase: 'const a = 0\n' })
    const first = lastSurface().props.oldFile as FileContents

    expect(first.name).toBe(FILE.name)
    expect(first.contents).toBe('const a = 0\n')
    expect(first.cacheKey).toBe(contentCacheKey(FILE.name, 'const a = 0\n'))

    rerender({ diffBase: 'const a = 9\n' })
    expect((lastSurface().props.oldFile as FileContents).cacheKey).not.toBe(first.cacheKey)
  })

  it('lays the live diff out unified by default and split on request', () => {
    mount({ diffBase: '' })
    expect(surfaceOptions().diffStyle).toBe('unified')

    cleanup()
    pierre.surfaces.length = 0
    mount({ diffBase: '', diffSplit: true })
    expect(surfaceOptions().diffStyle).toBe('split')
  })

  it('folds unchanged regions unless the call site asks to expand them', () => {
    mount({ diffBase: '' })
    expect(surfaceOptions().expandUnchanged).toBe(false)

    cleanup()
    pierre.surfaces.length = 0
    mount({ diffBase: '', diffExpandUnchanged: true })
    expect(surfaceOptions().expandUnchanged).toBe(true)
  })

  it('zeroes the code padding the caret overlay double-counts', () => {
    // `Editor.#getLineY` adds `metrics.paddingTop` to an `offsetTop` that
    // already carries it, so a non-zero `[data-code]` padding puts the caret and
    // the drag-selection range that many pixels below their row.
    mount({ diffBase: '' })
    expect(String(surfaceOptions().unsafeCSS)).toMatch(/\[data-code\]\{padding-top:0\}/)
  })

  it('keeps a call site’s own unsafeCSS when adding the caret alignment', () => {
    // Appended, never assigned: overwriting would silently drop whichever
    // surface-specific CSS the caller passed.
    mount({ diffBase: '', options: { unsafeCSS: '[data-line]{color:red}' } })
    const css = String(surfaceOptions().unsafeCSS)
    expect(css).toContain('[data-line]{color:red}')
    expect(css).toMatch(/\[data-code\]\{padding-top:0\}/)
  })

  it('drives Pierre with the dashboard theme rather than the OS preference', () => {
    document.documentElement.setAttribute('data-theme', 'kirocrew-dark')
    mount()
    expect(surfaceOptions().themeType).toBe('dark')

    cleanup()
    pierre.surfaces.length = 0
    document.documentElement.setAttribute('data-theme', 'kirocrew-light')
    mount()
    expect(surfaceOptions().themeType).toBe('light')
  })

  it('lets a call site override the shared code defaults', () => {
    mount({ options: { overflow: 'wrap' } })

    expect(surfaceOptions().overflow).toBe('wrap')
    // The rest of the shared defaults survive the merge.
    expect(surfaceOptions().disableFileHeader).toBe(true)
  })

  it('appends the caller class to the scroll container', () => {
    const { view } = mount({ className: 'papyrus-editor' })
    expect(scrollerOf(view).className).toContain('papyrus-editor')

    cleanup()
    const bare = mount()
    expect(scrollerOf(bare.view).className.trim().endsWith('overflow-auto')).toBe(true)
  })

  it('keeps one editor identity for the life of the surface', () => {
    // Pierre's factory caches by options-object identity: a fresh object on any
    // re-render would rebuild the edit session and drop the caret mid-word.
    const { rerender } = mount()
    const first = lastSurface().props.editorOptions

    rerender({ file: { ...FILE, contents: `${FILE.contents}x`, cacheKey: 'a.ts:next' } })
    rerender({ markers: [{ severity: 'error', message: 'boom', line: 1 }] })

    expect(pierre.surfaces.length).toBeGreaterThan(2)
    expect(lastSurface().props.editorOptions).toBe(first)
  })
})

describe('PierreEditorImpl marker mapping', () => {
  it('maps a diagnostic onto a whole-line Pierre marker', () => {
    const { rerender } = mount({ markers: [] })
    const editor = attachEditor()

    rerender({ markers: [{ severity: 'warning', message: 'unused import', line: 3 }] })

    const sent = editor.of('setMarkers')
    expect(sent).toHaveLength(1)
    expect(sent[0].args[0]).toEqual([
      {
        severity: 'warning',
        message: 'unused import',
        start: { line: 2, character: 0 },
        end: { line: 2, character: Number.MAX_SAFE_INTEGER },
      },
    ])
  })

  it('maps every severity and every diagnostic in the list', () => {
    const { rerender } = mount({ markers: [] })
    const editor = attachEditor()

    rerender({
      markers: [
        { severity: 'error', message: 'undefined name', line: 1 },
        { severity: 'info', message: 'consider const', line: 2 },
      ],
    })

    const sent = editor.of('setMarkers')[0].args[0] as { severity: string; start: { line: number } }[]
    expect(sent.map(m => m.severity)).toEqual(['error', 'info'])
    expect(sent.map(m => m.start.line)).toEqual([0, 1])
  })

  it('clears the gutter with an empty list rather than leaving stale diagnostics', () => {
    const { rerender } = mount({ markers: [] })
    const editor = attachEditor()

    rerender({ markers: [{ severity: 'error', message: 'boom', line: 2 }] })
    rerender({ markers: [] })

    const sent = editor.of('setMarkers')
    expect(sent).toHaveLength(2)
    expect(sent[1].args[0]).toEqual([])
  })

  it('never touches the marker gutter while the prop is absent', () => {
    // A surface with no diagnostics contract must not wipe markers some other
    // owner set; `undefined` and `[]` are different requests.
    const { rerender } = mount()
    const editor = attachEditor()

    rerender({ file: { ...FILE, cacheKey: 'a.ts:again' } })

    expect(editor.of('setMarkers')).toHaveLength(0)
  })
})

describe('PierreEditorImpl cursor mirror', () => {
  it('reports the caret one-based on key-up', () => {
    const { view, onCursorChange } = mount()
    const editor = attachEditor()
    editor.state = oneSelection(3, 5)

    fireEvent.keyUp(scrollerOf(view))

    expect(onCursorChange).toHaveBeenCalledWith(4, 6)
  })

  it('reports the caret on mouse-up too', () => {
    const { view, onCursorChange } = mount()
    const editor = attachEditor()
    editor.state = oneSelection(0, 0)

    fireEvent.mouseUp(scrollerOf(view))

    expect(onCursorChange).toHaveBeenCalledWith(1, 1)
  })

  it('reports the edit and the caret together when Pierre announces a change', () => {
    const { onChange, onCursorChange } = mount()
    const editor = attachEditor()
    editor.state = oneSelection(1, 4)

    act(() => {
      editorOptions().onChange?.({ name: FILE.name, contents: 'edited\n' })
    })

    expect(onChange).toHaveBeenCalledWith('edited\n')
    expect(onCursorChange).toHaveBeenCalledWith(2, 5)
  })

  it('stays quiet when the editor reports no selection', () => {
    const { view, onCursorChange } = mount()
    const editor = attachEditor()
    const scroller = scrollerOf(view)

    editor.state = { selections: [] }
    fireEvent.keyUp(scroller)

    editor.state = {}
    fireEvent.keyUp(scroller)

    editor.state = undefined
    fireEvent.keyUp(scroller)

    expect(editor.of('getState')).toHaveLength(3)
    expect(onCursorChange).not.toHaveBeenCalled()
  })

  it('stays quiet before the editor has attached', () => {
    // Pierre announces the editor a render late, so every listener on the
    // container fires at least once with nothing behind it.
    const { view, onCursorChange } = mount()

    fireEvent.keyUp(scrollerOf(view))
    fireEvent.mouseUp(scrollerOf(view))

    expect(pierre.editors).toHaveLength(0)
    expect(onCursorChange).not.toHaveBeenCalled()
  })

  it('tolerates a surface that wants no cursor readout', () => {
    const { view } = mount({ onCursorChange: undefined })
    const editor = attachEditor()
    editor.state = oneSelection(2, 2)

    fireEvent.keyUp(scrollerOf(view))

    expect(editor.of('getState')).toHaveLength(1)
  })
})

describe('PierreEditorImpl save chord', () => {
  it('runs the save handler on Cmd+S and swallows the browser default', () => {
    // Without preventDefault the browser opens its own "save page" dialog over
    // the editor.
    const { view, onSave } = mount()

    const notPrevented = fireEvent.keyDown(scrollerOf(view), { key: 's', metaKey: true })

    expect(notPrevented).toBe(false)
    expect(onSave).toHaveBeenCalledTimes(1)
  })

  it('accepts Ctrl+S and a shifted S', () => {
    const { view, onSave } = mount()
    const scroller = scrollerOf(view)

    fireEvent.keyDown(scroller, { key: 's', ctrlKey: true })
    fireEvent.keyDown(scroller, { key: 'S', metaKey: true, shiftKey: true })

    expect(onSave).toHaveBeenCalledTimes(2)
  })

  it('leaves an unmodified S and other chords to the editor', () => {
    const { view, onSave } = mount()
    const scroller = scrollerOf(view)

    expect(fireEvent.keyDown(scroller, { key: 's' })).toBe(true)
    expect(fireEvent.keyDown(scroller, { key: 'a', metaKey: true })).toBe(true)

    expect(onSave).not.toHaveBeenCalled()
  })

  it('still swallows the chord on a surface with no save handler', () => {
    const { view } = mount({ onSave: undefined })

    expect(fireEvent.keyDown(scrollerOf(view), { key: 's', metaKey: true })).toBe(false)
  })
})

describe('PierreEditorImpl imperative handle', () => {
  it('moves the caret to the requested line, zero-based, and focuses', () => {
    const { ref } = mount()
    const editor = attachEditor()

    act(() => ref.current!.jumpToLine(3))

    // One call, not a selection plus a bare focus: focus({ lineNumber }) is
    // Pierre's jump entry point and the only one that parks a retry when the
    // target row has not been built yet. lineNumber is one-based.
    expect(editor.of('focus').map(c => c.args[0])).toEqual([{ lineNumber: 3 }])
    expect(editor.of('setSelections')).toHaveLength(0)
  })

  it('clamps a non-positive line onto the first row', () => {
    // Callers pass line numbers straight from compiler output, which uses 0 for
    // "whole file"; a negative zero-based line would land Pierre out of range.
    const { ref } = mount()
    const editor = attachEditor()

    act(() => ref.current!.jumpToLine(0))
    act(() => ref.current!.jumpToLine(-40))

    // Callers pass 0 for "whole file"; Pierre's lineNumber is one-based, so a
    // non-positive request must floor onto the first line, not underflow.
    const lines = editor.of('focus').map(c => (c.args[0] as { lineNumber: number }).lineNumber)
    expect(lines).toEqual([1, 1])
  })

  it('asks Pierre to reveal the target line instead of scrolling the host itself', () => {
    // The reveal is delegated: setSelections materializes the row when the
    // rendered window does not cover it, then scrolls the caret into view.
    // Computing an offset here would have to assume a fixed row height and a
    // known scroll container, and windowing invalidates both.
    const { view, ref } = mount()
    const editor = attachEditor()
    const scroller = scrollerOf(view)

    act(() => ref.current!.jumpToLine(1))
    act(() => ref.current!.jumpToLine(120))
    act(() => ref.current!.jumpToLine(400))

    const lines = editor.of('focus').map(c => (c.args[0] as { lineNumber: number }).lineNumber)
    expect(lines).toEqual([1, 120, 400])
    // Nothing hand-rolled a scroll offset on the host.
    expect(scroller.scrollTop).toBe(0)
  })

  it('renders the code inside a Virtualizer that owns the scroll container', () => {
    // Without a Virtualizer ancestor Pierre silently renders a DOM row per
    // source line, so this pins the provider's presence AND that the scroll
    // classes sit on it — Pierre listens for scroll on its own root, so a
    // scroller anywhere else would leave the window frozen at the first page.
    const { view } = mount()

    const virtualizer = view.container.querySelector('[data-testid="pierre-virtualizer"]')
    expect(virtualizer).not.toBeNull()
    expect(virtualizer!.className).toContain('overflow-auto')
    expect(scrollerOf(view)).toBe(virtualizer)
  })

  it('selects the whole span for a cited line range', () => {
    // `path:2410-2465` must highlight every cited row. The reveal still targets
    // the START (direction 'none'), so the view lands on 2410 rather than
    // jumping to the end of the span.
    const { ref } = mount()
    const editor = attachEditor()

    act(() => ref.current!.jumpToLine(2410, 2465))

    expect(editor.of('focus').map(c => c.args[0])).toEqual([{ lineNumber: 2410 }])
    expect(editor.of('setSelections')[0].args[0]).toEqual([
      {
        start: { line: 2409, character: 0 },
        end: { line: 2464, character: Number.MAX_SAFE_INTEGER },
        direction: 'none',
      },
    ])
  })

  it('ignores an end line that does not extend the span', () => {
    const { ref } = mount()
    const editor = attachEditor()

    act(() => ref.current!.jumpToLine(40, 40))
    act(() => ref.current!.jumpToLine(40, 12))

    expect(editor.of('setSelections')).toHaveLength(0)
    expect(editor.of('focus')).toHaveLength(2)
  })

  it('replays a jump requested before the editor attached', () => {
    // The panel publishes this handle on mount and fires its reveal effect on
    // that commit, but Pierre binds its editor later. The request must survive
    // the gap: the caller consumes its reveal nonce on the call, so a dropped
    // jump opens the file at the top and never corrects itself.
    const { ref } = mount()

    expect(() => {
      act(() => ref.current!.jumpToLine(765))
      act(() => ref.current!.focus())
    }).not.toThrow()
    expect(pierre.editors).toHaveLength(0)

    const editor = attachEditor()

    expect(editor.of('focus').map(c => c.args[0])).toEqual([{ lineNumber: 765 }])
  })

  it('replays a queued range, not just its first line', () => {
    // A cold open is the common case for a citation, so the span must survive
    // the queue too.
    const { ref } = mount()
    act(() => ref.current!.jumpToLine(2410, 2465))

    const editor = attachEditor()

    expect(editor.of('focus').map(c => c.args[0])).toEqual([{ lineNumber: 2410 }])
    expect(editor.of('setSelections')[0].args[0]).toEqual([
      {
        start: { line: 2409, character: 0 },
        end: { line: 2464, character: Number.MAX_SAFE_INTEGER },
        direction: 'none',
      },
    ])
  })

  it('replays the pending jump only once', () => {
    const { ref } = mount()
    act(() => ref.current!.jumpToLine(120))
    const editor = attachEditor()
    expect(editor.of('focus')).toHaveLength(1)

    // A later plain focus() must not re-run the stale jump.
    act(() => ref.current!.focus())

    expect(editor.of('focus').map(c => c.args[0])).toEqual([{ lineNumber: 120 }, undefined])
  })

  it('forwards focus() to the attached editor', () => {
    const { ref } = mount()
    const editor = attachEditor()

    act(() => ref.current!.focus())

    expect(editor.of('focus')).toHaveLength(1)
    expect(editor.of('setSelections')).toHaveLength(0)
  })
})
