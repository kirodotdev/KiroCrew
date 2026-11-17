/**
 * The timed choices Settings offers for silencing something for a while: the
 * Sound pane's "Mute sounds" and the Desktop alerts pane's "Do not disturb".
 * While one runs, each select also shows `MUTED_NOW`.
 */
import { i18nT } from '../../i18n/t'
import { fmtTime } from '../../i18n/format'

export const MUTE_CHOICES = ['off', '1h', '2h', 'morning'] as const
export type MuteChoice = typeof MUTE_CHOICES[number]
export const MUTED_NOW = 'muted'
const MORNING_HOUR = 8

/** When a mute chosen at `now` ends, in ms: one or two hours later, or the next 8:00. */
export function muteEnd(choice: Exclude<MuteChoice, 'off'>, now: number): number {
  if (choice !== 'morning') return now + (choice === '1h' ? 1 : 2) * 3_600_000
  const at = new Date(now)
  at.setHours(MORNING_HOUR, 0, 0, 0)
  if (at.getTime() <= now) at.setDate(at.getDate() + 1)
  return at.getTime()
}

export const muteLabel = (choice: MuteChoice, now: number): string => {
  switch (choice) {
    case 'off': return i18nT('pages.settings.notificationsPanel.mute_off')
    case '1h': return i18nT('pages.settings.notificationsPanel.mute_for_1h')
    case '2h': return i18nT('pages.settings.notificationsPanel.mute_for_2h')
    case 'morning': return i18nT('pages.settings.notificationsPanel.mute_until', { time: fmtTime(muteEnd('morning', now)) })
  }
}
