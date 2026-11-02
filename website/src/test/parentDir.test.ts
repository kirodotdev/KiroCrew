/**
 * `parentDir` (`utils/parentDir.ts`): the containing directory of a gateway path,
 * on POSIX paths and on the backslash paths a Windows gateway prints.
 */
import { describe, it, expect } from 'vitest'
import { parentDir } from '../utils/parentDir'

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
