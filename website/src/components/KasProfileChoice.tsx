import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { KasLoginProfile } from '../api/client'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'

/**
 * The sign-in step a company sign-in shows when it can use more than one Kiro
 * profile: one radio row per profile (name and region), `selected` preselected,
 * Continue to finish with the pick, and the waiting step's way back out.
 */
export default function KasProfileChoice({
  profiles,
  selected,
  busy,
  notice,
  askAgent,
  onContinue,
  onCancel,
}: {
  profiles: KasLoginProfile[]
  selected: string
  /** True while the pick (or a cancel) is in flight: nothing can be pressed twice. */
  busy: boolean
  /** A cancel that did not settle, as on the waiting step. */
  notice: string
  /** Offer the agent hand-off on that notice: in card chrome only, as on the waiting step. */
  askAgent: boolean
  onContinue: (arn: string) => void
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const [arn, setArn] = useState(selected)
  const group = useId()
  return (
    <div className="w-full max-w-md" data-testid="kas-profile-choice">
      <p id={`${group}-title`} className="text-[15px] font-semibold text-text-strong">
        {t('components.kasLogin.choose_profile_title')}
      </p>
      <p className="mt-1 text-[13px] text-muted">{t('components.kasLogin.choose_profile_body')}</p>
      <div role="radiogroup" aria-labelledby={`${group}-title`} className="mt-4 space-y-2">
        {profiles.map(profile => (
          <label
            key={profile.arn}
            className={`flex cursor-pointer items-center gap-3 rounded-lg border p-3 ${arn === profile.arn ? 'border-accent bg-accent-subtle' : 'border-border bg-bg'}`}
          >
            <input
              type="radio"
              name={group}
              value={profile.arn}
              aria-label={profile.name || profile.arn}
              checked={arn === profile.arn}
              disabled={busy}
              onChange={() => setArn(profile.arn)}
              className="accent-[var(--accent)]"
            />
            <span className="min-w-0 flex-1" translate="no">
              <span className="block truncate text-[13px] font-medium text-text">{profile.name || profile.arn}</span>
              {profile.region ? <span className="block text-[12px] text-muted">{profile.region}</span> : null}
            </span>
          </label>
        ))}
      </div>
      <ErrorNotice className="mt-4" askAgent={askAgent} message={notice || null} testId="kas-login-settle-error" />
      <div className="mt-4 flex items-center gap-2">
        <Btn type="button" primary disabled={busy} onClick={() => onContinue(arn)}>
          {t('components.kasLogin.choose_profile_continue')}
        </Btn>
        <Btn type="button" disabled={busy} onClick={onCancel}>
          {t('components.kasLogin.use_different_sign_in')}
        </Btn>
      </div>
    </div>
  )
}
