/**
 * The block-assembler → CodeBlock sourcepos handoff: when the renderer runs
 * with `sourcePos` (the artifact page), a code block receives the 1-based
 * source line of its first CONTENT line as `sourceStartLine` — the handle
 * `CodeCommentContext` routing keys on. Without `sourcePos` (chat), the prop
 * must stay absent so commenting stays inert there.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({ received: [] as (number | undefined)[] }))

vi.mock('../components/CodeBlock', () => ({
  CodeBlock: ({ sourceStartLine }: { sourceStartLine?: number }) => {
    hoisted.received.push(sourceStartLine)
    return <div data-testid="code-block" />
  },
}))

import MarkdownRenderer from '../components/MarkdownRenderer'

const DOC = 'Intro paragraph.\n\n```java\nclass Foo {}\n```\n'

beforeEach(() => { hoisted.received.length = 0; cleanup() })

describe('MarkdownRenderer code-block sourcepos handoff', () => {
  it('passes the first-content-line number to CodeBlock when sourcePos is enabled', async () => {
    render(<MarkdownRenderer content={DOC} sourcePos />)
    await vi.waitFor(() => expect(hoisted.received.length).toBeGreaterThan(0))
    expect(hoisted.received).toEqual([4])
  })

  it('passes no sourceStartLine without sourcePos (chat surfaces)', async () => {
    render(<MarkdownRenderer content={DOC} />)
    await vi.waitFor(() => expect(hoisted.received.length).toBeGreaterThan(0))
    expect(hoisted.received).toEqual([undefined])
  })
})
