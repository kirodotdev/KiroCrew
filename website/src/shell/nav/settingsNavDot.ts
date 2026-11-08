import { i18nT } from '../../i18n/t'

/** What the Settings nav dot says, or `null` for no dot. One dot, two causes: an
 *  update (Settings > About) or a settings file edited outside the dashboard
 *  waiting for review (Settings > Security). The review names the dot when both
 *  hold: it is the one the owner must act on before it takes effect. */
export function settingsNavDotLabel(
  updateAvailable: boolean,
  configPending: boolean,
): { title: string; label: string } | null {
  if (configPending) {
    const text = i18nT('pages.settings.securityPanel.configPending.nav_dot')
    return { title: text, label: text }
  }
  if (updateAvailable) return { title: i18nT('app.update_available'), label: i18nT('app.update_available_2') }
  return null
}
