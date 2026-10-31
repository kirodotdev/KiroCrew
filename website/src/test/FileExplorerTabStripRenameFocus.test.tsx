// Closing a workspace tab's inline rename never strands focus on
// document.body. Enter (save) and Escape unmount the focused input, so both
// return focus to the tab the rename was opened from. A pointer blur saves but
// leaves focus where the user put it (WCAG 3.2.2).

import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import TabStrip from '../apps/file-explorer/TabStrip'
import type { FolderTab } from '../apps/file-explorer/types'
import { i18nT } from '../i18n/t'

// TabStrip measures its scroller for the edge fades.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

const FOLDERS: FolderTab[] = [
  { id: 'ft-1', rootPath: '/home/user', label: 'Home', expanded: {}, showSearch: false },
  { id: 'ft-2', rootPath: '/tmp', label: 'Scratch', expanded: {}, showSearch: false },
]
const RENAME = i18nT('apps.fileExplorer.tabStrip.rename_workspace_tab')

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })
const renameField = () => screen.queryByLabelText(RENAME)
const tabOf = (title: string) => screen.getByTitle(title)

afterEach(cleanup)

function openRename(onRenameFolder = vi.fn()) {
  render(
    <>
      <TabStrip folderTabs={FOLDERS} fileTabs={[]} activeFolderId="ft-1" activeFileId={null}
        onActivateFolder={vi.fn()} onActivateFile={vi.fn()} onCloseFolder={vi.fn()}
        onCloseFile={vi.fn()} onNewFolder={vi.fn()} onRenameFolder={onRenameFolder} />
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </>,
  )
  fireEvent.doubleClick(tabOf('/tmp'))
  const input = screen.getByLabelText(RENAME)
  expect(input).toHaveFocus()
  return { input, onRenameFolder }
}

describe('workspace tab rename focus', () => {
  it('Enter saves and returns focus to the renamed tab', async () => {
    const { input, onRenameFolder } = openRename()
    fireEvent.change(input, { target: { value: 'Builds' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(renameField()).toBeNull()
    await waitFor(() => expect(tabOf('/tmp')).toHaveFocus())
    expect(onRenameFolder).toHaveBeenCalledWith('ft-2', 'Builds')
  })

  it('Escape cancels and returns focus to the renamed tab', async () => {
    const { input, onRenameFolder } = openRename()
    fireEvent.change(input, { target: { value: 'Abandoned' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(renameField()).toBeNull()
    await waitFor(() => expect(tabOf('/tmp')).toHaveFocus())
    expect(onRenameFolder).not.toHaveBeenCalled()
  })

  it('a pointer blur saves but leaves focus where the user put it', async () => {
    const { input, onRenameFolder } = openRename()
    fireEvent.change(input, { target: { value: 'Clicked away' } })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    expect(renameField()).toBeNull()
    await nextFrame()
    expect(elsewhere).toHaveFocus()
    expect(onRenameFolder).toHaveBeenCalledWith('ft-2', 'Clicked away')
  })
})
