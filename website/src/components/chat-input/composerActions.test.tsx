import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import type { ComposerActionHostOptions as InvocationFrameOptions } from '../../composerActions/composerActionHost'
import type { ComposerActionItem } from '../../composerActions/composerActionRegistry'
import type { PasteBlock } from '../../utils/pasteTokens'
import { ComposerActionMenuItems, useComposerActions } from './composerActions'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../ui/dropdown-menu'

// The frame host is tested in composerActions/composerActionHost.test.ts; here it is a
// stand-in that records its options so the test can answer as the app.
const frames: Array<{ opts: InvocationFrameOptions; accept: ReturnType<typeof vi.fn>; refuse: ReturnType<typeof vi.fn>; cancel: ReturnType<typeof vi.fn> }> = []
vi.mock('../../composerActions/composerActionHost', () => ({
  ComposerActionHost: class {
    accept = vi.fn()
    refuse = vi.fn()
    cancel = vi.fn()
    constructor(opts: InvocationFrameOptions) { frames.push({ opts, accept: this.accept, refuse: this.refuse, cancel: this.cancel }) }
    start() {}
  },
}))

// The icon needs the theme provider; the rows' own text is what these tests read.
vi.mock('../AppIcon', () => ({ default: () => null }))

const item: ComposerActionItem = { key: 'snip:insert', appName: 'snip', appLabel: 'Snippets', id: 'insert', title: 'Insert snippet', icon: '', width: 360, maxHeight: 320 }

let api: { start: (i: ComposerActionItem) => void; value: string; setValue: (v: string) => void; setBlocks: (b: PasteBlock[]) => void } | null = null

function Host({ items = [item], scope = 's1', initial = '', locked = false }: { items?: ComposerActionItem[]; scope?: string; initial?: string; locked?: boolean }) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState<PasteBlock[]>([])
  const actions = useComposerActions({
    items, value, onChange: setValue, pasteBlocks: blocks,
    composerControl: () => ({ focus() {}, getRootElement: () => null, getSelection: () => null, setSelection() {} }),
    scopeKey: scope, locked, anchor: () => null,
  })
  api = { start: actions.start, value, setValue, setBlocks }
  return <>{actions.popover}<output data-testid="draft">{value}</output></>
}

beforeEach(() => { frames.length = 0 })
afterEach(() => { cleanup(); api = null })

const notice = () => screen.queryByTestId('composer-action-hint')?.textContent ?? null

