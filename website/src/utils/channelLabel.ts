import { i18nT } from '../i18n/t'

/** Channel ids are the closed lowercase vocabulary `telemetry_channel_of`
 *  returns (`dashboard`, `cron`, `background`, `telegram` …), shown capitalised
 *  so the legend reads as places spend went rather than as code. `other` is the
 *  classifier's "key shape not recognised" and gets its own label, since shown
 *  as-is it would be mistaken for the fold layer's "Other (n more)". */
export function channelLabel(id: string): string {
  if (id === 'other') return i18nT('pages.overview.usageSeriesChart.channel_unrecognized')
  const words = id.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}
