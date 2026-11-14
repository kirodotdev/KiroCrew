import { describe, expect, it } from 'vitest'
import { tipDocHref } from './docsLink'

const BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs'

describe('tipDocHref', () => {
  it.each([
    ['connections.md', `${BASE}/connections.md`],
    ['connections.md#atlassian-organization-admin-checklist', `${BASE}/connections.md#atlassian-organization-admin-checklist`],
  ])('resolves %s', (doc, href) => {
    expect(tipDocHref(doc)).toBe(href)
  })

  it.each([
    undefined,
    '',
    'https://evil.example/x.md',
    '//evil.example/x.md',
    '../secrets.md',
    'guides/x.md',
    'x.md?ref=y',
    'x.md#',
    'x.md#a#b',
    'x.md#a/b',
    'x.md#a b',
    'x.md#-a',
    'x.md#a--b',
    'x.md#"><script>',
    '#anchor',
    'javascript:alert(1)',
  ])('refuses %s', doc => {
    expect(tipDocHref(doc)).toBeNull()
  })
})
