import { describe, it, expect } from 'vitest'
import {
  endsWithSeparator,
  isWindowsPath,
  lastSegment,
  parentIsDriveList,
  pathSeparator,
  stripTrailingSeparator,
} from '../utils/browsePath'

describe('browsePath shape helpers', () => {
  it('classifies drive-letter and UNC paths as Windows, everything else as POSIX', () => {
    expect(isWindowsPath('C:\\Users')).toBe(true)
    expect(isWindowsPath('d:/work')).toBe(true)
    expect(isWindowsPath('\\\\server\\share')).toBe(true)
    expect(isWindowsPath('/home/u')).toBe(false)
    expect(isWindowsPath('/home/u/odd\\name')).toBe(false)
    expect(isWindowsPath('')).toBe(false)
  })

  it('keeps a bare drive root whole in all three spellings, and only those', () => {
    for (const p of ['C:', 'C:\\', 'C:/', 'z:\\']) expect(stripTrailingSeparator(p)).toBe(p)
    expect(stripTrailingSeparator('\\\\server\\')).toBe('\\\\server')
  })

  it('never turns repeated root separators into a drive-relative path', () => {
    // `D:\\` is what a stray second keystroke types; `D:` would resolve to the
    // drive's current directory, not its root.
    expect(stripTrailingSeparator('D:\\\\')).toBe('D:\\')
    expect(stripTrailingSeparator('D:\\\\\\')).toBe('D:\\')
    expect(stripTrailingSeparator('D://')).toBe('D:/')
    expect(stripTrailingSeparator('D:\\/')).toBe('D:\\')
  })

  it('picks the separator by shape', () => {
    expect(pathSeparator('C:\\Users')).toBe('\\')
    expect(pathSeparator('/home/u')).toBe('/')
  })

  it('treats a trailing backslash as a separator only on a Windows path', () => {
    expect(endsWithSeparator('C:\\Users\\')).toBe(true)
    expect(endsWithSeparator('C:/Users/')).toBe(true)
    expect(endsWithSeparator('C:\\Users')).toBe(false)
    expect(endsWithSeparator('/home/u/')).toBe(true)
    expect(endsWithSeparator('/home/u\\')).toBe(false)
  })

  it('strips a trailing separator but never turns a drive root into a drive-relative path', () => {
    expect(stripTrailingSeparator('C:\\Users\\')).toBe('C:\\Users')
    expect(stripTrailingSeparator('C:/Users//')).toBe('C:/Users')
    expect(stripTrailingSeparator('C:\\')).toBe('C:\\')
    expect(stripTrailingSeparator('C:/')).toBe('C:/')
    expect(stripTrailingSeparator('/home/u/')).toBe('/home/u')
    expect(stripTrailingSeparator('/')).toBe('/')
    expect(stripTrailingSeparator('///')).toBe('/')
    // On POSIX a trailing `\` is part of the name.
    expect(stripTrailingSeparator('/home/u/odd\\')).toBe('/home/u/odd\\')
  })

  it('takes the last segment across either separator', () => {
    expect(lastSegment('c:\\users\\me')).toBe('me')
    expect(lastSegment('c:/users/me')).toBe('me')
    expect(lastSegment('/home/u/pro')).toBe('pro')
    // On POSIX `\` is part of the name, so the filter keeps it.
    expect(lastSegment('/home/u/odd\\name')).toBe('odd\\name')
    expect(lastSegment('/home/u/')).toBe('')
    expect(lastSegment('')).toBe('')
  })

  it('reads an empty parent on a non-empty path as the drive-list cue', () => {
    expect(parentIsDriveList('C:\\', '')).toBe(true)
    expect(parentIsDriveList('/', '/')).toBe(false)
    expect(parentIsDriveList('/home/u', '/home')).toBe(false)
    // The drive list itself has no level above.
    expect(parentIsDriveList('', '')).toBe(false)
  })
})
