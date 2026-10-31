/** The calm row a locked composer shows in place of its buttons while another
 *  collaborator is typing in the same shared session: their initials and
 *  "<name> is typing…", or "<name> took over typing" when they took the lease
 *  from this user. No motion and no colour alarm; the read-only draft above it
 *  is this user's own and stays as it was. With the Shared Crew preview on it
 *  also offers Take over, and a failed takeover shows under it. */
import { Btn } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import type { ComposerTypingLockView } from './props'

export function typingLockLabel(lock: ComposerTypingLockView): string {
  return i18nT(lock.tookOver ? 'sharedCrew.composer_taken_over' : 'sharedCrew.composer_locked', { name: lock.holderName })
}

export function TypingLockBanner({ lock }: { lock: ComposerTypingLockView }) {
  const label = typingLockLabel(lock)
  const hint = i18nT(lock.tookOver ? 'sharedCrew.composer_taken_over_hint' : 'sharedCrew.composer_locked_hint')
  const takeOverLabel = i18nT('sharedCrew.take_over_typing_label', { name: lock.holderName })
  return (
    <div className="flex flex-col min-w-0 flex-1">
      <div className="flex items-center gap-2 min-w-0 px-1.5 py-1">
        <div
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 min-w-0 flex-1"
          data-testid="composer-typing-lock"
          data-taken-over={lock.tookOver ? '' : undefined}
        >
          <span
            aria-hidden
            className="inline-flex items-center justify-center w-6 h-6 shrink-0 rounded-full text-[10px] font-semibold bg-bg-hover text-text border border-border"
          >
            {lock.holderInitials}
          </span>
          <span className="min-w-0 flex flex-col leading-tight">
            <span className="text-[12px] text-text truncate" title={label}>{label}</span>
            <span className="text-[11px] text-muted truncate">{hint}</span>
          </span>
        </div>
        {lock.onTakeOver && (
          <Btn
            className="h-6 px-2 text-[12px] shrink-0"
            disabled={lock.takeOverPending}
            onClick={lock.onTakeOver}
            aria-label={takeOverLabel}
            title={takeOverLabel}
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
