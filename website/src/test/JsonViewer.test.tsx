import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { JsonViewer } from '../components/FileRenderers'

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
})
