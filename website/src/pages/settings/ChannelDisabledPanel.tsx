import { Lock, Loader2, AlertTriangle } from 'lucide-react'

import { i18nT } from '../../i18n/t'
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
          {i18nT('pages.settings.channelDisabledPanel.checking_your_organization_s_channel_policy')}
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
          {label} {i18nT('pages.settings.channelDisabledPanel.policy_status_unavailable')}
        </div>
        <p className="text-sm text-muted leading-relaxed">
          {i18nT('pages.settings.channelDisabledPanel.kirocrew_couldn_t_confirm_whether_your_organizat')}
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
        {label} {i18nT('pages.settings.channelDisabledPanel.is_turned_off_by_your_administrator')}
      </div>
      <p className="text-sm text-muted leading-relaxed">
        {i18nT('pages.settings.channelDisabledPanel.your_organization_s_security_policy_disables_thi')}
      </p>
    </div>
  )
}
