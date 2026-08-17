/**
 * Tests for syntax colouring of fenced code in the Notes app.
 *
 * Three halves, one per way the feature can be wrong: the fence's language
 * parsed from the wrong part of the info string, colour applied to a block whose
 * author never asked for one, and the highlighted HTML reaching the DOM without
 * being sanitized or losing the click-to-edit contract.
 *
 * `highlightAsync` is mocked throughout: the real one needs a Web Worker, and
 * what matters here is what this app asks of it and what it does with the reply.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { fenceLang } from '../apps/md-notebook/utils'
import { Preview } from '../apps/md-notebook/Preview'

const highlightAsync = vi.hoisted(() => vi.fn<(code: string, lang?: string) => Promise<string>>())
vi.mock('../utils/highlightClient', () => ({ highlightAsync }))

function renderPreview(content: string, onStartEdit = vi.fn()) {
  render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  return onStartEdit
}

function fence(lang: string, ...body: string[]): string {
  return ['```' + lang, ...body, '```'].join('\n')
}

beforeEach(() => {
  highlightAsync.mockReset()
  highlightAsync.mockResolvedValue('')
})

describe('md-notebook/fenceLang', () => {
  it('reads the language a fence names', () => {
    expect(fenceLang('```python')).toBe('python')
  })

  it('lowercases it, because grammars are keyed in lower case', () => {
    expect(fenceLang('```JS')).toBe('js')
  })

  it('keeps only the first word of the info string', () => {
    // CommonMark allows the rest to carry anything; highlight.js keys on word 1.
    expect(fenceLang('```js title="server.js"')).toBe('js')
    expect(fenceLang('```mermaid {1-3}')).toBe('mermaid')
  })

  it('has no language for a bare fence', () => {
    expect(fenceLang('```')).toBeUndefined()
    expect(fenceLang('```   ')).toBeUndefined()
  })

  it('reads a fence that is indented, or longer than three backticks', () => {
    expect(fenceLang('  ```ts')).toBe('ts')
    expect(fenceLang('````rust')).toBe('rust')
  })

  it('caps the length, so a note cannot probe the grammar registry', () => {
    expect(fenceLang('```' + 'a'.repeat(40))).toHaveLength(20)
  })
})

describe('md-notebook/Preview fenced code', () => {
  it('asks for colour with the language the fence named', async () => {
    renderPreview(fence('python', 'def f():', '    return 1'))
    await waitFor(() => expect(highlightAsync).toHaveBeenCalledTimes(1))
    expect(highlightAsync).toHaveBeenCalledWith('def f():\n    return 1', 'python')
  })

  it('leaves an unlabelled block alone rather than guessing a grammar', async () => {
    // A note's bare fence is usually a log or a tree; guessing paints arbitrary
    // words as keywords, so the highlighter is never asked in the first place.
    renderPreview(fence('', 'ERROR  connection reset', 'INFO   retrying'))
    await waitFor(() => expect(screen.getByText(/connection reset/)).toBeTruthy())
    expect(highlightAsync).not.toHaveBeenCalled()
  })

  it('shows the code as plain text until the colours arrive', () => {
    // Never a blank box: the worker can take up to its timeout to reply.
    highlightAsync.mockReturnValue(new Promise(() => {}))
    renderPreview(fence('ts', 'const x = 1'))
    expect(screen.getByText('const x = 1')).toBeTruthy()
  })

  it('swaps in the highlighted markup once it arrives', async () => {
    // Asserted on the `code.hljs` wrapper, which only the highlighted branch
    // renders, not on the `.hljs-*` spans inside it: DOMPurify under happy-dom
    // flattens markup to text, so those spans cannot be observed here. The
    // colours themselves are evidenced by the Playwright frames.
    highlightAsync.mockResolvedValue('<span class="hljs-keyword">const</span> x = 1')
    renderPreview(fence('ts', 'const x = 1'))
    await waitFor(() => expect(document.querySelector('code.hljs')).not.toBeNull())
    expect(document.querySelector('code.hljs')?.textContent).toContain('const')
  })

  it('never lets an event-handler attribute through to the DOM', async () => {
    // The HTML derives from note text, so it stays untrusted even though
    // highlightAsync sanitizes its own output; this pins the sink's own pass.
    highlightAsync.mockResolvedValue('<img src=x onerror="alert(1)">ok')
    renderPreview(fence('ts', 'x'))
    await waitFor(() => expect(document.querySelector('code.hljs')).not.toBeNull())
    expect(document.querySelector('[onerror]')).toBeNull()
    expect(document.querySelector('code.hljs img')).toBeNull()
  })

  it('keeps click-to-edit on a coloured block', async () => {
    highlightAsync.mockResolvedValue('<span class="hljs-keyword">def</span> f()')
    const onStartEdit = renderPreview(['Intro', '', fence('python', 'def f()')].join('\n'))
    await waitFor(() => expect(document.querySelector('code.hljs')).not.toBeNull())
    await userEvent.click(document.querySelector('code.hljs') as HTMLElement)
    // The fence's own lines, so the editor opens the SOURCE and not the colours.
    expect(onStartEdit).toHaveBeenCalledWith(2, 4)
  })
})
