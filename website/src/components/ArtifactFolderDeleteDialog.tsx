import { useMemo, useState } from 'react'
import { Loader2, Trash2, FolderOutput } from 'lucide-react'
import Modal from './Modal'
import { Btn } from './ui'
import { folderSubtreeStats } from '../utils/artifactFolderTree'
import type { ArtifactFolder } from '../types'

import { i18nT } from '../i18n/t'
/**
 * Delete-folder confirmation dialog. Never silent: the user
 * explicitly picks between the destructive cascade (folder + every descendant
 * folder AND artifact, permanent) and the safe structural delete (folder only —
 * contents re-parent to the deleted folder's parent, root if none).
 */
export default function ArtifactFolderDeleteDialog({
  folder,
  folders,
  onConfirm,
  onClose,
}: {
  /** The folder being deleted, or null when the dialog is closed. */
  folder: ArtifactFolder | null
  folders: readonly ArtifactFolder[]
  /** Called with the chosen mode; the caller runs the DELETE and closes. */
  onConfirm: (deleteContents: boolean) => Promise<void> | void
  onClose: () => void
}) {
  const [busy, setBusy] = useState<'cascade' | 'keep' | null>(null)
  const stats = useMemo(
    () => (folder ? folderSubtreeStats(folders, folder.id) : { artifactCount: 0, subfolderCount: 0 }),
    [folder, folders],
  )
  const run = async (deleteContents: boolean) => {
    setBusy(deleteContents ? 'cascade' : 'keep')
    try {
      await onConfirm(deleteContents)
    } finally {
      setBusy(null)
    }
  }
  return (
    <Modal open={!!folder} onClose={() => { if (!busy) onClose() }} title={i18nT('components.artifactFolderDeleteDialog.delete_folder', { name: folder?.name ?? '' })} maxWidth={480}>
      <p className="text-sm text-text m-0">
        {i18nT('components.artifactFolderDeleteDialog.this_folder_contains')} {i18nT('components.artifactFolderDeleteDialog.artifact', { count: stats.artifactCount })}
        {stats.subfolderCount > 0 ? ` across ${i18nT('components.artifactFolderDeleteDialog.subfolder', { count: stats.subfolderCount })}` : ''}.
      </p>
      <div className="flex flex-col gap-2 mt-4">
        <Btn
          danger
          disabled={busy !== null}
          onClick={() => run(true)}
          className="justify-start text-left flex items-center gap-2"
        >
          {busy === 'cascade' ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
          <span>
            <span className="block font-medium">{i18nT('components.artifactFolderDeleteDialog.delete_folder_and_all_contents')}</span>
            <span className="block text-[12px] opacity-80">
              {i18nT('components.artifactFolderDeleteDialog.permanently_deletes_every_artifact_and_subfolder')}
            </span>
          </span>
        </Btn>
        <Btn
          disabled={busy !== null}
          onClick={() => run(false)}
          className="justify-start text-left flex items-center gap-2"
        >
          {busy === 'keep' ? <Loader2 size={14} className="animate-spin" /> : <FolderOutput size={14} />}
          <span>
            <span className="block font-medium">{i18nT('components.artifactFolderDeleteDialog.delete_folder_only_keep_artifacts')}</span>
            <span className="block text-[12px] opacity-80">
              {i18nT('components.artifactFolderDeleteDialog.contents_move_up_to')} {folder?.parent_id ? 'the parent folder' : 'the library root'}.
            </span>
          </span>
        </Btn>
        <Btn disabled={busy !== null} onClick={onClose} className="justify-center">
          {i18nT('components.artifactFolderDeleteDialog.cancel')}
        </Btn>
      </div>
    </Modal>
  )
}
