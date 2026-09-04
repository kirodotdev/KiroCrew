/**
 * CodeBlock's line-anchored commenting wiring: with a mounted
 * `CodeCommentContext` and a `sourceStartLine`, the block hands Pierre the
 * interaction options (line selection + gutter utility), routes the gutter
 * click into `onCommentRange` with the hovered line / selected range, and
 * renders routed threads as annotation chips that activate on click. Without
 * a context or a sourcepos it must stay exactly the inert block it was —
 * chat and previews never see any of this.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import type { LineAnnotation, SelectedLineRange } from '@pierre/diffs'

interface CapturedProps {
  options?: { enableLineSelection?: boolean; onLineSelected?: (r: SelectedLineRange | null) => void; enableGutterUtility?: boolean }
  lineAnnotations?: LineAnnotation<unknown>[]
  renderAnnotation?: (a: LineAnnotation<unknown>) => React.ReactNode
  renderGutterUtility?: (getHoveredLine: () => { lineNumber: number } | undefined) => React.ReactNode
}

const hoisted = vi.hoisted(() => ({ captured: [] as CapturedProps[] }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  // Capture the props CodeBlock hands Pierre, and render the gutter +
  // annotation slots into light DOM so the test can click them.
  PierreCode: (props: CapturedProps) => {
    hoisted.captured.push(props)
    return (
      <div data-testid="pierre-code">
        {props.renderGutterUtility?.(() => ({ lineNumber: 2 }))}
        {props.lineAnnotations?.map((a, i) => (
          <div key={i} data-testid="annotation-slot">{props.renderAnnotation?.(a)}</div>
        ))}
      </div>
    )
  },
}))

import { CodeBlock } from '../components/CodeBlock'
import { CodeCommentContext, type CodeCommentContextValue, type CodeLineAnnotationMeta } from '../components/codeComments'

const CODE = 'line one\nline two\nline three'

function ctxValue(over?: Partial<CodeCommentContextValue> & { annotations?: CodeLineAnnotationMeta[] }): CodeCommentContextValue & { onCommentRange: ReturnType<typeof vi.fn>; onActivate: ReturnType<typeof vi.fn> } {
  const annotations = over?.annotations ?? []
  return {
    annotationsFor: vi.fn(() => annotations),
    onCommentRange: vi.fn(),
    activeId: null,
    onActivate: vi.fn(),
    ...over,
  } as never
}

function mount(ctx: CodeCommentContextValue | null, sourceStartLine?: number) {
  hoisted.captured.length = 0
  return render(
    <CodeCommentContext.Provider value={ctx}>
      <CodeBlock code={CODE} lang="txt" complete sourceStartLine={sourceStartLine} />
    </CodeCommentContext.Provider>,
  )
}

beforeEach(() => cleanup())

describe('CodeBlock commenting wiring', () => {
  it('stays inert without a context (chat) and without a sourcepos (no source mapping)', () => {
    mount(null, 3)
    let p = hoisted.captured.at(-1)!
    expect(p.options).toBeUndefined()
    expect(p.renderGutterUtility).toBeUndefined()
    expect(p.lineAnnotations).toBeUndefined()
    cleanup()
    mount(ctxValue(), undefined)
    p = hoisted.captured.at(-1)!
    expect(p.options).toBeUndefined()
    expect(p.renderGutterUtility).toBeUndefined()
  })

  it('enables line selection + gutter utility when commentable', () => {
    mount(ctxValue(), 3)
    const p = hoisted.captured.at(-1)!
    expect(p.options?.enableLineSelection).toBe(true)
    expect(p.options?.enableGutterUtility).toBe(true)
    expect(typeof p.renderGutterUtility).toBe('function')
  })

  it('gutter click comments on the hovered line', () => {
    const ctx = ctxValue()
    mount(ctx, 3)
    fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))
    expect(ctx.onCommentRange).toHaveBeenCalledWith(
      expect.objectContaining({ blockStartLine: 3, start: 2, end: 2 }),
    )
  })

  it('gutter click comments on the selected RANGE when the hovered line is inside it', () => {
    const ctx = ctxValue()
    mount(ctx, 3)
    // Pierre reports a drag-selection of lines 1..3 (hovered line 2 is inside).
    act(() => hoisted.captured.at(-1)!.options!.onLineSelected!({ start: 3, end: 1 } as SelectedLineRange))
    fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))
    expect(ctx.onCommentRange).toHaveBeenCalledWith(
      expect.objectContaining({ blockStartLine: 3, start: 1, end: 3 }),
    )
  })

  it('renders routed threads as annotation chips and activates on click', () => {
    const ctx = ctxValue({ annotations: [{ id: 'c9', line: 2, count: 3, unread: false }] })
    mount(ctx, 3)
    const p = hoisted.captured.at(-1)!
    expect(p.lineAnnotations).toEqual([{ lineNumber: 2, metadata: { id: 'c9', line: 2, count: 3, unread: false } }])
    fireEvent.click(screen.getByRole('button', { name: 'Comment thread (3)' }))
    expect(ctx.onActivate).toHaveBeenCalledWith('c9')
  })
})
