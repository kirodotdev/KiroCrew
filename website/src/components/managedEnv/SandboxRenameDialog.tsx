/**
 * The "Rename…" dialog on a managed environment's crew row: the crew name, by the crew-name
 * rule (2 to 32 lowercase letters, digits or single hyphens; a letter first, a letter or digit
 * last). It only asks; SandboxCrewRowDetail posts `POST /api/managed-environments/{id}/rename`,
 * whose server checks the rule again. The service's answer decides: every dashboard gets the
 * name, or none does. A failed rename shows here, and the dialog stays open with the old name
 * back in the field.
 */
import { useEffect, useState } from 'react'
import { Btn } from '../ui'
import ErrorNotice from '../ErrorNotice'
import Modal from '../Modal'
import { RENAME_TEXT, renameDialogTitle } from './text'

/** A failed rename's line, with a count so the same line twice still resets the field. */
export interface RenameFailure {
  text: string
  seq: number
}

/** The engine's crew-name rule (kiro_crew/managed_env/engine.py validate_crew_name). */
export function crewNameOk(name: string): boolean {
  return /^[a-z][a-z0-9-]{0,30}[a-z0-9]$/.test(name) && !name.includes('--')
}

export default function SandboxRenameDialog({
  sandboxId,
  crewName,
  open,
  busy,
  failure = null,
  onClose,
  onSave,
}: {
  sandboxId: string
  crewName: string
  open: boolean
  busy: boolean
  failure?: RenameFailure | null
  onClose: () => void
  onSave: (name: string) => void
}) {
  const [text, setText] = useState(crewName)
  useEffect(() => {
    if (open) setText(crewName)
  }, [open, crewName])
  // a failed rename: the old name back in the field (nothing was renamed)
  useEffect(() => {
    if (failure) setText(crewName)
  }, [failure, crewName])
  const name = text.trim()
  const ok = crewNameOk(name)
  const canSave = ok && !busy
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={renameDialogTitle(crewName)}
      maxWidth={440}
      footer={
        <>
          <Btn onClick={onClose}>{RENAME_TEXT.cancel}</Btn>
          <Btn
            primary
            disabled={!canSave}
            onClick={() => canSave && onSave(name)}
            data-testid={`sandbox-rename-save-${sandboxId}`}
          >
            {RENAME_TEXT.save}
          </Btn>
        </>
      }
    >
      <div className="space-y-3" data-testid={`sandbox-rename-dialog-${sandboxId}`}>
        <label className="block">
          <span className="text-[12px] font-medium text-muted">{RENAME_TEXT.label}</span>
          <input
            value={text}
            onChange={e => setText(e.target.value)}
            aria-invalid={!ok}
            className="mt-1 w-full font-mono"
            aria-label={RENAME_TEXT.label}
            data-testid={`sandbox-rename-input-${sandboxId}`}
          />
        </label>
        <p className="text-[12px] text-muted">{RENAME_TEXT.hint}</p>
        <ErrorNotice variant="inline" message={failure?.text} testId={`sandbox-rename-error-${sandboxId}`} />
        {!ok ? (
          <p className="text-[12px] text-danger" data-testid={`sandbox-rename-refused-${sandboxId}`}>
            {RENAME_TEXT.rule}
          </p>
        ) : null}
      </div>
    </Modal>
  )
}
