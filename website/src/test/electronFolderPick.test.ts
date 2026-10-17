// The renderer's wrappers around the desktop shell's folder-pick bridge. They
// never throw: availability is false wherever the native chooser cannot be used,
// and a pick ends as a folder, a cancel, or a failure -- a missing bridge, a
// refusal from the shell or a dialog that could not open -- so the picker can
// tell the user about a failure instead of reading it as a cancel.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { canPickFolderNatively, hasNativeFolderPicker, pickFolderNatively } from '../lib/electron'

function install(bridge: unknown) {
  ;(window as { folderPickAPI?: unknown }).folderPickAPI = bridge
}

afterEach(() => {
  delete (window as { folderPickAPI?: unknown }).folderPickAPI
})

describe('lib/electron — native folder picker', () => {
  it('reports no picker, no availability and no pick without the bridge', async () => {
    expect(hasNativeFolderPicker()).toBe(false)
    expect(await canPickFolderNatively()).toBe(false)
    expect(await pickFolderNatively()).toEqual({ status: 'failed' })
  })

  it('treats a bridge missing either call as absent', async () => {
    for (const partial of [{ available: vi.fn() }, { pick: vi.fn() }, {}]) {
      install(partial)
      expect(hasNativeFolderPicker()).toBe(false)
      expect(await canPickFolderNatively()).toBe(false)
      expect(await pickFolderNatively()).toEqual({ status: 'failed' })
    }
  })

  it('is available only on an exact true from the shell', async () => {
    for (const [answer, expected] of [[true, true], [false, false], ['true', false], [1, false], [undefined, false]] as const) {
      install({ available: vi.fn().mockResolvedValue(answer), pick: vi.fn() })
      expect(await canPickFolderNatively()).toBe(expected)
    }
  })

  it('is unavailable when the shell refuses the check', async () => {
    install({ available: vi.fn().mockRejectedValue(new Error('restricted to the local dashboard')), pick: vi.fn() })
    expect(await canPickFolderNatively()).toBe(false)
  })

  it('returns the chosen folder, a cancel, or a failure', async () => {
    const pick = vi.fn().mockResolvedValue('/home/u/project')
    install({ available: vi.fn(), pick })
    expect(await pickFolderNatively()).toEqual({ status: 'picked', path: '/home/u/project' })
    expect(pick).toHaveBeenCalledWith()

    install({ available: vi.fn(), pick: vi.fn().mockResolvedValue(null) })
    expect(await pickFolderNatively()).toEqual({ status: 'cancelled' })

    for (const answer of ['', 42, { path: '/x' }, undefined]) {
      install({ available: vi.fn(), pick: vi.fn().mockResolvedValue(answer) })
      expect(await pickFolderNatively()).toEqual({ status: 'failed' })
    }
  })

  it('reports a refused or failed pick as a failure, not a cancel', async () => {
    install({ available: vi.fn(), pick: vi.fn().mockRejectedValue(new Error('restricted to the local dashboard')) })
    expect(await pickFolderNatively()).toEqual({ status: 'failed' })
  })
})
