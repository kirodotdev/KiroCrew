/** The sidebar's one folder create / settings modal. Its submit awaits the
 *  write and closes only on success, and an edit sends only the fields the
 *  person touched. */
import FolderConfigModal from '../../components/FolderConfigModal'
import { requestFolderReveal } from '../../store/chatSlice'
import { safeSetItem } from '../../utils/safeStorage'
import { FLAT_VIEW_LS_KEY } from './persistence'
import type { FolderModalTarget, SidebarScope } from './scope'

/** The modal for the folder the sidebar targets. */
export function renderFolderModal(shell: SidebarScope, folderModal: FolderModalTarget): React.ReactNode {
  const { createFolderMutation, defaultAgent, dispatch, flatView, folders, installedAgents, refetchTags, setFlatView,
    setFolderModal, tagsData, tagsQueryFailed, updateFolderMutation } = shell
  return (
        <FolderConfigModal
          open={true}
          mode={folderModal.mode}
          parentId={folderModal.mode === 'create' ? folderModal.parentId : undefined}
          folder={folderModal.mode === 'edit' ? folders.find(f => f.id === folderModal.folderId) : undefined}
          folders={folders}
          installedAgents={installedAgents}
          globalDefaultAgent={defaultAgent}
          availableTags={tagsData}
          availableTagsFailed={tagsQueryFailed}
          onRetryTags={() => { void refetchTags() }}
          onClose={() => setFolderModal(null)}
          onSubmit={async draft => {
            // AWAIT the mutation and only close on success. The backend rejects a
            // free-typed project_dir (not absolute / not an existing directory /
            // sensitive) and a multi-emoji icon with a 400; closing optimistically
            // discarded the whole draft with no feedback. Rethrowing lets the modal
            // stay open and render the reason.
            if (folderModal.mode === 'create') {
              const created = await createFolderMutation.mutateAsync({
                name: draft.name,
                parentId: folderModal.parentId || undefined,
                projectDir: draft.projectDir,
                defaultAgent: draft.defaultAgent,
                color: draft.color,
                icon: draft.icon,
                tags: draft.tags,
                steeringDirs: draft.steeringDirs,
              })
              // Creating a folder while flat view is on would otherwise appear
              // to do nothing (flat rendering skips folder blocks in both the
              // list and the board columns). Exit flat view so the new folder
              // is visible, whichever entry point created it.
              if (flatView) {
                setFlatView(false)
                safeSetItem(FLAT_VIEW_LS_KEY, '0')
              }
              // A subfolder lands inside its parent, and a collapsed parent would
              // hide it, so the create looked like it had done nothing. The folder
              // reveal opens the parent and every collapsed ancestor, scrolls the
              // new row into view and flashes it.
              const createdId = (created as { id?: unknown } | null)?.id
              if (folderModal.parentId && typeof createdId === 'string' && createdId) {
                dispatch(requestFolderReveal(createdId))
              }
            } else {
              // Build the PATCH from what the USER edited (draft.touched, measured
              // against what the modal opened with) — NOT from a diff against live
              // cache, whose shape would revert any field another client changed
              // mid-edit.
              const touched = new Set(draft.touched)
              const body: Record<string, unknown> = {}
              if (touched.has('name')) body.name = draft.name
              if (touched.has('projectDir')) body.project_dir = draft.projectDir
              if (touched.has('defaultAgent')) body.default_agent = draft.defaultAgent
              // '' is a legitimate color instruction: it clears back to gray.
              if (touched.has('color')) body.color = draft.color
              // regenerate_icon and a manual icon are mutually exclusive on the
              // backend; the modal keeps them exclusive in the draft, and this
              // branch keeps them exclusive on the wire.
              if (draft.regenerateIcon) {
                body.regenerate_icon = true
              } else if (touched.has('icon')) {
                // '' clears back to the default glyph.
                body.icon = draft.icon
              }
              // An empty array is a legitimate instruction too: it clears the
              // folder's tags.
              if (touched.has('tags')) body.tags = draft.tags
              // '' / [] clears here as well: PATCH steering_dirs:[] removes the
              // folder's extra steering directories (server resolves effective
              // dirs from folder_id, so nothing resolved is sent).
              if (touched.has('steeringDirs')) body.steering_dirs = draft.steeringDirs
              if (Object.keys(body).length > 0) {
                await updateFolderMutation.mutateAsync({ id: folderModal.folderId, body })
              }
            }
            setFolderModal(null)
          }}
        />
  )
}
