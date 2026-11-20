/**
 * The confirm a chat that already has a user message asks before conductor mode
 * turns on: from then on the gateway keeps the chat on the conductor
 * (`crew_mode_locked`), so the switch cannot be undone there.
 *
 * The dialog says what conductor mode is with the welcome page's own slogan and
 * flow (`CrewModeIntro`), then one short line in the warning color that it
 * cannot be turned off for this chat. Its
 * confirm button is the ordinary primary action: turning the mode on is the
 * step the person asked for, not a risk.
 *
 * "Don't show this notice again" is saved only on Confirm, through
 * `savePrefExplicitly`, and backed up with the other durable preferences
 * (`uiPrefsKeys.ts`). Once saved, the switch goes ahead with no dialog.
 */
import { useState } from 'react'
import { CrewModeIntro } from '../../../components/CrewModeWelcome'
import { askAppConfirm } from '../../../lib/appConfirm'
import { savePrefExplicitly } from '../../../lib/uiPrefs'
import { i18nT } from '../../../i18n/t'
import { safeGetItem } from '../../../utils/safeStorage'

export const CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY = 'kirocrew:conductor-mode-midchat-confirm-dismissed'

function Body({ choice }: { choice: { dontShowAgain: boolean } }) {
  const [checked, setChecked] = useState(false)
  return (
    <div className="flex flex-col gap-3">
      <CrewModeIntro />
      <p data-testid="crew-mode-midchat-lock-line" className="m-0 text-sm text-warn-text">
        {i18nT('components.crewMode.midchat_confirm_body')}
      </p>
      <label className="flex items-center gap-2 text-sm text-muted cursor-pointer">
        {/* Named from the same key as the visible text beside it, as the
            approval-mode confirm's checkbox is. */}
        <input
          type="checkbox"
          className="rounded"
          aria-label={i18nT('components.crewMode.midchat_confirm_dont_show')}
          checked={checked}
          onChange={e => { setChecked(e.target.checked); choice.dontShowAgain = e.target.checked }}
        />
        {i18nT('components.crewMode.midchat_confirm_dont_show')}
      </label>
    </div>
  )
}

/** Resolves `true` when conductor mode may turn on: the person confirmed, or
 *  saved "Don't show this notice again" on an earlier confirm. */
export async function confirmCrewModeMidChat(): Promise<boolean> {
  if (safeGetItem(CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY) === '1') return true
  const choice = { dontShowAgain: false }
  const ok = await askAppConfirm({
    title: i18nT('components.crewMode.midchat_confirm_title'),
    body: <Body choice={choice} />,
    confirmLabel: i18nT('components.crewMode.midchat_confirm_action'),
    primary: true,
  })
  if (ok && choice.dontShowAgain) savePrefExplicitly(CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY, '1')
  return ok
}
