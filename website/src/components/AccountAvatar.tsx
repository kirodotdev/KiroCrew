import { UserRound } from 'lucide-react'
import { accountIdentityName, type KiroAccountUsage } from './KiroAccountModal'
import { emailInitials } from '../lib/emailInitials'

/** Desktop rail diameter: a step above the rail's 20px icons, so the row reads as "you". */
export const ACCOUNT_AVATAR_RAIL_PX = 26
/** Phone drawer diameter, sized to the drawer's larger touch tiles. */
export const ACCOUNT_AVATAR_TOUCH_PX = 30

/**
 * The Account & Settings row's icon: the signed-in account's initials in a
 * circle, or an empty person circle when no account is read. The nav row pins
 * its icon slot to 20px, so the circle is centred on that slot and drawn past
 * it rather than resized by it.
 */
export function AccountAvatar({ usage, size }: { usage: KiroAccountUsage; size: number }) {
  const name = accountIdentityName(usage)
  return (
    <span aria-hidden="true" data-testid="account-avatar" className="relative flex items-center justify-center">
      <span
        style={{ width: size, height: size, fontSize: Math.round(size * 0.4) }}
        className="absolute flex items-center justify-center rounded-full border border-accent/30 bg-accent/10 font-semibold tracking-[0.02em] text-accent"
      >
        {name
          ? <span data-testid="account-avatar-initials">{emailInitials(name)}</span>
          : <UserRound data-testid="account-avatar-empty" style={{ width: size * 0.6, height: size * 0.6 }} strokeWidth={1.8} />}
      </span>
    </span>
  )
}
