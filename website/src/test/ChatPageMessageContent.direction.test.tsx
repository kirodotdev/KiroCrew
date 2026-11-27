import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { renderUserContent } from '../pages/chat/ChatPageMessageContent'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

const prose = 'שלום (API 42)\nEnglish paragraph'
const paste: PasteBlock = { id: 'boundary', seq: 1, lines: 3, content: 'one\ntwo\nthree' }

describe('plain chat content direction', () => {
  it.each([
    { name: 'paste-adjacent prose', content: `${prose} ${formatToken(paste)}`, meta: { pastes: [paste] } },
    { name: 'paste-adjacent attachment prose', content: `${prose} @a.ts ${formatToken(paste)}`, meta: { pastes: [paste], files: ['/repo/a.ts'] } },
    { name: 'attachment caption prose', content: `${prose} @a.ts`, meta: { files: ['/repo/a.ts'] } },
  ])('auto-directs and isolates $name without rewriting Unicode', ({ content, meta }) => {
    const { container } = render(<>{renderUserContent({ content, meta })}</>)
    const body = container.querySelector<HTMLElement>('[data-message-body]')!
    expect(body).not.toBeNull()
    expect(body.getAttribute('dir')).toBe('auto')
    expect(body.style.unicodeBidi).toBe('plaintext')
    expect(body.style.whiteSpace).toBe('pre-wrap')
    expect(body.textContent).toContain(prose)
  })
})
