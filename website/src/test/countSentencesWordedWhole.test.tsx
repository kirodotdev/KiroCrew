import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import ArtifactFolderDeleteDialog from '../components/ArtifactFolderDeleteDialog'
import { CommentList, type InlineComment } from '../components/CommentOverlay'
import FileChangeChips, { type FileChangeEntry } from '../components/FileChangeChips'
import { i18next } from '../i18n/all'
import type { ArtifactFolder } from '../types'

/* Each of these sentences is one catalog message, so a language words it whole:
 * the English text is unchanged, and Japanese gets its own word order with no
 * English space glued between two translated pieces. */

const FOLDERS: ArtifactFolder[] = [
  { id: 'a', name: 'Alpha', order: 0, parent_id: '', item_count: 2 },
  { id: 'b', name: 'Beta', order: 0, parent_id: 'a', item_count: 3 },
]
const COMMENTS: InlineComment[] = [
  { id: 'c1', anchor: 'first paragraph', text: 'tighten this' },
  { id: 'c2', anchor: 'second paragraph', text: 'cut this' },
]
const fileOnly = (path: string): FileChangeEntry => ({
  path, before: '', after: '', truncated: true, content_omitted: true, omitted_reason: 'older_turn',
})

const views = {
  folder: () => <ArtifactFolderDeleteDialog folder={FOLDERS[0]} folders={FOLDERS} onConfirm={vi.fn()} onClose={vi.fn()} />,
  leafFolder: () => <ArtifactFolderDeleteDialog folder={FOLDERS[1]} folders={FOLDERS} onConfirm={vi.fn()} onClose={vi.fn()} />,
  comments: () => <CommentList comments={COMMENTS} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />,
  files: () => <FileChangeChips fileChanges={[fileOnly('/a.py'), fileOnly('/b.py'), fileOnly('/c.py')]} />,
}

afterEach(async () => {
  cleanup()
  await act(async () => { await i18next.changeLanguage('en') })
})

describe('count sentences worded as one message', () => {
  it.each([
    ['folder', 'This folder contains 5 artifacts across 1 subfolder.', 'このフォルダーには 1 個のサブフォルダーにわたって 5 個のアーティファクトが含まれています。'],
    ['leafFolder', 'This folder contains 3 artifacts.', 'このフォルダーには 3 個のアーティファクトが含まれています。'],
    ['comments', '2 comments pending', '保留中のコメント 2 件'],
    ['files', '3 files changed', '変更されたファイル 3 個'],
  ] as const)('%s: same English, whole Japanese', async (view, english, japanese) => {
    const first = render(views[view]())
    expect(screen.getByText(english)).toBeInTheDocument()
    first.unmount()
    await act(async () => { await i18next.changeLanguage('ja') })
    render(views[view]())
    expect(screen.getByText(japanese)).toBeInTheDocument()
  })

  it.each([
    ['folder', 'Contents move up to the library root.', 'コンテンツはライブラリのルートに移動します。'],
    ['leafFolder', 'Contents move up to the parent folder.', 'コンテンツは親フォルダーに移動します。'],
  ] as const)('%s: where the contents go, as one sentence', async (view, english, japanese) => {
    const first = render(views[view]())
    expect(screen.getByText(english)).toBeInTheDocument()
    first.unmount()
    await act(async () => { await i18next.changeLanguage('ja') })
    render(views[view]())
    expect(screen.getByText(japanese)).toBeInTheDocument()
  })
})
