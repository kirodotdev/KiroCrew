/**
 * The desktop app auto-saves a download only under the exact name it was told
 * to expect, and Chromium rewrites some characters before it saves. The name
 * announced and the name clicked must both be the rewritten one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { chromiumSavedName, downloadBlob, downloadUrl } from './download'

describe('download file names', () => {
  const clicked: string[] = []
  const announced: string[] = []
  const origCreate = URL.createObjectURL
  const origRevoke = URL.revokeObjectURL
  beforeEach(() => {
    vi.useFakeTimers()
    clicked.length = 0
    announced.length = 0
    URL.createObjectURL = (() => 'blob:x') as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = (() => {}) as unknown as typeof URL.revokeObjectURL
    ;(window as unknown as { kirocrew?: unknown }).kirocrew = {
      expectDownload: (name: string) => announced.push(name),
    }
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(this: HTMLAnchorElement) {
      clicked.push(this.download)
    })
  })
  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
    vi.restoreAllMocks()
    URL.createObjectURL = origCreate
    URL.revokeObjectURL = origRevoke
    delete (window as unknown as { kirocrew?: unknown }).kirocrew
  })

  it('announces and clicks the name Chromium will save', () => {
    downloadBlob(new Blob(['hi']), 'notes 10:30.md')
    downloadUrl('/api/files/x', 'a?b*c.txt')
    expect(announced).toEqual(['notes 10_30.md', 'a_b_c.txt'])
    expect(clicked).toEqual(announced)
  })

  it('rewrites the characters Chromium replaces and keeps the rest', () => {
    expect(chromiumSavedName('a"b<c>d|e\\f/g.md')).toBe('a_b_c_d_e_f_g.md')
    expect(chromiumSavedName('tab\there\u200b.md')).toBe('tab_here_.md')
    expect(chromiumSavedName('Crew – export (2).kirobot')).toBe('Crew – export (2).kirobot')
  })
})

describe('chromiumSavedName edge rewrites', () => {
  // [name, saved on Linux and macOS, saved on Windows]. The first rows are
  // Chromium 152's own GenerateFileName and ReplaceIllegalCharactersInPath test
  // vectors (with `_` for their `-`); the rest are names a workspace file has.
  it.each([
    ['evil.exe.', 'evil.exe', 'evil.exe_'],
    ['evil.exe.  .  .', 'evil.exe.  . _', 'evil.exe_______'],
    ['. . . . .', '_. . ._', 'download'],
    ['meh.exe\u00a0', 'meh.exe_', 'meh.exe_'],
    ['..', 'download', 'download'],
    ['.download.txt', 'download.txt', 'download.txt'],
    [' .foo', '_.foo', '_.foo'],
    ['\u2008.(\u2007).\u3000', '_.(\u2007)._', '_.(\u2007)._'],
    ['~config1.txt', '_config1.txt', '_config1.txt'],
    ['config1.txt~', 'config1.txt_', 'config1.txt_'],
    ['config~1.txt', 'config~1.txt', 'config_1.txt'],
    ['conf,g~1.txt', 'conf,g~1.txt', 'conf,g~1.txt'],
    ['config~12.txt', 'config~12.txt', 'config~12.txt'],
    ['bad\uffffname\u{10fffe}.jpg', 'bad_name_.jpg', 'bad_name_.jpg'],
    ['COM4.txt', 'COM4.txt', '_COM4.txt'],
    ['clock$.txt', 'clock$.txt', '_clock$.txt'],
    ['mycom1.foo', 'mycom1.foo', 'mycom1.foo'],
    ['Desktop.ini', 'Desktop.ini', '_Desktop.ini'],
    ['Setup.exe.lnk', 'Setup.exe.lnk', 'Setup.exe.download'],
    ['.gitignore', 'gitignore', 'gitignore'],
    [' notes.md', '_notes.md', '_notes.md'],
    ['notes.md ', 'notes.md_', 'notes.md_'],
    ['NUL', 'NUL', '_NUL'],
    ['~', '_', '_'],
  ])('saves %j as %j, or %j on Windows', (name, posix, windows) => {
    expect(chromiumSavedName(name, false)).toBe(posix)
    expect(chromiumSavedName(name, true)).toBe(windows)
  })
})
