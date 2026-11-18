import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, Copy, KeyRound } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { copyCode } from '../utils/clipboard'
import { KasLoginEmbedded, SignInBackContext, type SignInBackSlot } from './KasLoginGate'
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from './ui/dialog'
import ErrorNotice from './ErrorNotice'
import { Btn, Card, IconButton } from './ui'

/** Closes the dialog once the flow reports a working sign-in. */
function CloseWhenSignedIn({ onDone }: { onDone: () => void }) {
  useEffect(() => { onDone() }, [onDone])
  return null
}

/**
 * The one Kiro sign-in dialog: Kiro Crew's own sign-in (the bundled engine's),
 * opened by every surface that offers it. Its body is the shared flow
 * (`KasLoginEmbedded`, driven by `hooks/useKasLogin`): the four choices, the
 * browser wait or the device code, cancel. It closes itself once a sign-in lands.
 * *reauth* opens the choices over a stored sign-in: a switch, or one Kiro refused.
 * Closing it mid-sign-in cancels the pending login (`useKasLoginFlow`).
 */
export function KiroSignInDialog({
  open,
  onOpenChange,
  reauth = false,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  reauth?: boolean
}) {
  const close = () => onOpenChange(false)
  // The device-code screen hands its back control to this header (next to the
  // title), so the way back to the choices sits where a dialog's back sits.
  const [back, setBack] = useState<{ onBack: () => void; disabled: boolean } | null>(null)
  const backSlot = useMemo<SignInBackSlot>(() => ({ set: setBack }), [])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent maxWidth={440} data-testid="kiro-sign-in-dialog">
        <DialogHeader>
          {back ? (
            <IconButton
              aria-label={i18nT('components.kasLogin.use_different_sign_in')}
              disabled={back.disabled}
              onClick={back.onBack}
              className="-ml-1"
              data-testid="kiro-sign-in-back"
            >
              <ArrowLeft size={16} aria-hidden="true" />
            </IconButton>
          ) : null}
          <DialogTitle>{i18nT('components.kasLogin.sign_in_to_kiro')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <SignInBackContext.Provider value={backSlot}>
          <KasLoginEmbedded
            bare
            startInReauth={reauth}
            renderPending={() => null}
            // A switch whose old account could not be removed stays open on its
            // warning, so the person reads it; any other landed sign-in closes it.
            renderAuthenticated={(_status, { replaceWarning }) => (replaceWarning ? null : <CloseWhenSignedIn onDone={close} />)}
          />
          </SignInBackContext.Provider>
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The signed-out line with its Sign in button, the one shape every chat surface
 * shows for Kiro Crew's own sign-in: before a first chat and under a failed one.
 * A failed turn's request id sits behind a copy control.
 */
export function KiroSignInStrip({
  title,
  reauth = false,
  requestId,
  testId,
}: {
  title: string
  reauth?: boolean
  requestId?: string
  testId?: string
}) {
  const [open, setOpen] = useState(false)
  // copyCode resolves false when both clipboard paths fail; then the id is shown
  // as selectable text with the failure, so it can still be copied by hand.
  const [copyFailed, setCopyFailed] = useState(false)
  const copyRequestId = async (id: string) => {
    setCopyFailed((await copyCode(id)) === false)
  }
  return (
    <Card className="mb-0 p-3 md:p-3 w-full min-w-0" data-testid={testId}>
      <div className="flex flex-wrap items-center gap-3">
        <p className="m-0 flex min-w-0 flex-1 items-center gap-2 text-[14px] font-semibold text-text-strong">
          <KeyRound className="lucide-inline text-accent" aria-hidden="true" />
          {title}
        </p>
        {requestId ? (
          <IconButton
            aria-label={i18nT('components.kiroSignInPrompt.copy_request_id')}
            onClick={() => void copyRequestId(requestId)}
            data-testid="kiro-sign-in-copy-request-id"
          >
            <Copy size={14} aria-hidden="true" />
          </IconButton>
        ) : null}
        <Btn primary onClick={() => setOpen(true)} data-testid="kiro-sign-in-open">
          {i18nT('components.kiroSignInPrompt.sign_in')}
        </Btn>
      </div>
      {requestId && copyFailed ? (
        <ErrorNotice
          className="mt-2"
          variant="inline"
          message={i18nT('components.kiroSignInPrompt.copy_request_id_failed', { id: requestId })}
          onDismiss={() => setCopyFailed(false)}
          noHandoffReason="the agent runs as the Kiro sign-in this strip reports as not working, so a chat it opens fails the same way; the id is shown to select by hand"
          testId="kiro-sign-in-copy-request-id-failed"
        />
      ) : null}
      <KiroSignInDialog open={open} onOpenChange={setOpen} reauth={reauth} />
    </Card>
  )
}

/** The request id a failed row's text carries, for the copy control. */
export function requestIdOf(text: string): string | undefined {
  return /request[_ ]id:\s*([0-9a-f-]{8,})/i.exec(text)?.[1]
}
