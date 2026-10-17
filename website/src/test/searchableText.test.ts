import { searchableText, searchableTextMemo } from '../utils/searchableText'
import type { ChatMessage } from '../types'

const msg = (role: string, content: string): ChatMessage => ({ role, content, cls: '' })

describe('searchableText', () => {
  it('strips a trailing [OPTIONS:] block from assistant content', () => {
    const t = searchableText(msg('assistant', 'visible body\n\n[OPTIONS: a | b]'))
    expect(t).toBe('visible body')
    expect(t).not.toContain('OPTIONS')
  })

  it('strips <mcwidget> blocks (iframe content the highlighter cannot reach)', () => {
    const t = searchableText(msg('assistant', 'before widget <mcwidget title="x"><div>Green status</div></mcwidget> after widget'))
    expect(t).toContain('before widget')
    expect(t).toContain('after widget')
    expect(t).not.toContain('Green status')
    expect(t).not.toContain('mcwidget')
  })

  it('strips multiple widgets', () => {
    const t = searchableText(msg('assistant', '<mcwidget>one</mcwidget> mid <mcwidget>two</mcwidget>'))
    expect(t).not.toContain('one')
    expect(t).not.toContain('two')
    expect(t).toContain('mid')
  })

  // False-negative guards: highlightable content MUST be preserved.
  it('preserves fenced code blocks (highlightable in the body)', () => {
    const src = 'see this:\n```ts\nconst replication = 1\n```\ndone'
    expect(searchableText(msg('assistant', src))).toContain('const replication = 1')
  })

  it('strips a trailing keep-visible marker (renders as nothing — a match would be phantom, #7948)', () => {
    const t = searchableText(msg('assistant', 'substantive report\n\n<!-- keep-visible -->'))
    expect(t).toBe('substantive report')
    expect(t).not.toContain('keep-visible')
  })

  it('preserves a keep-visible marker quoted inside fenced code (visible content, tail-anchored regex, #7948)', () => {
    const src = 'the marker looks like:\n```html\n<!-- keep-visible -->\n```\nend of message'
    expect(searchableText(msg('assistant', src))).toContain('<!-- keep-visible -->')
  })

  it('preserves prose and inline code', () => {
    const src = 'The `useMessageSearch` hook scans messages.'
    expect(searchableText(msg('assistant', src))).toBe(src)
  })

  it('leaves non-assistant content untouched', () => {
    const src = 'user typed [OPTIONS: x] literally and <mcwidget>y</mcwidget>'
    expect(searchableText(msg('user', src))).toBe(src)
  })
})

describe('searchableTextMemo', () => {
  it('matches searchableText output', () => {
    const m = msg('assistant', 'body\n\n[OPTIONS: a | b]')
    expect(searchableTextMemo(m)).toBe(searchableText(m))
  })

  it('returns a cached value for the same message object (same reference)', () => {
    const m = msg('assistant', 'hello <mcwidget>w</mcwidget>')
    const first = searchableTextMemo(m)
    expect(searchableTextMemo(m)).toBe(first) // referentially identical cached string
  })

  it('recomputes when the same object\u2019s content changes (streaming mutation)', () => {
    const m = msg('assistant', 'partial')
    expect(searchableTextMemo(m)).toBe('partial')
    m.content = 'partial more [OPTIONS: x]'
    expect(searchableTextMemo(m)).toBe('partial more')
  })
})
