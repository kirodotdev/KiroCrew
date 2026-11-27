import { describe, expect, it } from 'vitest'
import { docSectionHref } from './docsLink'

const BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs'

describe('docSectionHref', () => {
  it('resolves a file and heading anchor', () => {
    expect(docSectionHref('connections.md#atlassian-organization-admin-checklist')).toBe(
      `${BASE}/connections.md#atlassian-organization-admin-checklist`,
    )
  })

  it.each([
    undefined,
    '',
    'connections.md',
    'https://evil.example/x.md#a',
    '//evil.example/x.md#a',
    '../secrets.md#a',
    'guides/x.md#a',
    'x.md?ref=y#a',
    'X.md#a',
    'x_y.md#a',
    'x.md#',
    'x.md#a#b',
    'x.md#a/b',
    'x.md#a b',
    'x.md#-a',
    'x.md#a--b',
    'x.md#"><script>',
    '#anchor',
    'javascript:alert(1)',
  ])('refuses %s', section => {
    expect(docSectionHref(section)).toBeNull()
  })
})
