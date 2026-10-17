/**
 * The Files app's deep-link helpers (`apps/file-explorer/deepLink.ts`).
 *
 * Pinned here rather than through the page so each rule is one assertion: the
 * URL another surface builds, which tab root a path belongs to, and the exact
 * chain of directories the tree must expand to show it — on POSIX paths and on
 * the backslash paths a Windows gateway prints.
 */
import { describe, it, expect } from 'vitest'
import {
  FILE_EXPLORER_ROUTE,
  FILE_EXPLORER_PATH_PARAM,
  fileExplorerDeepLink,
  normPath,
  underRoot,
  parentDir,
  revealChain,
} from '../apps/file-explorer/deepLink'

describe('fileExplorerDeepLink', () => {
  it('builds the native route with the path URL-encoded under the agreed param', () => {
    const href = fileExplorerDeepLink('/home/u/notes & drafts/a#1.md')
    const url = new URL(href, 'http://x')
    expect(url.pathname).toBe(FILE_EXPLORER_ROUTE)
    expect(url.searchParams.get(FILE_EXPLORER_PATH_PARAM)).toBe('/home/u/notes & drafts/a#1.md')
  })

  it('round-trips a Windows path through the same param', () => {
    const url = new URL(fileExplorerDeepLink('C:\\Users\\u\\report.md'), 'http://x')
    expect(url.searchParams.get(FILE_EXPLORER_PATH_PARAM)).toBe('C:\\Users\\u\\report.md')
  })
})

describe('normPath / underRoot', () => {
  it('keeps a POSIX path byte-identical — it IS case-sensitive', () => {
    expect(normPath('/home/U/Docs')).toBe('/home/U/Docs')
    expect(underRoot('/home/u/docs', '/home/U')).toBe(false)
  })

  it('folds separators and case on drive and UNC paths', () => {
    expect(normPath('C:\\Users\\U')).toBe('c:/users/u')
    expect(underRoot('c:/users/u/a.txt', 'C:\\Users\\U')).toBe(true)
    expect(underRoot('\\\\srv\\share\\x', '//SRV/share')).toBe(true)
  })

  it('treats the root itself as under the root, and a sibling prefix as outside', () => {
    expect(underRoot('/home/u', '/home/u/')).toBe(true)
    expect(underRoot('/tmp/database', '/tmp/data')).toBe(false)
  })

  it('puts every path under the POSIX root', () => {
    expect(underRoot('/tmp/x', '/')).toBe(true)
  })
})

describe('parentDir', () => {
  it('walks POSIX paths and stops at /', () => {
    expect(parentDir('/home/u/a.txt')).toBe('/home/u')
    expect(parentDir('/home')).toBe('/')
    expect(parentDir('/')).toBeNull()
  })

  it('walks backslash paths and stops at the drive root', () => {
    expect(parentDir('C:\\Users\\u\\a.txt')).toBe('C:\\Users\\u')
    expect(parentDir('C:\\Users')).toBe('C:\\')
    expect(parentDir('C:\\')).toBeNull()
  })

  it('ignores a trailing separator and has no parent for a bare name', () => {
    expect(parentDir('/home/u/')).toBe('/home')
    expect(parentDir('notes.txt')).toBeNull()
  })
})

describe('revealChain', () => {
  it('lists the root, then every directory down to the target, outermost first', () => {
    expect(revealChain('/home/u', '/home/u/.kiro/crew/workspace')).toEqual([
      '/home/u', '/home/u/.kiro', '/home/u/.kiro/crew', '/home/u/.kiro/crew/workspace',
    ])
  })

  it('is just the root when the target is the root', () => {
    expect(revealChain('/home/u', '/home/u')).toEqual(['/home/u'])
    expect(revealChain('/home/u/', '/home/u')).toEqual(['/home/u/'])
  })

  it('is empty when the target is not under the root', () => {
    expect(revealChain('/home/u', '/tmp/x')).toEqual([])
    expect(revealChain('/tmp/data', '/tmp/database/x')).toEqual([])
  })

  it('reaches down from the POSIX root', () => {
    expect(revealChain('/', '/home/u')).toEqual(['/', '/home', '/home/u'])
  })

  it('keeps each entry byte-identical to the backend spelling on a Windows gateway', () => {
    // `FolderTab.expanded` is keyed by the tree node's `path`, which the backend
    // prints with backslashes; a re-joined or case-folded path would miss.
    expect(revealChain('C:\\Users\\u', 'C:\\Users\\u\\src\\lib')).toEqual([
      'C:\\Users\\u', 'C:\\Users\\u\\src', 'C:\\Users\\u\\src\\lib',
    ])
    expect(revealChain('C:\\', 'C:\\x')).toEqual(['C:\\', 'C:\\x'])
  })
})
