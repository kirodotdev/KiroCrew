import { Lock, Loader2, AlertTriangle } from 'lucide-react'

/**
 * Shown in place of a channel's editable config panel when the `channels`
 * governance policy is not a confirmed ALLOW. The real panel (with the bot-token
 * form) must NOT render unless we KNOW the channel is permitted — otherwise a
 * user could type/save config that will never take effect (the backend gates the
 * transport start + every inbound/outbound path via the `channels` chokepoints).
 *
 * Three states, so the form is never shown on an unconfirmed policy:
 * - `denied`      — policy explicitly disables the channel ("Off by admin").
 * - `pending`     — the policy is still loading; don't flash the editable form.
 * - `unavailable` — the policy fetch failed; enforcement is server-side and
 *                   unaffected, but we can't confirm ALLOW, so we don't render
 *                   an editable form that might not take effect.
 * Parametrized by channel label so one component serves Discord / Telegram /
 * Webex / WeCom.
 */
export function ChannelDisabledPanel({
  label,
  variant = 'denied',
}: {
  label: string
  variant?: 'denied' | 'pending' | 'unavailable'
}) {
  if (variant === 'pending') {
    return (
      <div className="py-10 flex flex-col items-center text-center max-w-md mx-auto">
        <Loader2 size={20} className="lucide-inline text-muted mb-4 animate-spin" />
        <div className="text-sm text-muted leading-relaxed">
          Checking your organization's channel policy…
        </div>
      </div>
    )
  }
  if (variant === 'unavailable') {
    return (
      <div className="py-10 flex flex-col items-center text-center max-w-md mx-auto">
        <div className="w-12 h-12 rounded-full bg-bg-hover border border-border flex items-center justify-center mb-4">
          <AlertTriangle size={20} className="lucide-inline text-warn" />
        </div>
        <div className="text-base font-semibold text-text-strong mb-1.5">
          {label} policy status unavailable
        </div>
        <p className="text-sm text-muted leading-relaxed">
          KiroCrew couldn't confirm whether your organization's security policy
          permits this channel. Its settings are hidden until the status resolves —
          enforcement is unaffected. Try reloading.
        </p>
      </div>
    )
  }
  return (
    <div className="py-10 flex flex-col items-center text-center max-w-md mx-auto">
      <div className="w-12 h-12 rounded-full bg-bg-hover border border-border flex items-center justify-center mb-4">
        <Lock size={20} className="lucide-inline text-muted" />
      </div>
      <div className="text-base font-semibold text-text-strong mb-1.5">
        {label} is turned off by your administrator
      </div>
      <p className="text-sm text-muted leading-relaxed">
        Your organization's security policy disables this channel. Its settings
        are unavailable and any configuration here would not take effect.
      </p>
    </div>
  )
}
