import { useEffect, useId, useRef, useState } from 'react'
import Modal from '../../components/Modal'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Input, SendBtn } from '../../components/ui'
import { useImeGuard } from '../../hooks/useImeGuard'
import { i18nT } from '../../i18n/t'

/** Refused in a name: the file goes in the dialog's folder and the upload keeps
 *  only the part after a separator, and the browser sends a `"` as `%22`. */
const BAD_CHARS = ['/', '\\', '"']

/**
 * The Files rail's "New file…" dialog: name a file and create it in `folder`.
 * A refusal (the name already exists, a type the upload does not accept) keeps
 * the dialog open with the name typed, so it can be corrected.
 */
export default function NewFileDialog({ folder, onCreate, onCreated, onClose }: {
  /** How the target directory is named in the title. */
  folder: string
  /** Creates the file; resolves its absolute path or the message to show.
   *  `retryable` marks a failure the same name may pass on a second try. */
  onCreate: (name: string) => Promise<{ path: string } | { error: string; retryable?: boolean }>
  onCreated: (path: string) => void
  onClose: () => void
}) {
  const ime = useImeGuard()
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  // The name the last refusal was for: Create stays off until it is changed,
  // so the button never looks like it could replace the file that exists.
  const [refused, setRefused] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Set synchronously on submit: `saving` disables Create only after the next
  // render, and a second Enter can land before it.
  const inFlight = useRef(false)
  const nameRef = useRef<HTMLInputElement>(null)
  const nameId = useId()
  const hintId = useId()
  // After Modal's own focus-in, which takes the header's close button.
  useEffect(() => {
    const raf = requestAnimationFrame(() => nameRef.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(raf)
  }, [])

  const trimmed = name.trim()
  const badChars = BAD_CHARS.some(c => trimmed.includes(c))
  const blocked = !trimmed || badChars || trimmed === refused
  const submit = async () => {
    if (blocked || inFlight.current) return
    inFlight.current = true
    // Keep focus in the dialog while Create is disabled, so the page's
    // shortcuts cannot act behind it.
    nameRef.current?.focus({ preventScroll: true })
    setSaving(true)
    setError(null)
    const result = await onCreate(trimmed)
    inFlight.current = false
    setSaving(false)
    if ('error' in result) {
      setError(result.error)
      if (!result.retryable) setRefused(trimmed)
      nameRef.current?.focus({ preventScroll: true })
      return
    }
    onCreated(result.path)
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={i18nT('pages.chat.fileBrowserRail.new_file_title', { folder })}
      maxWidth={360}
      guardAccidentalDismiss={!!trimmed}
      dismissDisabled={saving}
      footer={
        <>
          <Btn onClick={onClose} disabled={saving}>{i18nT('components.confirmDialog.cancel')}</Btn>
          <SendBtn onClick={() => { void submit() }} disabled={blocked || saving}>
            {i18nT('pages.chat.fileBrowserRail.new_file_create')}
          </SendBtn>
        </>
      }
    >
      <label htmlFor={nameId} className="flex flex-col gap-1.5">
        <span className="text-[11.5px] font-semibold text-muted">{i18nT('pages.chat.fileBrowserRail.new_file_name')}</span>
        <Input
          ref={nameRef}
          id={nameId}
          value={name}
          // Read-only rather than disabled, so it keeps focus while the create runs.
          readOnly={saving}
          aria-invalid={badChars || undefined}
          aria-describedby={badChars ? hintId : undefined}
          onChange={e => { setName(e.target.value); setError(null) }}
          {...ime.bindComposition()}
          onKeyDown={e => {
            if (e.key !== 'Enter') return
            if (ime.claimEnter(e)) void submit()
          }}
          className="w-full"
        />
      </label>
      {badChars && (
        <p id={hintId} className="mt-2 text-[12px] text-muted">
          {i18nT('pages.chat.fileBrowserRail.new_file_bad_chars', { chars: BAD_CHARS.join(' ') })}
        </p>
      )}
      {/* No hand-off: the typed file name is not saved anywhere yet. */}
      <ErrorNotice className="mt-3" message={error} testId="new-file-error" />
    </Modal>
  )
}
