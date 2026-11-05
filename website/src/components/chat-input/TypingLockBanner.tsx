/** The one place a locked composer says who is typing: "<name> is typing…", or
 *  "<name> took over typing" when they took the lease from this user. It
 *  replaces the composer's button row while another collaborator types in the
 *  same shared session. No motion and no colour alarm; the read-only draft above
 *  it is this user's own and stays as it was. With the Shared Crew preview on it
 *  also offers Take over, and a failed takeover shows under it. */
import { Btn } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import type { ComposerTypingLockView } from './props'

/** The short label a pending approval or question card carries when another
 *  person's turn raised it ("Raised by Kyle"). Draws nothing for an empty name. */
export function RaisedBy({ name }: { name: string }) {
  if (!name) return null
  return <span role="note" className="shrink-0 rounded-full border border-border px-2 text-[12px] leading-5 text-muted" data-testid="raised-by">{i18nT('sharedCrew.raised_by', { name })}</span>
}

export function TypingLockBanner({ lock }: { lock: ComposerTypingLockView }) {
  const keeps = i18nT('sharedCrew.take_over_keeps_draft', { name: lock.holderName })
  const label = i18nT(lock.tookOver ? 'sharedCrew.composer_taken_over' : 'sharedCrew.composer_locked', { name: lock.holderName })
  return (
    <div className="flex flex-col min-w-0 flex-1">
      <div className="flex items-center gap-2 min-w-0 px-1.5 py-1">
        <div
          role="status"
          aria-live="polite"
          className="flex items-center min-w-0 flex-1"
          data-testid="composer-typing-lock"
          data-taken-over={lock.tookOver ? '' : undefined}
        >
          <span className="min-w-0 flex flex-col leading-tight">
            <span className="text-[12px] text-text truncate">{label}</span>
            {lock.onTakeOver && !lock.tookOver && <span className="text-[12px] text-muted truncate">{keeps}</span>}
          </span>
        </div>
        {lock.onTakeOver && (
          <Btn
            className="h-6 px-2 text-[12px] shrink-0"
            disabled={lock.takeOverPending}
            onClick={lock.onTakeOver}
            aria-label={keeps}
            data-testid="composer-take-over"
          >
            {i18nT('sharedCrew.take_over_typing')}
          </Btn>
        )}
      </div>
      {lock.onTakeOver && <ErrorNotice message={lock.takeOverError} variant="inline" askAgent testId="composer-take-over-error" />}
    </div>
  )
}
