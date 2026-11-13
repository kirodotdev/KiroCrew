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
