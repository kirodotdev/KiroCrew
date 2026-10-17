import { useEffect, useRef, useState } from 'react'

/** How long the folder-name "Saved" confirmation stays up. Matches the explicit
 *  -save channel panels so the affordance reads as one behavior across Settings. */
const SAVED_MS = 6000

/**
 * The save-on-change plumbing shared by the QR-paired channel panels.
 *
 * Extracted because WeChat's and WhatsApp's panels carried byte-identical copies
 * of it: both authenticate by QR rather than by a pasted token, so the second
 * panel was written from the first. What was duplicated is not product-specific
 * though -- it is a folder field plus save sequencing that any channel panel
 * could want -- so this is shared rather than exempted from the duplicate gate.
 *
 * Three invariants live here, and none of them is guessable from the call site:
 *
 * 1. **The last ACCEPTED name is kept apart from the editable draft.** A draft
 *    may hold a value the server rejected, and that text is deliberately
 *    preserved so the user can correct it. Re-enabling the setting must persist
 *    a name that is known good, so it reads the accepted name, never the draft.
 *    An empty value is never recorded as accepted: `""` is how the backend
 *    encodes the setting being OFF, not a folder name.
 * 2. **Only folder-bearing patches take a sequence ticket.** Clicking any other
 *    control is what BLURS the name field, so "rename, then click the DM-policy
 *    picker" lands both saves in one gesture. If the orthogonal save took the
 *    ticket, the rename's rejection would always arrive superseded and the field
 *    would silently keep a name the server refused.
 * 3. **Clearing an error is ownership-aware.** A rename's rejection races an
 *    orthogonal control's save, with no ordering guarantee between two in-flight
 *    requests. An unconditional clear lets whichever success lands last erase a
 *    folder rejection it has no claim over, so a folder save may always clear
 *    the slot while an orthogonal success may clear only an error it could have
 *    produced itself.
 */
export interface ChannelFolderSave<TPatch> {
  /** Whether the name field is showing. Distinct from "a name is saved": the
   *  toggle reveals the field without persisting anything. */
  folderOn: boolean
  /** The editable draft. Committed on blur / Enter, never per keystroke. */
  folderName: string
  setFolderName: (value: string) => void
  /** Transient confirmation that a name commit landed. These panels have no Save
   *  button, so without it a rename succeeds invisibly. */
  folderSaved: boolean
  /** Server-reported rejection for the control that owns the slot. */
  saveError: string
  /** Toggle handler: reveals or clears the folder, persisting either the last
   *  accepted name or `""`. */
  toggleFolder: (on: boolean) => void
  /** Blur / Enter handler: commits the draft, falling back to the default name. */
  commitFolderName: () => void
  /** Persist any patch. Folder-bearing patches participate in the sequencing
   *  and error-ownership rules above; everything else bypasses them. */
  save: (patch: Partial<TPatch>, onRevert?: () => void, onSaved?: () => void) => void
}

export function useChannelFolderSave<TPatch extends { session_folder?: string }>(opts: {
  /** The folder name the server currently holds; `""` or undefined means off. */
  serverFolder: string | undefined
  /** Fallback name when the field is enabled or committed empty (the brand name). */
  defaultName: string
  /** The panel's mutation. Rejections surface through `saveError`. */
  mutate: (patch: Partial<TPatch>) => Promise<unknown>
}): ChannelFolderSave<TPatch> {
  const { serverFolder, defaultName, mutate } = opts

  const acceptedName = useRef('')
  const [folderName, setFolderName] = useState('')
  useEffect(() => {
    // Tracked only while the server HAS a name: switching the setting off
    // persists "", and treating that as the accepted name would discard a
    // custom folder on every off/on round trip.
    if (serverFolder) {
      acceptedName.current = serverFolder
      setFolderName(serverFolder)
    }
  }, [serverFolder])

  // Re-seeded from the server so an external edit (or a save that cleared the
  // name) is reflected.
  const [folderOn, setFolderOn] = useState(false)
  useEffect(() => {
    setFolderOn(!!serverFolder)
  }, [serverFolder])

  const [saveError, setSaveError] = useState('')
  const [folderSaved, setFolderSaved] = useState(false)
  const folderSavedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(folderSavedTimer.current), [])

  const saveSeq = useRef(0)
  const folderError = useRef(false)

  const save = (
    patch: Partial<TPatch>,
    onRevert?: () => void,
    onSaved?: () => void,
  ) => {
    const touchesFolder = 'session_folder' in patch
    const seq = touchesFolder ? ++saveSeq.current : saveSeq.current
    const latest = () => !touchesFolder || seq === saveSeq.current
    void mutate(patch)
      .then(() => {
        // A committed save is the second authority on what the server holds, and
        // it must be recorded here rather than left to the refetch: the query
        // does not retry, so a refetch that fails leaves the data stale, the seed
        // effect never fires, and a later off/on would persist the superseded
        // name over a rename the server had already accepted. Recorded even for
        // a superseded call: any name the server accepted is a legitimate
        // known-good fallback for re-enabling.
        const next = patch.session_folder
        if (typeof next === 'string' && next) acceptedName.current = next
        if (!latest()) return
        if (touchesFolder || !folderError.current) {
          setSaveError('')
          folderError.current = false
        }
        onSaved?.()
      })
      .catch((e: unknown) => {
        if (latest()) {
          // Without this the folder-name validation (rejects "/", "\", control
          // characters, over-long names) rejects the value server-side while the
          // input keeps the typed text and the user is told nothing.
          setSaveError(e instanceof Error && e.message ? e.message : String(e))
          folderError.current = touchesFolder
          // A "Saved" check from an earlier commit must not sit next to a fresh
          // error: the pair reads as the failed value having been saved.
          setFolderSaved(false)
        }
        onRevert?.()
      })
  }

  const toggleFolder = (on: boolean) => {
    setFolderOn(on)
    // A toggle supersedes any in-flight rename (its own save advances the
    // sequence); clearing the flag here keeps a still-armed "Saved." from
    // surviving the field's unmount and repainting on the next turn-on, which
    // would be a false confirmation since the last completed write by then is
    // the off-patch that cleared the name.
    clearTimeout(folderSavedTimer.current)
    setFolderSaved(false)
    // Enabling persists the last accepted name, never the draft, which can hold
    // a value the server rejected. Reusing a rejected draft makes every enable
    // attempt fail while the field it lives in is hidden, leaving no way to
    // correct it. Resetting the draft to the same value keeps the revealed field
    // showing what was persisted.
    const next = on ? acceptedName.current || defaultName : ''
    if (on) setFolderName(next)
    save({ session_folder: next } as Partial<TPatch>, () => setFolderOn(!!serverFolder))
  }

  const commitFolderName = () => {
    save({ session_folder: folderName.trim() || defaultName } as Partial<TPatch>, undefined, () => {
      clearTimeout(folderSavedTimer.current)
      setFolderSaved(true)
      folderSavedTimer.current = setTimeout(() => setFolderSaved(false), SAVED_MS)
    })
  }

  return {
    folderOn,
    folderName,
    setFolderName,
    folderSaved,
    saveError,
    toggleFolder,
    commitFolderName,
    save,
  }
}
