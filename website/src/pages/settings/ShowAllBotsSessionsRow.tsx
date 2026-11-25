/**
 * Settings > Chat > Sessions: the **Show all bots in my Sessions** row --
 * `dashboard.show_bot_sessions_in_list`, the same switch that sits at the top of
 * every bot's Sessions list (`BotSessionListSwitches`), read and written through
 * the same hook so every copy shows one value.
 */
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'
import { AllBotsConfigLoadNotice, useAllBotsSessionsSwitch } from '../members/BotSessionListSwitches'

export function ShowAllBotsSessionsRow() {
  const all = useAllBotsSessionsSwitch()
  return (
    <div>
      <SettingsToggle
        label={i18nT('pages.membersPage.bot_sessions_all_switch')}
        configKey="dashboard.show_bot_sessions_in_list"
        checked={all.on}
        onChange={all.set}
        disabled={!all.cfg.data || all.pending}
      />
      <AllBotsConfigLoadNotice cfg={all.cfg} />
      {/* No hand-off: this row sits in ChatPanel, whose `localRoleOther` /
          `localBudget` / `localKeepChars` drafts would be unmounted by the navigation. */}
      <ErrorNotice message={all.saveFailed ? i18nT('pages.settings.crewmatesSection.failed_to_save') : null} testId="show-all-bots-save-error" />
    </div>
  )
}
