import { afterEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_MODE_REFUSED_EVENT, resetDefaultModeRefusedForTests, takeDefaultModeRefused } from './defaultModeNotice'

describe('takeDefaultModeRefused', () => {
  afterEach(() => resetDefaultModeRefusedForTests())

  it('strips the refusal from the slot and announces only the first one', () => {
    const heard = vi.fn()
    window.addEventListener(DEFAULT_MODE_REFUSED_EVENT, heard)
    try {
      expect(takeDefaultModeRefused({ key: 'a', default_mode_refused: 'policy' })).toEqual({ key: 'a' })
      expect(takeDefaultModeRefused({ key: 'b', default_mode_refused: 'policy' })).toEqual({ key: 'b' })
      expect(heard).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(DEFAULT_MODE_REFUSED_EVENT, heard)
    }
  })

  it('lets a folder failure on the same create keep the notice, and says it on the next refusal', () => {
    const heard = vi.fn()
    window.addEventListener(DEFAULT_MODE_REFUSED_EVENT, heard)
    try {
      const both = takeDefaultModeRefused({ key: 'd', default_mode_refused: 'policy', session_folder_notice: 'session_folder_unavailable' })
      // The folder notice is left for takeSessionFolderNotice, which runs next.
      expect(both).toEqual({ key: 'd', session_folder_notice: 'session_folder_unavailable' })
      expect(heard).not.toHaveBeenCalled()
      takeDefaultModeRefused({ key: 'e', default_mode_refused: 'policy' })
      expect(heard).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(DEFAULT_MODE_REFUSED_EVENT, heard)
    }
  })

  it('leaves a create the policy did not refuse untouched and silent', () => {
    const heard = vi.fn()
    window.addEventListener(DEFAULT_MODE_REFUSED_EVENT, heard)
    try {
      const slot = { key: 'c' }
      expect(takeDefaultModeRefused(slot)).toBe(slot)
      expect(heard).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(DEFAULT_MODE_REFUSED_EVENT, heard)
    }
  })
})
