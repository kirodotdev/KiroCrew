import { useEffect, useId, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { seedCreatedChatFolder } from '../api/chatFoldersWrite'
import { Btn, SendBtn, Input, Checkbox } from './ui'
import Modal from './Modal'
import ErrorNotice from './ErrorNotice'
import { useNewFolderMove } from '../hooks/useNewFolderMove'
import { useMoveSlotToFolderAsync } from '../hooks/useMoveSlotToFolder'
import { useImeGuard } from '../hooks/useImeGuard'
import { useConnected } from '../hooks/useConnected'
import { offlineProps } from '../utils/offline'
import { OfflineReasonRow } from './OfflineMenuReason'
import { store, useAppDispatch, useAppSelector } from '../store'
import { requestSlotFolderOpen } from '../store/chatSlice'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'
import type { ChatFolder } from '../types'

/**
 * The session menus' "Move to folder… → New folder…" dialog: name a folder,
 * create it, and move the session into it, so filing a session into a folder
 * that does not exist yet is one step instead of leaving the menu to create the
 * folder first.
 *
 * Rendered once per chat page, beside its provider, and opened through
 * `useNewFolderMove`. Single column at every width, so it needs no narrow branch.
 */
export default function NewFolderMoveDialog() {
  const ctx = useNewFolderMove()
  if (!ctx || ctx.slotKey === null) return null
  // Keyed by the slot so every opening starts from an empty draft.
  return <NewFolderMoveForm key={ctx.slotKey} slotKey={ctx.slotKey} onDone={ctx.close} />
}

/** The folder this dialog already created, kept so a failed move is retried
 *  into it rather than creating a second folder. `name` is the name the server
 *  stored, `parentId` where it was created and `parentName` that folder's name
 *  as the dialog showed it. */
type Created = { id: string; name: string; parentId?: string; parentName?: string }

function NewFolderMoveForm({ slotKey, onDone }: { slotKey: string; onDone: () => void }) {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const move = useMoveSlotToFolderAsync()
  const ime = useImeGuard()
  // The entry is refused offline, but the gateway can drop while the dialog is
  // open; the name stays typed and Create waits for the reconnect.
  const connected = useConnected()
  // Same key as the menus and the sidebar, so this reads their cache.
  const foldersQuery = useQuery<ChatFolder[]>({ queryKey: ['chat-folders'], queryFn: () => api.chatFolders() })
  const folders = foldersQuery.data ?? []
  // The session's folder when the dialog opened, read once: the move's optimistic
  // update rewrites the live value to the folder this dialog creates, which
  // would then offer that folder as its own parent while the move is pending.
  const [currentFolderId] = useState(() => store.getState().dashboard.slots.find(x => x.key === slotKey)?.folder_id || '')
  // A session already in a folder is offered the new folder inside that one.
  // Off by default, so the plain path matches the sidebar's own "New folder".
  // The name is kept once seen, so a refetch that briefly lacks the folder does
  // not take the offer (and a tick on it) away; the create then names the
  // snapshot id and the server answers for it.
  const listed = currentFolderId ? folders.find(f => f.id === currentFolderId) : undefined
  const [currentName, setCurrentName] = useState('')
  // Adjusting state while rendering, the React-documented form for state that
  // follows a prop or query value.
  if (listed && listed.name !== currentName) setCurrentName(listed.name)
  // A partial list (the first after a gateway restart, while sessions are being
  // restored) can leave this session out, so it does not count as loaded here.
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded && !s.dashboard.slotsListPartial)
  const sessionListed = useAppSelector(s => s.dashboard.slots.some(x => x.key === slotKey))
  const [name, setName] = useState('')
  const [inside, setInside] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Not a failure: nothing was asked of the server, so this is status text, not an ErrorNotice.
  const [closedHint, setClosedHint] = useState(false)
  const [saving, setSaving] = useState(false)
  const [created, setCreated] = useState<Created | null>(null)
  // Set synchronously on submit: `saving` disables the controls only after the
  // next render, and a second Enter or click can land before it.
  const inFlight = useRef(false)
  // Every dismissal path is refused while a write is in flight, so this form is
  // unmounted mid-write only by leaving the page. A write settling after that
  // must neither touch this form's state nor close the provider's dialog,
  // which by then may be open for another session.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  // Focus the name field on open, after Modal's focus-in (which takes the first
  // focusable, the header's close button) and after the menu that opened this
  // has torn down: rAF + preventScroll, as the sidebar's folder modal does.
  const nameRef = useRef<HTMLInputElement>(null)
  const nameId = useId()
  useEffect(() => {
    const raf = requestAnimationFrame(() => nameRef.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(raf)
  }, [])

  const submit = async () => {
    const trimmed = name.trim()
    // Held until the session list has loaded since the last (re)connect: before
    // that, the list cannot say whether this session still exists.
    if ((!trimmed && !created) || inFlight.current || !connected || !slotsLoaded) return
    // The session was closed while the dialog was open: there is nothing to
    // move, so create nothing.
    if (!store.getState().dashboard.slots.some(x => x.key === slotKey)) {
      setClosedHint(true)
      return
    }
    inFlight.current = true
    // Keep keyboard focus inside the dialog for the whole write. A click leaves
    // it on the button, which the re-render disables, and focus on <body> lets
    // the page's shortcuts (Settings, session switching) act behind the dialog.
    nameRef.current?.focus({ preventScroll: true })
    setSaving(true)
    setError(null)
    setClosedHint(false)
    let target = created
    if (target) {
      // A retry: the folder made on the first try may have been deleted since
      // (another tab, an agent). Moving into it would fail on every retry, so
      // a folder that is gone is created again instead.
      try {
        const list = await api.chatFolders() as ChatFolder[] | null
        const row = Array.isArray(list) ? list.find(f => f.id === target!.id) : undefined
        if (Array.isArray(list) && !row) {
          target = null
          if (mounted.current) setCreated(null)
        } else if (row) {
          // Renamed or re-parented since (deleting a parent lifts its children
          // to the top level): the notice below names it as it is now.
          const parentId = row.parent_id || undefined
          target = {
            id: target.id,
            name: row.name || target.name,
            parentId,
            parentName: parentId ? list!.find(f => f.id === parentId)?.name : undefined,
          }
          if (mounted.current) setCreated(target)
        }
      } catch {
        // The list could not be read; the move below reports what is wrong.
      }
    }
    if (!target) {
      const parentId = created ? created.parentId : (inside && currentFolderId ? currentFolderId : undefined)
      const parentName = created ? created.parentName : (parentId ? currentName : undefined)
      const folderName = created ? created.name : trimmed
      try {
        const row = await api.createChatFolder(folderName, parentId)
        // Seeded before the move so the sidebar can draw the session's new
        // folder on the next render instead of after the refetch.
        seedCreatedChatFolder(queryClient, row)
        const { id, name: stored } = (row ?? {}) as { id?: unknown; name?: unknown }
        if (typeof id !== 'string' || !id) throw new Error(i18nT('components.errorBoundary.something_went_wrong'))
        target = { id, name: typeof stored === 'string' && stored ? stored : folderName, parentId, parentName }
        if (mounted.current) setCreated(target)
      } catch (e) {
        inFlight.current = false
        if (!mounted.current) return
        // The dialog stays open with the name intact, so it can be corrected.
        setError(errMessage(e) || i18nT('components.errorBoundary.something_went_wrong'))
        setSaving(false)
        nameRef.current?.focus({ preventScroll: true })
        return
      }
    }
    try {
      // The same optimistic move, with rollback, as picking an existing folder,
      // but awaited: the dialog closes only once the server has the move.
      await move(slotKey, target.id)
    } catch (e) {
      inFlight.current = false
      if (!mounted.current) return
      // The folder exists now, so say so, and where: the retry moves into it and
      // creates nothing.
      const reason = errMessage(e) || i18nT('components.errorBoundary.something_went_wrong')
      setError(target.parentName
        ? i18nT('components.newFolderMoveDialog.move_failed_inside', { name: target.name, parent: target.parentName, error: reason })
        : i18nT('components.newFolderMoveDialog.move_failed', { name: target.name, error: reason }))
      setSaving(false)
      // Enter in the field retries, so focus goes back there.
      nameRef.current?.focus({ preventScroll: true })
      return
    }
    inFlight.current = false
    // Open the destination's path (a subfolder lands inside a parent that may be
    // collapsed), keeping the sidebar's view, search and filters as they are.
    // From the folder itself, so a retried move opens it too.
    if (target.parentId) dispatch(requestSlotFolderOpen({ key: slotKey, folder_id: target.id }))
    if (mounted.current) onDone()
  }

  const submitLabel = created
    ? i18nT('components.newFolderMoveDialog.retry_move')
    : i18nT('components.newFolderMoveDialog.create_and_move')
  // Once the folder exists only the move is left, so offline that is what waits.
  const offlineVerb = created ? i18nT('utils.offline.move_sessions') : i18nT('utils.offline.create_folders')
  const offlineReasonId = useId()
  return (
    <Modal
      open
      onClose={onDone}
      title={i18nT('components.newFolderMoveDialog.title')}
      maxWidth={360}
      // A typed name is unsaved input; a write in flight must not be orphaned.
      guardAccidentalDismiss={!!name.trim() || !!created}
      dismissDisabled={saving}
      footer={
        <>
          <Btn onClick={onDone} disabled={saving}>{i18nT('components.newFolderMoveDialog.cancel')}</Btn>
          <SendBtn
            onClick={() => { void submit() }}
            {...offlineProps(connected, offlineVerb, submitLabel)}
            aria-describedby={connected ? undefined : offlineReasonId}
            // Held, too, while the closed-session note stands: there is nothing to move.
            disabled={(!name.trim() && !created) || saving || !connected || !slotsLoaded || (closedHint && !sessionListed)}
          >
            {submitLabel}
          </SendBtn>
        </>
      }
    >
      <label htmlFor={nameId} className="flex flex-col gap-1.5">
        <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.newFolderMoveDialog.name_label')}</span>
        <Input
          ref={nameRef}
          id={nameId}
          value={name}
          // Read-only rather than disabled, so it keeps keyboard focus (and the
          // dialog its key isolation) while a write is in flight. Greyed once
          // the folder exists, since the retry moves into that folder.
          readOnly={saving || !!created}
          onChange={e => setName(e.target.value)}
          {...ime.bindComposition()}
          onKeyDown={e => {
            if (e.key !== 'Enter') return
            if (ime.claimEnter(e)) void submit()
          }}
          className={created ? 'w-full opacity-60 cursor-not-allowed' : 'w-full'}
        />
      </label>
      {currentFolderId && currentName && (
        <label className="mt-3 flex items-start gap-2 text-[13px] text-text cursor-pointer min-w-0">
          <Checkbox checked={inside} disabled={saving || !!created} onChange={e => setInside(e.target.checked)} style={{ marginTop: 2 }} />
          <span className="min-w-0 break-words">{i18nT('components.newFolderMoveDialog.inside_current', { name: currentName })}</span>
        </label>
      )}
      {/* The reason Create is held, in the dialog rather than only in the button's tooltip. */}
      {!connected && (
        <OfflineReasonRow
          testId="new-folder-move-offline"
          id={offlineReasonId}
          className="mt-3"
          message={i18nT('utils.offline.gateway_offline_reconnect', { action: offlineVerb })}
        />
      )}
      {/* Only while the session is still missing, so a session that reappears
          clears it, and not offline, where the list cannot say either way. */}
      {closedHint && !sessionListed && connected && (
        <p role="status" className="mt-3 text-[13px] text-muted" data-testid="new-folder-move-closed">
          {i18nT('components.newFolderMoveDialog.session_closed')}
        </p>
      )}
      {/* No hand-off: the typed folder name, or the move still to be retried, is not saved anywhere yet. */}
      <ErrorNotice className="mt-3" message={error} testId="new-folder-move-error" />
      {/* No hand-off: the typed folder name is not saved anywhere yet. */}
      <ErrorNotice
        className="mt-3"
        message={foldersQuery.error ? (errMessage(foldersQuery.error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
        testId="new-folder-move-folders-error"
      />
    </Modal>
  )
}
