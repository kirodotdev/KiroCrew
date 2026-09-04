import { describe, it, expect } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { renderUserContent } from '../pages/ChatPage'

const noop = () => {}

describe('renderUserContent — markdown rendering', () => {
  it('renders bold text via markdown', () => {
    const { container } = render(<>{renderUserContent({ content: 'hello **bold** world', meta: undefined, onFileOpen: noop })}</>)
    expect(container.querySelector('strong')).toHaveTextContent('bold')
  })

  it('renders italic text via markdown', () => {
    const { container } = render(<>{renderUserContent({ content: 'hello *italic* world', meta: undefined, onFileOpen: noop })}</>)
    expect(container.querySelector('em')).toHaveTextContent('italic')
  })

  it('renders inline code via markdown', () => {
    const { container } = render(<>{renderUserContent({ content: 'use `npm install`', meta: undefined, onFileOpen: noop })}</>)
    expect(container.querySelector('code')).toHaveTextContent('npm install')
  })

  it('renders links via markdown', () => {
    const { container } = render(<>{renderUserContent({ content: 'see [docs](https://example.com)', meta: undefined, onFileOpen: noop })}</>)
    const link = container.querySelector('a')
    expect(link).toHaveTextContent('docs')
    expect(link).toHaveAttribute('href', 'https://example.com')
  })

  it('renders unordered lists via markdown', () => {
    const { container } = render(<>{renderUserContent({ content: '- item one\n- item two', meta: undefined, onFileOpen: noop })}</>)
    const items = container.querySelectorAll('li')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('item one')
  })

  it('renders code blocks via markdown', async () => {
    const { container } = render(<>{renderUserContent({ content: '```js\nconst x = 1\n```', meta: undefined, onFileOpen: noop })}</>)
    const pre = container.querySelector('pre')
    expect(pre).toBeInTheDocument()
    // Pierre owns the block's internals and fills them after mount, so assert the
    // rendered TEXT rather than a `<code>` element its markup does not emit.
    await waitFor(() => expect(pre).toHaveTextContent('const x = 1'))
  })

  it('renders plain text without extra wrapping issues', () => {
    const { container } = render(<>{renderUserContent({ content: 'just plain text', meta: undefined, onFileOpen: noop })}</>)
    expect(container).toHaveTextContent('just plain text')
  })

  it('renders image markdown', () => {
    const { container } = render(<>{renderUserContent({ content: '![alt](/path/to/img.png)', meta: undefined, onFileOpen: noop })}</>)
    const img = container.querySelector('img')
    expect(img).toBeInTheDocument()
    // MarkdownRenderer rewrites local paths to /api/file-raw?path=<encoded>
    expect(decodeURIComponent(img?.getAttribute('src') || '')).toContain('/path/to/img.png')
  })

  it('renders file chips for attached files with markdown in surrounding text', () => {
    const content = '[attached_file 1] /home/user/file.ts\ncheck this **bold** text'
    const { container } = render(<>{renderUserContent({ content: content, meta: undefined, onFileOpen: noop })}</>)
    // File chip rendered
    expect(container.querySelector('[title="/home/user/file.ts"]')).toBeInTheDocument()
    // Markdown in remaining text
    expect(container.querySelector('strong')).toHaveTextContent('bold')
  })

  it('renders a file card for a standalone upload and never leaks raw [attached_file token text', () => {
    // Empty-caption upload persists as a standalone token line + meta.files.
    const content = '[attached_file 1] /home/user/uploads/x_CHANGE_PCT.docx'
    const meta = { files: ['/home/user/uploads/x_CHANGE_PCT.docx'] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: noop })}</>)
    const card = container.querySelector('.flex-col [title="/home/user/uploads/x_CHANGE_PCT.docx"]')
    expect(card).toBeInTheDocument()
    expect(card).toHaveTextContent('x_CHANGE_PCT.docx')
    expect(container.textContent).not.toContain('[attached_file')
  })

  it('renders one card per attachment for a multi-file standalone upload', () => {
    const content = [
      '[attached_file 1] /home/user/uploads/a_CHANGE_PCT.docx',
      '[attached_file 2] /home/user/uploads/b_CHANGE_AMT.docx',
      '[attached_file 3] /home/user/uploads/c_MONTHLY_RT.docx',
    ].join('\n')
    const meta = { files: [
      '/home/user/uploads/a_CHANGE_PCT.docx',
      '/home/user/uploads/b_CHANGE_AMT.docx',
      '/home/user/uploads/c_MONTHLY_RT.docx',
    ] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: noop })}</>)
    expect(container.querySelector('[title$="a_CHANGE_PCT.docx"]')).toBeInTheDocument()
    expect(container.querySelector('[title$="b_CHANGE_AMT.docx"]')).toBeInTheDocument()
    expect(container.querySelector('[title$="c_MONTHLY_RT.docx"]')).toBeInTheDocument()
    expect(container.textContent).not.toContain('[attached_file')
  })

  it('calls onFileOpen with the full path when a file card is clicked', () => {
    let opened = ''
    const onOpen = (p: string) => { opened = p }
    const content = '[attached_file 1] /home/user/report.docx'
    const meta = { files: ['/home/user/report.docx'] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: onOpen })}</>)
    const card = container.querySelector('.flex-col [title="/home/user/report.docx"]') as HTMLElement
    expect(card).toBeInTheDocument()
    card.click()
    expect(opened).toBe('/home/user/report.docx')
  })

  it('keeps an inline @-mentioned file as an inline chip, not a block card', () => {
    // Fresh message: caption @-mentions the file inline; meta.files carries the path.
    const content = 'check @report.docx please'
    const meta = { files: ['/home/user/report.docx'] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: noop })}</>)
    const chip = container.querySelector('.inline-flex[title="/home/user/report.docx"]')
    expect(chip).toBeInTheDocument()
    expect(chip).toHaveTextContent('@report.docx')
    // Not a block card (block card uses flex-col container, not inline-flex).
    expect(container.querySelector('.flex-col [title="/home/user/report.docx"]')).not.toBeInTheDocument()
  })

  it('keeps a mentioned file inline when the persisted shape carries BOTH token content and meta.files', () => {
    // This is the real completed-bubble shape the server persists: content is
    // the LLM-facing [attached_file N] token form AND meta.files is present.
    // Regression guard: the file must render as an inline chip, not a card.
    const content = 'this is [attached_file 1] /home/user/triage/hcm-ozone.csv file'
    const meta = { files: ['/home/user/triage/hcm-ozone.csv'] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: noop })}</>)
    const chip = container.querySelector('.inline-flex[title="/home/user/triage/hcm-ozone.csv"]')
    expect(chip).toBeInTheDocument()
    expect(container.querySelector('.flex-col [title="/home/user/triage/hcm-ozone.csv"]')).not.toBeInTheDocument()
    // Surrounding caption text is preserved.
    expect(container.textContent).toContain('this is')
    expect(container.textContent).toContain('file')
    expect(container.textContent).not.toContain('[attached_file')
    // Caption text and chip share ONE inline flow — no block <p> wrapping the
    // text runs (which would break the line around the chip).
    expect(container.querySelector('p')).not.toBeInTheDocument()
  })

  it('resolves a spaced filename losslessly via the token index (not truncated at the space)', () => {
    // meta.files carries the real path with a space; the [attached_file N] token
    // number is the 1-based index into it. The chip target must be the FULL path.
    const content = 'see [attached_file 1] /home/user/Q2 Report.docx here'
    const meta = { files: ['/home/user/Q2 Report.docx'] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: noop })}</>)
    const chip = container.querySelector('[title="/home/user/Q2 Report.docx"]')
    expect(chip).toBeInTheDocument()
    // The truncated path must NOT appear as a click target.
    expect(container.querySelector('[title="/home/user/Q2"]')).not.toBeInTheDocument()
    expect(container.textContent).not.toContain('[attached_file')
  })

  it('renders exactly one card for a standalone upload (no duplication)', () => {
    const content = 'here is the file\n[attached_file 1] /home/user/data.csv'
    const meta = { files: ['/home/user/data.csv'] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: noop })}</>)
    const refs = container.querySelectorAll('[title="/home/user/data.csv"]')
    expect(refs.length).toBe(1)
    expect(container.textContent).not.toContain('[attached_file')
  })

  it('resolves a spaced filename correctly even when an image precedes it (original-list index)', () => {
    // token N=2 must index the ORIGINAL list (image at 0, doc at 1), and the
    // spaced path must resolve in full — not truncate at the space.
    const content = '![image](/home/user/pic.png)\n\nsee [attached_file 2] /home/user/Q2 Report.docx here'
    const meta = { files: ['/home/user/pic.png', '/home/user/Q2 Report.docx'] }
    const { container } = render(<>{renderUserContent({ content: content, meta: meta, onFileOpen: noop })}</>)
    expect(container.querySelector('[title="/home/user/Q2 Report.docx"]')).toBeInTheDocument()
    expect(container.querySelector('[title="/home/user/Q2"]')).not.toBeInTheDocument()
    expect(container.textContent).not.toContain('[attached_file')
  })
})
