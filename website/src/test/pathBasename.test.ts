/**
 * `pathBasename` / `stripTrailingSeparators` (utils/pathBasename.ts), issue
 * #14581: `\` is a separator only in a Windows-shaped path (drive-rooted or
 * backslash UNC); every other path splits on `/` exactly as
 * `split('/').pop()` / `replace(/\/+$/, '')` always did.
 */
import { describe, it, expect } from 'vitest'
import { backslashIsSeparator, pathBasename, stripTrailingSeparators } from '../utils/pathBasename'

describe('pathBasename', () => {
  it.each([
    // Windows-shaped: `\` separates.
    ['C:\\Users\\u\\report.md', 'report.md'],
    ['c:\\report.md', 'report.md'],
    ['C:/Users/u/report.md', 'report.md'],
    ['C:\\Users\\u/mixed\\final.ts', 'final.ts'],
    ['C:/Users\\u/report.md', 'report.md'],
    ['\\\\host\\share\\dir\\report.md', 'report.md'],
    ['\\\\host/share/report.md', 'report.md'],
    ['C:\\repo\\', ''],
    // POSIX: `\` is part of the name.
    ['/tmp/we\\ird.md', 'we\\ird.md'],
    ['src/report\\final.csv', 'report\\final.csv'],
    ['back\\slash', 'back\\slash'],
    ['/repo/src/a.ts', 'a.ts'],
    ['a.ts', 'a.ts'],
    ['/repo/', ''],
    ['//host/share/a.ts', 'a.ts'],
    ['', ''],
  ])('%s -> %s', (input, expected) => {
    expect(pathBasename(input)).toBe(expected)
  })

  it.each([
    '/repo/src/a.ts', '/tmp/we\\ird.md', 'src/x\\y', 'plain', '/a/b/', '', '/',
    '//host/share/a.ts', 'C:not-rooted\\x', 'C:', '\\single\\lead',
  ])('a non-Windows-shaped path splits exactly like split("/").pop(): %s', (p) => {
    expect(pathBasename(p)).toBe(p.split('/').pop())
  })
})

describe('stripTrailingSeparators', () => {
  it.each([
    ['C:\\repo\\', 'C:\\repo'],
    ['C:\\repo\\/\\', 'C:\\repo'],
    ['C:/repo/', 'C:/repo'],
    ['C:\\', 'C:'],
    ['\\\\host\\share\\', '\\\\host\\share'],
    ['/repo/', '/repo'],
    ['/', ''],
    ['/tmp/name\\', '/tmp/name\\'],
  ])('%s -> %s', (input, expected) => {
    expect(stripTrailingSeparators(input)).toBe(expected)
  })

  it.each(['/a/b//', '/tmp/x\\', 'rel/dir/', '', '//'])('a non-Windows-shaped path strips exactly like replace(/\\/+$/, ""): %s', (p) => {
    expect(stripTrailingSeparators(p)).toBe(p.replace(/\/+$/, ''))
  })
})

describe('backslashIsSeparator', () => {
  it.each([
    ['C:\\x', true], ['z:/x', true], ['\\\\host\\x', true], ['\\\\host/x', true],
    ['/x\\y', false], ['x\\y', false], ['C:x\\y', false], ['\\\\', false], ['CD:\\x', false],
  ])('%s -> %s', (p, expected) => {
    expect(backslashIsSeparator(p)).toBe(expected)
  })
})
