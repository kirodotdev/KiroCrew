import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ArtifactFolderDeleteDialog from '../components/ArtifactFolderDeleteDialog'
import type { ArtifactFolder } from '../types'

const folders: ArtifactFolder[] = [
  { id: 'a', name: 'Alpha', order: 0, parent_id: '', item_count: 2 },
  { id: 'b', name: 'Beta', order: 0, parent_id: 'a', item_count: 3 },
  { id: 'e', name: 'Echo', order: 1, parent_id: '', item_count: 1 },
]

describe('ArtifactFolderDeleteDialog', () => {
  it('renders nothing when folder is null', () => {
    render(
      <ArtifactFolderDeleteDialog folder={null} folders={folders} onConfirm={() => {}} onClose={() => {}} />,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('shows subtree impact counts (artifacts across subfolders)', () => {
    render(
      <ArtifactFolderDeleteDialog folder={folders[0]} folders={folders} onConfirm={() => {}} onClose={() => {}} />,
    )
    // Alpha subtree: 2 (own) + 3 (Beta) artifacts, 1 subfolder.
    expect(screen.getByText(/5 artifacts/)).toBeInTheDocument()
    expect(screen.getByText(/1 subfolder\b/)).toBeInTheDocument()
  })

  it('confirms the destructive cascade with deleteContents=true', () => {
    const onConfirm = vi.fn()
    render(
      <ArtifactFolderDeleteDialog folder={folders[0]} folders={folders} onConfirm={onConfirm} onClose={() => {}} />,
    )
    fireEvent.click(screen.getByText('Delete folder and all contents'))
    expect(onConfirm).toHaveBeenCalledWith(true)
  })

  it('confirms the safe path with deleteContents=false', () => {
    const onConfirm = vi.fn()
    render(
      <ArtifactFolderDeleteDialog folder={folders[0]} folders={folders} onConfirm={onConfirm} onClose={() => {}} />,
    )
    fireEvent.click(screen.getByText('Delete folder only, keep artifacts'))
    expect(onConfirm).toHaveBeenCalledWith(false)
  })

  it('describes the re-parent destination: root for a root folder, parent for a nested one', () => {
    const { unmount } = render(
      <ArtifactFolderDeleteDialog folder={folders[0]} folders={folders} onConfirm={() => {}} onClose={() => {}} />,
    )
    expect(screen.getByText(/move up to the library root/)).toBeInTheDocument()
    unmount()
    render(
      <ArtifactFolderDeleteDialog folder={folders[1]} folders={folders} onConfirm={() => {}} onClose={() => {}} />,
    )
    expect(screen.getByText(/move up to the parent folder/)).toBeInTheDocument()
  })

  it('cancel closes without confirming', () => {
    const onConfirm = vi.fn()
    const onClose = vi.fn()
    render(
      <ArtifactFolderDeleteDialog folder={folders[2]} folders={folders} onConfirm={onConfirm} onClose={onClose} />,
    )
    fireEvent.click(screen.getByText('Cancel'))
    expect(onClose).toHaveBeenCalled()
    expect(onConfirm).not.toHaveBeenCalled()
  })
})
