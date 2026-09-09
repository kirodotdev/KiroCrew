import { useState } from 'react'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogBody,
  DialogFooter,
  DialogTitle,
  DialogDescription,
} from '../../components/ui/dialog'
import { Btn, Input } from '../../components/ui'
import { i18nT } from '../../i18n/t'

// "Create skill" is a session-level action, opened from the chat header menu (it
// captures the whole current session, not one message). This is the controlled
// modal that collects the purpose; the caller owns the open state.
export default function CreateSkillDialog({
  open,
  onOpenChange,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: (purpose: string) => void | Promise<void>
}) {
  const [draft, setDraft] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Purpose is mandatory: it captures user intent, so an empty description gives
  // the authoring subagent nothing to disambiguate against the transcript.
  const canSubmit = draft.trim().length > 0 && !submitting

  const submit = async () => {
    if (draft.trim().length === 0 || submitting) return
    setSubmitting(true)
    try {
      await onSubmit(draft.trim())
      // Clear + close ONLY after the request succeeds, so a capacity/network
      // failure preserves the typed purpose and keeps the dialog open to retry.
      setDraft('')
      onOpenChange(false)
    } catch {
      // Submission failed (the caller surfaces the error); keep the dialog open
      // with the draft intact so the entered purpose is never lost.
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={o => {
        // Ignore Escape / outside-click closes while a submission is in flight, so the
        // typed purpose is never discarded before the request resolves.
        if (submitting) return
        onOpenChange(o)
        if (!o) setDraft('')
      }}
    >
      <DialogContent maxWidth={420}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.chat.assistantMessage.create_skill')}</DialogTitle>
          <DialogDescription>
            {i18nT('pages.chat.assistantMessage.create_skill_hint')}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {/*
            Password managers (1Password, LastPass, Bitwarden, Chrome autofill) decorate
            plain text inputs with an inline "import"/save affordance that reads as a
            second submit control. The data-* opt-outs suppress those injected UIs;
            autoComplete="off" backs them up for browsers that honour it.
          */}
          <Input
            autoFocus
            required
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                e.preventDefault()
                submit()
              }
            }}
            placeholder={i18nT('pages.chat.assistantMessage.create_skill_placeholder')}
            aria-label={i18nT('pages.chat.assistantMessage.create_skill_placeholder')}
            className="w-full"
            autoComplete="off"
            data-1p-ignore
            data-lpignore="true"
            data-form-type="other"
          />
        </DialogBody>
        <DialogFooter>
          <Btn primary onClick={submit} disabled={!canSubmit}>
            {i18nT('pages.chat.assistantMessage.create_skill_submit')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
