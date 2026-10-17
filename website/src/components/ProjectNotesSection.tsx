import { useCallback, useEffect, useState } from 'react'
import { ArrowUp, X, StickyNote } from 'lucide-react'
import { apiTransport } from '../api/apiTransport'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import SearchableSelect from './SearchableSelect'
import { i18nT } from '../i18n/t'

/** One learned project note as the read route returns it. */
interface ProjectNote {
  id: string
  text: string
  tags: string[]
  created_at: string
}

interface NotesResponse {
  notes: ProjectNote[]
  namespace: string
}

interface Props {
  /** The folder whose project's notes to show; its project is resolved
   *  server-side from this id. */
  folderId: string
  /** The folder's own declared steering directories — the only places a note
   *  may be promoted into. Empty disables promote with an explaining hint. */
  steeringDirs: string[]
}

/**
 * The "Project notes" section of the folder-settings dialog.
 *
 * Learned project notes are reference DATA an agent recorded about this folder's
 * project; a later session finds them first when it searches. This section lets
 * the PERSON see them, delete one, and PROMOTE one into a standing instruction —
 * the one data->instruction crossing, reserved to a person, which is why it
 * lives in the person's own settings dialog and not anywhere an agent reaches.
 *
 * It reuses the generic knowledge item routes for what they already do: the
 * folder->project read (`GET /api/knowledge/project-notes?folder_id=`) hands back
 * the project namespace and the notes, and a delete goes through
 * `DELETE /api/knowledge/items/{id}`. Promote is the project-notes route, refused
 * to any non-person principal server-side.
 */
export default function ProjectNotesSection({ folderId, steeringDirs }: Props) {
  const [notes, setNotes] = useState<ProjectNote[] | null>(null)
  const [loadErr, setLoadErr] = useState('')
  const [actionErr, setActionErr] = useState('')
  const [busyId, setBusyId] = useState('')
  // Which note's promote picker is open, and the directory chosen in it.
  const [promoteId, setPromoteId] = useState('')
  const [promoteDir, setPromoteDir] = useState('')

  const load = useCallback(async () => {
    setLoadErr('')
    try {
      const r = await apiTransport.get(
        `/api/knowledge/project-notes?folder_id=${encodeURIComponent(folderId)}`,
      )
      if (!r.ok) throw new Error(String(r.status))
      const data = (await apiTransport.j(r)) as NotesResponse
      setNotes(Array.isArray(data?.notes) ? data.notes : [])
    } catch {
      setNotes(null)
      setLoadErr(i18nT('components.folderConfigModal.notes_load_failed'))
    }
  }, [folderId])

  useEffect(() => {
    if (folderId) void load()
  }, [folderId, load])

  const onDelete = useCallback(
    async (id: string) => {
      setActionErr('')
      setBusyId(id)
      try {
        const r = await apiTransport.del(`/api/knowledge/items/${encodeURIComponent(id)}`)
        if (!r.ok) throw new Error(String(r.status))
        setNotes(prev => (prev ? prev.filter(n => n.id !== id) : prev))
      } catch {
        setActionErr(i18nT('components.folderConfigModal.notes_delete_failed'))
      } finally {
        setBusyId('')
      }
    },
    [],
  )

  const onPromote = useCallback(
    async (id: string, dir: string) => {
      setActionErr('')
      setBusyId(id)
      try {
        const r = await apiTransport.post(
          `/api/knowledge/project-notes/${encodeURIComponent(id)}/promote`,
          { folder_id: folderId, steering_dir: dir },
        )
        if (!r.ok) throw new Error(String(r.status))
        setPromoteId('')
        setPromoteDir('')
      } catch {
        setActionErr(i18nT('components.folderConfigModal.notes_promote_failed'))
      } finally {
        setBusyId('')
      }
    },
    [folderId],
  )

  // Nothing to show until the read resolves; a project with no notes renders the
  // empty-state line so the section is never a bare heading.
  return (
    <div className="flex flex-col gap-1.5" data-testid="folder-config-project-notes">
      <span className="flex items-center gap-1.5 text-[11.5px] font-semibold text-muted">
        <StickyNote size={12} className="shrink-0" />{' '}
        {i18nT('components.folderConfigModal.project_notes')}
      </span>
      {loadErr ? (
        <div className="flex flex-col gap-1.5" data-testid="folder-config-notes-load-error">
          <ErrorNotice message={loadErr} />
          <div>
            <Btn data-testid="folder-config-notes-retry" onClick={() => void load()}>
              {i18nT('components.folderConfigModal.notes_retry')}
            </Btn>
          </div>
        </div>
      ) : notes === null ? (
        <span className="text-[11px] text-muted-strong">
          {i18nT('components.folderConfigModal.notes_loading')}
        </span>
      ) : notes.length === 0 ? (
        <span className="text-[11px] text-muted-strong" data-testid="folder-config-notes-empty">
          {i18nT('components.folderConfigModal.no_project_notes')}
        </span>
      ) : (
        <div className="flex flex-col gap-1.5">
          {notes.map(note => (
            <div
              key={note.id}
              data-testid={`folder-config-note-${note.id}`}
              className="flex flex-col gap-1.5 bg-bg-elevated border border-border rounded-lg px-2.5 py-2"
            >
              <div className="flex items-start gap-2">
                <span className="flex-1 min-w-0 text-[12px] text-text whitespace-pre-wrap break-words">
                  {note.text}
                </span>
                <button
                  type="button"
                  data-testid={`folder-config-note-promote-${note.id}`}
                  aria-label={i18nT('components.folderConfigModal.notes_promote')}
                  disabled={busyId === note.id || steeringDirs.length === 0}
                  onClick={() => {
                    setPromoteId(note.id === promoteId ? '' : note.id)
                    setPromoteDir(steeringDirs[0] || '')
                  }}
                  className="shrink-0 p-0.5 text-muted hover:text-accent rounded hover:bg-bg-hover cursor-pointer bg-transparent border-none disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <ArrowUp size={13} />
                </button>
                <button
                  type="button"
                  data-testid={`folder-config-note-delete-${note.id}`}
                  aria-label={i18nT('components.folderConfigModal.notes_delete')}
                  disabled={busyId === note.id}
                  onClick={() => void onDelete(note.id)}
                  className="shrink-0 p-0.5 text-muted hover:text-danger rounded hover:bg-bg-hover cursor-pointer bg-transparent border-none disabled:opacity-40"
                >
                  <X size={13} />
                </button>
              </div>
              {promoteId === note.id && (
                <div
                  className="flex items-center gap-2"
                  data-testid={`folder-config-note-promote-row-${note.id}`}
                >
                  <SearchableSelect
                    aria-label={i18nT('components.folderConfigModal.notes_promote_target')}
                    options={steeringDirs.map(d => ({ value: d, label: d }))}
                    value={promoteDir}
                    onChange={setPromoteDir}
                  />
                  <Btn
                    data-testid={`folder-config-note-promote-confirm-${note.id}`}
                    disabled={!promoteDir || busyId === note.id}
                    onClick={() => void onPromote(note.id, promoteDir)}
                  >
                    {i18nT('components.folderConfigModal.notes_promote')}
                  </Btn>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {actionErr && <ErrorNotice message={actionErr} />}
      <span className="text-[11px] text-muted-strong">
        {steeringDirs.length === 0
          ? i18nT('components.folderConfigModal.notes_promote_needs_steering')
          : i18nT('components.folderConfigModal.project_notes_hint')}
      </span>
    </div>
  )
}
