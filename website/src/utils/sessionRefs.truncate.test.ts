import { describe, expect, it } from 'vitest'
import { formatSessionRefLink, sanitizeRefTitle } from './sessionRefs'

// 78 ASCII units, then an emoji occupying UTF-16 units 78-79: the 79-unit head
// before the ellipsis ends on the emoji's high surrogate.
const TITLE = `${'a'.repeat(78)}🚀 long session title continues here`

describe('sanitizeRefTitle with astral characters', () => {
  it('drops an emoji the 80-unit cap would cut in half instead of keeping its high surrogate', () => {
    expect(sanitizeRefTitle(TITLE)).toBe(`${'a'.repeat(78)}…`)
  })

  it('sends a link label with no broken emoji half', () => {
    const link = formatSessionRefLink({ key: 'chat-1', title: TITLE } as Parameters<typeof formatSessionRefLink>[0])
    expect(link.startsWith(`[${'a'.repeat(78)}…](`)).toBe(true)
  })

  it('keeps an emoji that ends exactly at the cap', () => {
    const title = `${'a'.repeat(77)}🚀 and more`
    expect(sanitizeRefTitle(title)).toBe(`${'a'.repeat(77)}🚀…`)
  })

  it('leaves long ASCII at 80 units including the ellipsis', () => {
    expect(sanitizeRefTitle('x'.repeat(200))).toBe(`${'x'.repeat(79)}…`)
  })
})
