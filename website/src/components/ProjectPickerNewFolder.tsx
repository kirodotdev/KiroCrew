import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { FolderPlus, X } from 'lucide-react'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import type { useImeGuard } from '../hooks/useImeGuard'
import { parseErrorCode, reportForError } from '../utils/errorReport'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { i18nT } from '../i18n/t'

/**
 * The project picker's New folder control, in two parts: the toggle that sits beside the
 * Browse pane's favourite star, and the name field it opens on its own row beneath. A
 * create makes one folder on the gateway host (`POST /api/browse-dirs/create`) inside the
 * directory the pane shows, then hands the new path back so the pane lists it and the user
 * can Select it, the same two steps that used to need a terminal.
 */

// One sentence per refusal the route names. `not_a_directory` and `not_found` are the same
// fact to the user (the folder they are looking at is gone), so they share copy. Anything
// else -- a busy probe pool, a disk error, a dropped connection -- takes the generic arm.
const CREATE_FAILED_KEYS: Record<string, string> = {
  invalid_name: 'components.projectPicker.new_folder_invalid_name',
  name_too_long: 'components.projectPicker.new_folder_too_long',
  already_exists: 'components.projectPicker.new_folder_exists',
  access_denied: 'components.projectPicker.new_folder_protected',
  permission_denied: 'components.projectPicker.new_folder_no_permission',
  not_a_directory: 'components.projectPicker.new_folder_parent_gone',
  not_found: 'components.projectPicker.new_folder_parent_gone',
  parent_changed: 'components.projectPicker.new_folder_parent_changed',
}

function createFailureKey(err: unknown): string {
  const code = err instanceof ApiError && !err.authRequired ? parseErrorCode(err.body) : undefined
  return (code && CREATE_FAILED_KEYS[code]) || 'components.projectPicker.new_folder_failed'
}

/** The toggle. Pressed while the name field is open; a second press closes it. */
export function NewFolderToggle({ open, parent, onToggle }: { open: boolean; parent: string; onToggle: () => void }) {
  return (
    <Btn
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      aria-controls="pp-new-folder"
      // Shift+Enter in the Browse path field opens the same field; the tooltip names it too.
      aria-keyshortcuts="Shift+Enter"
      title={i18nT('components.projectPicker.new_folder_hint', { path: parent })}
      data-testid="pp-new-folder-toggle"
      className={`ml-auto shrink-0 gap-1 px-1.5 py-1 text-[11px] border-transparent rounded hover:border-transparent hover:bg-bg-hover active:scale-100 ${open ? 'text-accent' : 'text-muted'}`}
    >
      <FolderPlus size={13} className="shrink-0" aria-hidden />
      <span>{i18nT('components.projectPicker.new_folder')}</span>
    </Btn>
  )
}

/**
 * The name field. Enter or Create makes the folder; Escape or the close control leaves
 * without one. The name is trimmed before it is sent, since the route refuses surrounding
 * whitespace rather than rewrite what the user typed.
 *
 * The success callback rides `mutate`, not the hook: a create that answers after the user
 * closed the field or moved to another folder must not pull the pane back to it.
 */
export function NewFolderForm({ parent, ime, onCreated, onCancel }: {
  parent: string
  /** The picker's own IME guard, shared rather than a second document-tracked latch. */
  ime: ReturnType<typeof useImeGuard>
  onCreated: (path: string) => void
  onCancel: () => void
}) {
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: (vars: { parent: string; name: string }) => api.createBrowseDir(vars.parent, vars.name),
  })
  const trimmed = name.trim()
  const busy = create.isPending
  const canCreate = !!trimmed && !busy
  const submit = () => {
    if (!canCreate) return
    create.mutate({ parent, name: trimmed }, { onSuccess: d => onCreated(d.path) })
  }
  const failed = create.isError && create.variables
    ? i18nT(createFailureKey(create.error), { name: create.variables.name, path: create.variables.parent })
    : null
  return (
    <div id="pp-new-folder" className="px-2 py-1.5 border-b border-border flex flex-col gap-1.5" aria-busy={busy || undefined}>
      <div className="flex items-center gap-1.5">
        <FolderPlus size={13} className="text-accent shrink-0" aria-hidden />
        <input
          autoFocus
          type="text"
          aria-label={i18nT('components.projectPicker.new_folder_name')}
          placeholder={i18nT('components.projectPicker.new_folder_name')}
          value={name}
          maxLength={255}
          onChange={e => { setName(e.target.value); if (create.isError) create.reset() }}
          {...ime.bindComposition()}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              if (ime.claimEnter(e)) submit()
            } else if (e.key === 'Escape') {
              // Claimed here so the Escape closes this field only: left to bubble it would reach
              // the popover's and the host modal's own dismissal and close everything at once.
              if (!ime.claimKey(e)) return
              e.preventDefault(); e.stopPropagation(); onCancel()
            }
          }}
          data-testid="pp-new-folder-name"
          className="flex-1 min-w-0 bg-bg-elevated border border-border rounded px-2 py-1 text-[13px] font-mono text-text placeholder:text-muted focus:outline-hidden focus-visible:border-accent"
        />
        <Btn
          type="button"
          onClick={submit}
          disabled={!canCreate}
          data-testid="pp-new-folder-create"
          className="shrink-0 px-2 py-1 text-[11px] bg-accent/20 text-accent border-transparent rounded hover:bg-accent/30 hover:border-transparent active:scale-100"
        >
          {busy ? i18nT('components.projectPicker.new_folder_creating') : i18nT('components.projectPicker.new_folder_create')}
        </Btn>
        <Btn
          type="button"
          onClick={onCancel}
          aria-label={i18nT('components.projectPicker.new_folder_cancel')}
          title={i18nT('components.projectPicker.new_folder_cancel')}
          data-testid="pp-new-folder-cancel"
          className="shrink-0 p-1 text-muted border-transparent rounded hover:text-text hover:bg-bg-hover hover:border-transparent active:scale-100"
        >
          <X size={13} aria-hidden />
        </Btn>
      </div>
      {failed && (
        <>
          {/* No hand-off: the field above holds the folder name being typed, and the hand-off
              navigates to the chat and unmounts the picker with it. The remedies are all here:
              edit the name, or close the field and pick another folder. */}
          <ErrorNotice
            variant="inline"
            className="whitespace-normal"
            report={reportForError(create.error)}
            message={failed}
            testId="pp-new-folder-error"
          />
        </>
      )}
    </div>
  )
}
