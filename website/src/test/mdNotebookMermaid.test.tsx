/**
 * Mermaid blocks in the Notes app's markdown preview.
 *
 * Three properties pinned here:
 *  1. A CLOSED ```mermaid fence renders as a diagram, and clicking it opens
 *     the fenced SOURCE range — click-to-edit is the app's core gesture and
 *     an SVG must not break it.
 *  2. The block never renders LESS than the source: while the async chunk
 *     loads, and whenever the diagram is invalid, the source text stays
 *     visible (with a hint in the failure case). The feature can only add.
 *  3. Everything that is not a closed ```mermaid fence — other info-strings,
 *     unclosed fences — keeps the generic code-block behaviour byte for byte.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

import { Preview } from '../apps/md-notebook/Preview'
import { parseMermaidBlock } from '../apps/md-notebook/utils'
import { mermaidErrorLabel } from '../apps/md-notebook/labels'

const { renderMock, initializeMock } = vi.hoisted(() => ({
  renderMock: vi.fn(),
  initializeMock: vi.fn(),
}))

vi.mock('mermaid', () => ({
  default: { initialize: initializeMock, render: renderMock },
}))

/** Preview with spies; returns the onStartEdit spy for click assertions. */
function renderPreview(content: string) {
  const onStartEdit = vi.fn()
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

const DIAGRAM = '```mermaid\ngraph TD\nA-->B\n```'

describe('parseMermaidBlock', () => {
  it('parses a closed fence into code and end line', () => {
    expect(parseMermaidBlock(['```mermaid', 'A-->B', '```'], 0)).toEqual({
      code: 'A-->B',
      end: 2,
    })
  })

  it('accepts trailing whitespace on the opening line only', () => {
    expect(parseMermaidBlock(['```mermaid  ', 'A', '```'], 0)).not.toBeNull()
    expect(parseMermaidBlock(['```mermaidjs', 'A', '```'], 0)).toBeNull()
    expect(parseMermaidBlock(['```python', 'A', '```'], 0)).toBeNull()
  })

  it('returns null for an unclosed fence', () => {
    expect(parseMermaidBlock(['```mermaid', 'A-->B'], 0)).toBeNull()
  })

  it('parses an empty diagram', () => {
    expect(parseMermaidBlock(['```mermaid', '```'], 0)).toEqual({ code: '', end: 1 })
  })
})

describe('mermaid blocks in Preview', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    renderMock.mockResolvedValue({ svg: '<svg><text>diagram-ok</text></svg>' })
  })

  it('renders the SVG once mermaid resolves', async () => {
    renderPreview(DIAGRAM)
    await waitFor(() => expect(screen.getByText('diagram-ok')).toBeInTheDocument())
    expect(initializeMock).toHaveBeenCalledWith(
      expect.objectContaining({ securityLevel: 'strict', startOnLoad: false }),
    )
  })

  it('sanitizes the rendered SVG before it reaches the DOM', async () => {
    // The exact shape of the blocking review finding: mermaid's output is
    // treated as untrusted markup, so scripts and handlers must not survive
    // the trip into dangerouslySetInnerHTML even if the renderer emits them.
    renderMock.mockResolvedValue({
      svg: '<svg><text>safe label</text><script>window.pwned = true</script><g onload="window.pwned = true"><text>styled</text></g></svg>',
    })
    renderPreview(DIAGRAM)
    const label = await screen.findByText('safe label')
    const host = label.closest('div')
    expect(host?.innerHTML).not.toContain('script')
    expect(host?.innerHTML).not.toContain('onload')
    expect((window as unknown as { pwned?: boolean }).pwned).toBeUndefined()
  })

  it('keeps mermaid on plain SVG text labels, which survive sanitization', async () => {
    renderPreview(DIAGRAM)
    await screen.findByText('diagram-ok')
    // DOMPurify's default profile strips <foreignObject>, where mermaid puts
    // its HTML labels: htmlLabels must therefore stay off in both spellings.
    expect(initializeMock).toHaveBeenCalledWith(
      expect.objectContaining({ htmlLabels: false, flowchart: { htmlLabels: false } }),
    )
  })

  it('shows the source before the async render lands', () => {
    // A promise that never settles pins the loading state.
    renderMock.mockReturnValue(new Promise(() => {}))
    renderPreview(DIAGRAM)
    expect(screen.getByText(/graph TD/)).toBeInTheDocument()
    expect(screen.queryByText(mermaidErrorLabel())).not.toBeInTheDocument()
  })

  it('keeps the source visible and adds a hint when the diagram is invalid', async () => {
    renderMock.mockRejectedValue(new Error('parse error'))
    renderPreview('```mermaid\nnot a diagram\n```')
    await waitFor(() => expect(screen.getByText(mermaidErrorLabel())).toBeInTheDocument())
    expect(screen.getByText('not a diagram')).toBeInTheDocument()
  })

  it('opens the full fenced source range on click', async () => {
    const onStartEdit = renderPreview(`intro\n\n${DIAGRAM}\n\noutro`)
    const svgText = await screen.findByText('diagram-ok')
    fireEvent.click(svgText)
    // Lines: 0 intro, 1 blank, 2 fence-open, 3-4 body, 5 fence-close.
    expect(onStartEdit).toHaveBeenCalledWith(2, 5)
  })

  it('leaves other fenced blocks on the generic code path', () => {
    renderPreview('```python\nprint(1)\n```')
    expect(screen.getByText('print(1)')).toBeInTheDocument()
    expect(renderMock).not.toHaveBeenCalled()
  })

  it('treats an unclosed mermaid fence as a generic run-away fence', () => {
    renderPreview('before\n\n```mermaid\nA-->B')
    // Generic run-away fences swallow their lines to end of note (no flush
    // after the loop on main); the point pinned here is that mermaid neither
    // renders nor crashes, and the note before the fence is intact.
    expect(screen.getByText('before')).toBeInTheDocument()
    expect(screen.queryByText('A-->B')).not.toBeInTheDocument()
    expect(renderMock).not.toHaveBeenCalled()
  })

  it('renders lines after the diagram exactly once', async () => {
    renderPreview(`${DIAGRAM}\n\nafter`)
    await screen.findByText('diagram-ok')
    expect(screen.getAllByText('after')).toHaveLength(1)
  })

  it('strips external <image> and external href attributes from the SVG', async () => {
    // GPT 5.6 blocking finding: mermaid can produce <image href="http://evil">
    // via image-node syntax; DOMPurify keeps it, CSP permits HTTPS, so an
    // attacker-controlled request fires on note open. The sanitizeMermaidSvg
    // function uses DOMPurify hooks to strip these.
    renderMock.mockResolvedValue({
      svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>label</text><image href="https://evil.example.com/track.png" width="1" height="1"/><a href="https://evil.example.com/phish"><text>click me</text></a><use href="#local-def"/></svg>',
    })
    renderPreview(DIAGRAM)
    const label = await screen.findByText('label')
    const host = label.closest('div')!
    // External <image> is removed entirely
    expect(host.innerHTML).not.toContain('evil.example.com')
    expect(host.innerHTML).not.toContain('<image')
    // External href on <a> is stripped
    expect(host.innerHTML).not.toContain('href="https://')
    // Internal fragment references (#id) are preserved
    expect(host.innerHTML).toContain('#local-def')
  })

  it('strips CSS url() references to external origins', async () => {
    // GPT 5.6 rounds 2+3: classDef can inject fill:url(https://...) or
    // protocol-relative fill:url(//...) via mermaid's style system.
    renderMock.mockResolvedValue({
      svg: '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://attacker.example/pixel)"/><rect style="fill:url(//attacker.example/track)"/><rect style="fill:url(#local-gradient)"/><text>node</text></svg>',
    })
    renderPreview(DIAGRAM)
    const node = await screen.findByText('node')
    const host = node.closest('div')!
    expect(host.innerHTML).not.toContain('attacker.example')
    expect(host.innerHTML).not.toContain('https://')
    expect(host.innerHTML).not.toContain('//attacker')
    // Local fragment references in url() are preserved
    expect(host.innerHTML).toContain('#local-gradient')
  })

  it('strips CSS image-set(), image(), and src() fetch functions to external origins', async () => {
    // GPT 5.6 blocking: classDef with mask-image:image-set('https://attacker/pixel' 1x)
    // or background:image('https://...') bypasses url()-only stripping.
    renderMock.mockResolvedValue({
      svg: '<svg xmlns="http://www.w3.org/2000/svg"><rect style="mask-image:image-set(\'https://attacker.example/pixel\' 1x)"/><rect style="background:image(\'https://attacker.example/track\')"/><rect style="border-image:src(\'https://attacker.example/exfil\')"/><rect style="fill:url(#safe-ref)"/><text>secure</text></svg>',
    })
    renderPreview(DIAGRAM)
    const node = await screen.findByText('secure')
    const host = node.closest('div')!
    expect(host.innerHTML).not.toContain('attacker.example')
    expect(host.innerHTML).not.toContain('image-set')
    expect(host.innerHTML).not.toContain('image(')
    expect(host.innerHTML).not.toContain('src(')
    // Local fragment references are still preserved
    expect(host.innerHTML).toContain('#safe-ref')
  })
})
