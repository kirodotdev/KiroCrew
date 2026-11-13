import { describe, expect, it } from 'vitest'

import { VOICE_RATE_OPTIONS, VOICE_SPEED_PRESETS, formatVoiceSpeed, voiceSpeedMultiplier } from '../lib/voiceSpeed'

describe('voiceSpeedMultiplier', () => {
  it.each([
    ['80%', 0.8],
    ['100%', 1],
    ['125%', 1.25],
    ['200%', 2],
  ])('reads %s as %s', (rate, multiplier) => {
    expect(voiceSpeedMultiplier(rate)).toBe(multiplier)
  })

  it.each([undefined, '', 'fast', '1.5', '1000%', '-5%'])('reads %j as normal speed', rate => {
    expect(voiceSpeedMultiplier(rate)).toBe(1)
  })
})

describe('formatVoiceSpeed', () => {
  it.each([
    ['80%', '80%'],
    ['100%', '100%'],
    ['150%', '150%'],
    ['90%', '90%'],
    [undefined, '100%'],
  ])('shows %s as %s', (rate, label) => {
    expect(formatVoiceSpeed(rate)).toBe(label)
  })
})

describe('VOICE_SPEED_PRESETS', () => {
  it('only offers rates that Settings can show', () => {
    for (const preset of VOICE_SPEED_PRESETS) expect(VOICE_RATE_OPTIONS).toContain(preset)
  })

  it('lists the presets from slowest to fastest', () => {
    const multipliers = VOICE_SPEED_PRESETS.map(voiceSpeedMultiplier)
    expect(multipliers).toEqual([...multipliers].sort((a, b) => a - b))
  })
})