describe('useComposerActions', () => {
  it('hands the app nothing from the composer', () => {
    render(<Host />)
    act(() => api!.start(item))
    expect(frames).toHaveLength(1)
    expect(Object.keys(frames[0].opts).sort()).toEqual(['actionId', 'appName', 'container', 'deadlineMs', 'onEnd', 'onEscape', 'onResize', 'onText', 'theme'])
  })

  it('puts plain text into an empty composer as ordinary draft text', () => {
    render(<Host />)
    act(() => api!.start(item))
    let verdict: string | undefined
    act(() => { verdict = frames[0].opts.onText('Standup notes for today') })
    expect(verdict).toBeUndefined()
    expect(api!.value).toBe('Standup notes for today')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([
    ['a slash command', '/goal clear'],
    ['a command on a later line', 'notes\n/goal clear'],
    ['a terminal command', '! echo hello'],
    ['an @prompt', '@prompt standup'],
    ['a $skill', 'run $deploy'],
    ['an image path', 'see /tmp/photo.png'],
    ['a staged-paste token', 'see [ Paste #1 · 3 lines ] here'],
  ])('refuses %s with a visible reason and inserts nothing', (_name, text) => {
    render(<Host />)
    act(() => api!.start(item))
    let verdict: string | undefined
    act(() => { verdict = frames[0].opts.onText(text) })
    expect(verdict).toBe('not_literal')
    expect(api!.value).toBe('')
    expect(notice()).toMatch(/Snippets's text starts with \/, ! or @/)
    // The frame stays, so the app can offer other text.
    expect(frames[0].cancel).not.toHaveBeenCalled()
  })

  it('shows a refusal as a plain hint, never as an alert', () => {
    render(<Host initial="hello" />)
    act(() => api!.start(item))
    expect(screen.getByTestId('composer-action-hint').getAttribute('role')).toBe('status')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('makes no frame when the composer already holds a draft, and says why', () => {
    render(<Host initial="hello" />)
    act(() => api!.start(item))
    expect(frames).toHaveLength(0)
    expect(notice()).toMatch(/Snippets can only insert into an empty message/)
    expect(api!.value).toBe('hello')
  })

  it('counts a collapsed paste as a draft', () => {
    render(<Host />)
    act(() => api!.setBlocks([{ id: 'p1', n: 1, text: 'a\nb\nc', lines: 3 } as unknown as PasteBlock]))
    act(() => api!.start(item))
    expect(frames).toHaveLength(0)
  })

  it('refuses when the person typed while the app worked: never mixes the draft', () => {
    render(<Host />)
    act(() => api!.start(item))
    act(() => api!.setValue('/goal\n'))
    let verdict: string | undefined
    act(() => { verdict = frames[0].opts.onText('clear') })
    expect(verdict).toBe('not_empty')
    expect(api!.value).toBe('/goal\n')
    expect(notice()).toMatch(/only insert into an empty message/)
  })

  it('cancels when the composer moves to another session', () => {
    const { rerender } = render(<Host scope="s1" />)
    act(() => api!.start(item))
    rerender(<Host scope="s2" />)
    expect(frames[0].cancel).toHaveBeenCalledWith('cancelled')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('cancels when another tab takes the typing lock, and never writes the draft', () => {
    const { rerender } = render(<Host />)
    act(() => api!.start(item))
    const { onText } = frames[0].opts
    rerender(<Host locked />)
    expect(frames[0].cancel).toHaveBeenCalledWith('cancelled')
    expect(screen.queryByRole('dialog')).toBeNull()
    // An offer already on its way lands nowhere.
    let verdict: string | undefined
    act(() => { verdict = onText('Standup notes') })
    expect(verdict).toBe('stale')
    expect(api!.value).toBe('')
  })

  it('opens nothing while the composer is typing-locked', () => {
    render(<Host locked />)
    act(() => api!.start(item))
    expect(frames).toHaveLength(0)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('cancels when the action is revoked', () => {
    const { rerender } = render(<Host items={[item]} />)
    act(() => api!.start(item))
    rerender(<Host items={[]} />)
    expect(frames[0].cancel).toHaveBeenCalledWith('revoked')
  })

  it('never grows taller than the room above its anchor', () => {
    render(<Host />)
    act(() => api!.start(item))
    act(() => frames[0].opts.onResize(600))
    // No anchor: the popover sits 96px from the bottom, 8px from the top.
    expect(screen.getByRole('dialog').style.maxHeight).toBe(`${window.innerHeight - 104}px`)
  })

  it('says what happened when the app fails', () => {
    render(<Host />)
    act(() => api!.start(item))
    act(() => frames[0].opts.onEnd('deadline'))
    expect(screen.getByRole('dialog').textContent).toMatch(/Snippets took too long/)
    // Through the shared error surface, as an alert, not a plain status line.
    expect(screen.getByTestId('composer-action-ended').textContent).toMatch(/Snippets took too long/)
    expect(screen.getByRole('alert').textContent).toMatch(/Snippets took too long/)
  })
})

describe('ComposerActionMenuItems', () => {
  it('lists each action as an item of the touch overflow menu and starts it on select', async () => {
    const onPick = vi.fn()
    render(
      <DropdownMenu open>
        <DropdownMenuTrigger>more</DropdownMenuTrigger>
        <DropdownMenuContent><ComposerActionMenuItems items={[item]} onPick={onPick} /></DropdownMenuContent>
      </DropdownMenu>,
    )
    const row = screen.getByRole('menuitem', { name: /Insert snippet/ })
    expect(row.textContent).toContain('Snippets')
    act(() => { fireEvent.click(row) })
    expect(onPick).toHaveBeenCalledWith(item)
  })

  it('items carry no restyle classes on the menu primitive itself', () => {
    render(
      <DropdownMenu open>
        <DropdownMenuTrigger>more</DropdownMenuTrigger>
        <DropdownMenuContent><ComposerActionMenuItems items={[item]} onPick={() => {}} /></DropdownMenuContent>
      </DropdownMenu>,
    )
    expect(screen.getByRole('menuitem').getAttribute('data-composer-action-row')).toBe(item.key)
  })

  it('renders nothing without actions', () => {
    render(
      <DropdownMenu open>
        <DropdownMenuTrigger>more</DropdownMenuTrigger>
        <DropdownMenuContent><ComposerActionMenuItems items={[]} onPick={() => {}} /></DropdownMenuContent>
      </DropdownMenu>,
    )
    expect(screen.queryByRole('menuitem')).toBeNull()
  })
})
