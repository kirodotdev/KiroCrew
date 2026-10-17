import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { JsonViewer } from '../components/FileRenderers'
import { i18nT } from '../i18n/t'

describe('JsonViewer', () => {
  it('renders ASCII JSON without showing the error banner', () => {
    const { container } = render(<JsonViewer content={'{"a": 1, "b": "hello"}'} />)
    const text = container.textContent || ''
    expect(text).not.toMatch(/Invalid JSON/)
    // Tree view shows keys
    expect(text).toContain('"a"')
    expect(text).toContain('"b"')
  })

  it('renders JSON containing UTF-8 multi-byte (CJK) characters', () => {
    // Synthetic fixture covering the bug class (multi-byte UTF-8 in the
    // tree viewer). All values are fabricated — no real case IDs, team
    // names, dates, or narratives.
    const payload = {
      schema_version: '1',
      sample_id: 'fixture-cjk-0001',
      labels: {
        zh: '中文標籤範例',
        ja: '日本語ラベル',
        ko: '한국어 라벨',
      },
      tags: ['測試', 'テスト', '테스트', '🐾'],
    }
    const content = JSON.stringify(payload, null, 2)
    const { container } = render(<JsonViewer content={content} />)
    const text = container.textContent || ''
    expect(text).not.toMatch(/Invalid JSON/)
    expect(text).toContain('中文標籤範例')
    expect(text).toContain('🐾')
  })

  it('shows the parse error message and a content preview when JSON is malformed', () => {
    // Defensive: if the user ever DOES hit a parse failure in the viewer,
    // they should see exactly what failed instead of a bare "Invalid JSON".
    const broken = '{"a": 1, "b": [1, 2,'
    const { container } = render(<JsonViewer content={broken} />)
    const text = container.textContent || ''
    expect(text).toContain('Invalid JSON')
    // Error message should include a hint from JSON.parse beyond our own
    // 'Invalid JSON' label (e.g. "position", "Unexpected", "end of"). We
    // don't lock in exact phrasing because it varies between JS engines —
    // but matching one of these tokens proves the engine's parse error
    // bubbled up, not just our static fallback.
    expect(text.toLowerCase()).toMatch(/position|unexpected|end of/)
    // Raw content preview is shown so the user can see what the viewer received.
    expect(text).toContain(broken)
  })

  it('re-escapes string values holding embedded JSON instead of rendering them raw', () => {
    // The bug class: JSON.parse consumes the escapes, and rendering the parsed
    // value wrapped in hand-placed quotes displays `"{"execute":…"` — which is
    // indistinguishable from invalid nested raw JSON. Common real payloads hit
    // this: SNS notification envelopes, SQS bodies, EventBridge detail strings.
    const content = JSON.stringify({ Message: '{"execute":{"type":"UPSERT"}}' })
    const { container } = render(<JsonViewer content={content} />)
    const text = container.textContent || ''
    expect(text).not.toMatch(/Invalid JSON/)
    // The leaf renders as valid JSON source: quotes inside the string are escaped.
    expect(text).toContain('"{\\"execute\\":{\\"type\\":\\"UPSERT\\"}}"')
    // And the misleading unescaped form is NOT shown.
    expect(text).not.toContain('"{"execute"')
  })

  it('re-escapes object keys containing quotes or backslashes', () => {
    const content = '{"quo\\"te": 1, "back\\\\slash": 2}'
    const { container } = render(<JsonViewer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('"quo\\"te"')
    expect(text).toContain('"back\\\\slash"')
  })

  it('truncates long strings without cutting an escape sequence in half', () => {
    // A `"` at raw index 199 straddles the 200-char slice point. Because the
    // RAW value is truncated before JSON.stringify, the escape for that quote
    // survives whole. The ellipsis renders OUTSIDE the closing quote so the
    // quoted text is never mistaken for the faithful full value.
    const long = 'a'.repeat(199) + '"' + 'b'.repeat(50)
    const { container } = render(<JsonViewer content={JSON.stringify({ v: long })} />)
    const text = container.textContent || ''
    // Truncated to 200 raw chars (…199 a's + the quote), escaped, quote-closed,
    // then the truncation marker outside the quotes.
    expect(text).toContain('a'.repeat(199) + '\\""…')
    // The b's beyond the truncation point never render.
    expect(text).not.toContain('bbb')
  })
})

describe('JsonViewer long string leaves', () => {
  const showAll = (count: number) => i18nT('components.fileRenderers.json_string_show_all', { count })
  const showLess = () => i18nT('components.fileRenderers.json_string_show_less')
  const preview = (value: string) => JSON.stringify(value.slice(0, 200)) + '…'
  // The element the toggle names in aria-controls: the string value itself.
  const valueOf = (toggle: HTMLElement) => document.getElementById(toggle.getAttribute('aria-controls') ?? '')?.textContent

  it('expands a long string to its full value and collapses it back to the preview', () => {
    const value = 'a'.repeat(240) + 'END-MARKER'
    render(<JsonViewer content={JSON.stringify({ prompt: value })} />)

    const toggle = screen.getByRole('button', { name: showAll(250) })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(valueOf(toggle)).toBe(preview(value))

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveTextContent(showLess())
    expect(valueOf(toggle)).toBe(JSON.stringify(value))

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveTextContent(showAll(250))
    expect(valueOf(toggle)).toBe(preview(value))
  })

  it('opens and closes the full value from the keyboard', async () => {
    const user = userEvent.setup()
    const value = 'k'.repeat(230) + 'KEY-MARKER'
    render(<JsonViewer content={JSON.stringify({ v: value })} />)

    const toggle = screen.getByRole('button', { name: showAll(240) })
    toggle.focus()
    await user.keyboard('{Enter}')
    expect(valueOf(toggle)).toBe(JSON.stringify(value))
    await user.keyboard(' ')
    expect(valueOf(toggle)).toBe(preview(value))
  })

  it('shows a string of exactly 200 characters whole, and offers the toggle from 201', () => {
    const atLimit = 'b'.repeat(199) + 'Z'
    const overLimit = 'c'.repeat(200) + 'Y'
    const { container } = render(<JsonViewer content={JSON.stringify({ atLimit, overLimit })} />)

    expect(container.textContent).toContain(JSON.stringify(atLimit))
    expect(screen.queryByRole('button', { name: showAll(200) })).toBeNull()

    const toggle = screen.getByRole('button', { name: showAll(201) })
    expect(valueOf(toggle)).toBe(preview(overLimit))
    fireEvent.click(toggle)
    expect(valueOf(toggle)).toBe(JSON.stringify(overLimit))
  })

  it('expands a multiline prompt with its line breaks kept as JSON escapes', () => {
    const value = Array.from({ length: 12 }, (_, i) => `Step ${i}: ${'p'.repeat(20)}`).join('\n') + '\nFINAL-LINE'
    render(<JsonViewer content={JSON.stringify({ prompt: value })} />)

    const toggle = screen.getByRole('button', { name: showAll(value.length) })
    expect(valueOf(toggle)).not.toContain('FINAL-LINE')
    fireEvent.click(toggle)
    expect(valueOf(toggle)).toBe(JSON.stringify(value))
    expect(valueOf(toggle)).toContain('\\nFINAL-LINE"')
  })

  it('keeps quotes and backslashes escaped in the preview and in the full value', () => {
    // 29 characters per repeat, so the 200-character cut lands right after a
    // backslash: the preview must still close on a whole `\\` escape.
    const value = 'He said "stop" at C:\\temp\\x. '.repeat(8) + 'TAIL'
    render(<JsonViewer content={JSON.stringify({ v: value })} />)

    const toggle = screen.getByRole('button', { name: showAll(value.length) })
    expect(valueOf(toggle)).toBe(preview(value))
    expect(valueOf(toggle)).toMatch(/C:\\\\temp\\\\"…$/)
    fireEvent.click(toggle)
    expect(valueOf(toggle)).toBe(JSON.stringify(value))
    expect(valueOf(toggle)).toContain('\\"stop\\" at C:\\\\temp\\\\x. TAIL"')
  })
})
