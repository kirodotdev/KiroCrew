import { afterEach, describe, it, expect, vi } from 'vitest'
import { act, cleanup, render, renderHook, screen, fireEvent } from '@testing-library/react'

import type { ChatFolder } from '../types'
import { folderProjectMismatch } from '../utils/folderAgent'

vi.mock('../components/VoicePlaybackBar', () => ({ default: () => null }))

import { useComposerAboveBand } from '../pages/chat/page/composerDock'
import { setMovedSlot } from '../hooks/useMoveSlotToFolder'

const folder = (id: string, opts: Partial<ChatFolder> = {}): ChatFolder => ({
  id, name: id, order: 0, collapsed: false, parent_id: '', ...opts,
})

const folders = [
  folder('repo', { project_dir: '/projects/repo' }),
  folder('child', { parent_id: 'repo' }),
  folder('plain'),
]

describe('folderProjectMismatch', () => {
  it('returns the folder directory when the session uses another one', () => {
    expect(folderProjectMismatch(folders, 'repo', '/projects/other')).toBe('/projects/repo')
  })

  it('resolves the directory from the nearest ancestor', () => {
    expect(folderProjectMismatch(folders, 'child', '/projects/other')).toBe('/projects/repo')
  })

  it('counts an unbound session as differing', () => {
    expect(folderProjectMismatch(folders, 'repo', '')).toBe('/projects/repo')
  })

  it('is no mismatch for the same directory, a folder with none, or no folder', () => {
    expect(folderProjectMismatch(folders, 'repo', '/projects/repo')).toBeUndefined()
    expect(folderProjectMismatch(folders, 'plain', '/projects/other')).toBeUndefined()
    expect(folderProjectMismatch(folders, undefined, '/projects/other')).toBeUndefined()
  })
})

type BandProps = { activeSlot: string; folderId: string | undefined; project: string; locked?: boolean }

function renderBand(initial: BandProps, setProject = vi.fn(async () => {})) {
  const hook = renderHook((p: BandProps) => useComposerAboveBand({
    langGen: 0,
    controls: {
      sessionControls: [],
      sessionControlsError: null,
      sessionControlStatusError: null,
      chatFolders: folders,
      chatFoldersError: null,
      folderSortMode: 'manual',
      folderSortError: null,
    } as unknown as Parameters<typeof useComposerAboveBand>[0]['controls'],
    folderSuggestion: undefined,
    activeSlot: p.activeSlot,
    sidebarOnScreen: true,
    folderSuggestionAccept: () => {},
    folderSuggestionDecline: () => {},
    slotFolderId: p.folderId,
    slotProject: p.project,
    setProject,
    projectLocked: !!p.locked,
    activeTip: null,
    dismissTip: () => {},
  }), { initialProps: initial })
  const view = render(hook.result.current)
  return { setProject, hook, view }
}

const moved = { activeSlot: 'slot-1', folderId: 'repo', project: '/projects/other' }

describe('composer band folder-project hint', () => {
  afterEach(() => { cleanup(); act(() => setMovedSlot(null)) })

  it('shows the hint for the session the user just moved into a folder with another project', () => {
    act(() => setMovedSlot('slot-1'))
    renderBand(moved)
    expect(screen.getByTestId('folder-project-hint').textContent)
      .toContain("Commands, files and search here use other, not this folder's repo.")
    expect(screen.getByTestId('folder-project-hint-use').textContent).toBe('Switch to repo')
    expect(screen.getByTestId('folder-project-hint-dismiss').textContent).toBe('Keep other')
    expect(screen.getByTestId('folder-project-hint-note').textContent).toBe(
      'Switching starts a fresh agent session in /projects/repo on your next message. The messages here stay, and the project picker can switch back.')
  })

  it('shows no hint for a session that was not moved, such as a follow-up worktree', () => {
    renderBand(moved)
    expect(screen.queryByTestId('folder-project-hint')).toBeNull()
  })

  it('rebinds through set-project with the folder directory on click', async () => {
    act(() => setMovedSlot('slot-1'))
    const { setProject } = renderBand({ ...moved, folderId: 'child' })
    await act(async () => { fireEvent.click(screen.getByTestId('folder-project-hint-use')) })
    expect(setProject).toHaveBeenCalledWith('/projects/repo')
  })

  it('disables the rebind while the project is locked by attached sub-agents', () => {
    act(() => setMovedSlot('slot-1'))
    const { setProject } = renderBand({ ...moved, locked: true })
    const use = screen.getByTestId('folder-project-hint-use') as HTMLButtonElement
    expect(use.disabled).toBe(true)
    fireEvent.click(use)
    expect(setProject).not.toHaveBeenCalled()
    // The reason is visible text, not only a hover tooltip, and the switch note is gone.
    const note = screen.getByTestId('folder-project-hint-note').textContent
    expect(note).toBe("The project can't change while sub-agents are running.")
    expect(note).not.toContain('Switching starts')
  })

  it('clears when the user dismisses it', () => {
    act(() => setMovedSlot('slot-1'))
    const { hook, view } = renderBand(moved)
    act(() => { fireEvent.click(screen.getByTestId('folder-project-hint-dismiss')) })
    view.rerender(hook.result.current)
    expect(screen.queryByTestId('folder-project-hint')).toBeNull()
  })

  it('shows no hint when the folder has no project or the same one', () => {
    act(() => setMovedSlot('slot-1'))
    renderBand({ ...moved, folderId: 'plain' })
    renderBand({ ...moved, project: '/projects/repo' })
    expect(screen.queryByTestId('folder-project-hint')).toBeNull()
  })
})
