import { fmtNumber } from '../i18n/format'

/** The speaking rates Settings → Voice → Speed offers. Values are the percentage
 *  strings the gateway stores as `voice_reply.rate` and every TTS provider
 *  accepts. */
export const VOICE_RATE_OPTIONS = ['80%', '90%', '95%', '100%', '110%', '120%', '130%', '150%']

/** The rates the Read aloud playback bar offers, a subset of
 *  `VOICE_RATE_OPTIONS` so Settings always shows the rate chosen on the bar. */
export const VOICE_SPEED_PRESETS = ['80%', '100%', '120%', '150%'] as const

const RATE_PATTERN = /^(\d{1,3})%$/

/** The rate as a plain multiplier (`'120%'` is 1.2); anything the gateway would
 *  reject as a rate reads as normal speed, as it does there. */
export function voiceSpeedMultiplier(rate: string | undefined): number {
  const match = rate ? RATE_PATTERN.exec(rate) : null
  return match ? Number(match[1]) / 100 : 1
}

/** The rate as shown on the control, in the active locale: `'120%'` reads `120%`. */
export function formatVoiceSpeed(rate: string | undefined): string {
  return fmtNumber(voiceSpeedMultiplier(rate), { style: 'percent' })
}
