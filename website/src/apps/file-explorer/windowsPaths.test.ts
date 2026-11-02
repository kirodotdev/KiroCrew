/**
 * Issue #14581 for the Files app: a Windows gateway's `C:\…` path shows its file
 * name, not the whole path, while a POSIX path splits exactly as before — the
 * contract `test/windowsBasenameSites.test.tsx` pins for the core sites.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { basename as explorerBasename, crumbLabel, parentChain } from './utils'

const WIN = 'C:\\Users\\u\\docs\\report.md'
const POSIX_BACKSLASH = '/tmp/we\\ird.md'

describe('Files app paths show their file name (#14581)', () => {
  it.each([
    [WIN, 'report.md'],
    ['C:\\Users\\u\\docs\\', 'docs'],
    [POSIX_BACKSLASH, 'we\\ird.md'],
    ['/a/b/', 'b'],
    ['/', ''],
  ])('basename(%s) is %s', (p, name) => {
    expect(explorerBasename(p)).toBe(name)
  })

  it('utils.ts derives file names through pathBasename only', () => {
    const src = readFileSync(resolve(__dirname, 'utils.ts'), 'utf8')
    expect(src).toMatch(/import \{[^}]*\bpathBasename\b[^}]*\} from '[./]+\/utils\/pathBasename'/)
    expect(src).not.toMatch(/\.split\('\/'\)\.pop\(\)/)
    expect(src).not.toMatch(/\.lastIndexOf\('\/'\) \+ 1\)/)
  })
})

// Breadcrumb navigation is out of scope: these pin main's behaviour so the
// separator-aware basename cannot collapse a Windows root crumb to `repo`.
describe('file-explorer breadcrumbs are unchanged by #14581', () => {
  it.each([
    ['C:\\Users\\u\\repo', ['/', 'C:\\Users\\u\\repo']],
    ['/a/b/c', ['/', '/a', '/a/b', '/a/b/c']],
    ['/tmp/we\\ird', ['/', '/tmp', '/tmp/we\\ird']],
  ])('parentChain(%s)', (p, chain) => {
    expect(parentChain(p)).toEqual(chain)
  })

  it.each([
    ['C:\\Users\\u\\repo', 'C:\\Users\\u\\repo'],
    ['C:/Users', 'Users'],
    ['/a/b/', 'b'],
    ['/tmp/we\\ird', 'we\\ird'],
  ])('crumbLabel(%s) is %s', (p, label) => {
    expect(crumbLabel(p)).toBe(label)
  })

  it('PathBar labels crumbs with crumbLabel, not the separator-aware basename', () => {
    const src = readFileSync(resolve(__dirname, 'PathBar.tsx'), 'utf8')
    expect(src).toContain('crumbLabel(s)')
    expect(src).not.toMatch(/\bbasename\(s\)/)
  })
})
