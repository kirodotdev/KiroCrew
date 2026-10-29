/** The calm row a locked composer shows in place of its buttons while another
 *  collaborator is typing in the same shared session: their initials and
 *  "<name> is typing…". No motion and no colour alarm; the read-only draft above
 *  it is this user's own and stays as it was. */
import { i18nT } from '../../i18n/t'
import type { ComposerTypingLockView } from './props'

export function TypingLockBanner({ lock }: { lock: ComposerTypingLockView }) {
  const label = i18nT('sharedCrew.composer_locked', { name: lock.holderName })
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-2 min-w-0 flex-1 px-1.5 py-1"
      data-testid="composer-typing-lock"
    >
      <span
        aria-hidden
        className="inline-flex items-center justify-center w-6 h-6 shrink-0 rounded-full text-[10px] font-semibold bg-bg-hover text-text border border-border"
      >
        {lock.holderInitials}
      </span>
      <span className="min-w-0 flex flex-col leading-tight">
        <span className="text-[12px] text-text truncate" title={label}>{label}</span>
        <span className="text-[11px] text-muted truncate">{i18nT('sharedCrew.composer_locked_hint')}</span>
      </span>
    </div>
  )
}
