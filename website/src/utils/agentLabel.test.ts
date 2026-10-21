/**
 * Built-in agents get a plain display name on every surface that labels a
 * chat's agent (#21531): a chat on the `kirocrew-conductor` TEMPLATE reads
 * "Conductor", while a crewmate that shares the name keeps it and a name
 * without a display name stays raw.
 */
import { afterEach, describe, expect, it } from 'vitest'
import i18next from 'i18next'
import { agentOrDefaultLabel, builtinAgentDisplayName } from './agentLabel'
import '../i18n/all'

describe('agentOrDefaultLabel — built-in display names (#21531)', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('shows "Conductor" for the conductor picked in the template namespace', () => {
    expect(agentOrDefaultLabel('kirocrew-conductor', 'kirocrew', 'template')).toBe('Conductor')
  })

  it('keeps the name of a crewmate that shares the conductor name', () => {
    expect(agentOrDefaultLabel('kirocrew-conductor', 'kirocrew', 'member')).toBe('kirocrew-conductor')
  })

  it('does not guess from the name when the session recorded no kind', () => {
    expect(agentOrDefaultLabel('kirocrew-conductor', 'kirocrew')).toBe('kirocrew-conductor')
    expect(agentOrDefaultLabel('kirocrew-conductor', 'kirocrew', '')).toBe('kirocrew-conductor')
  })

  it('leaves an agent without a display name raw', () => {
    expect(agentOrDefaultLabel('my-reviewer', 'kirocrew', 'template')).toBe('my-reviewer')
    expect(builtinAgentDisplayName('my-reviewer')).toBe('my-reviewer')
  })

  it('names the conductor when it is the inherited default template', () => {
    expect(agentOrDefaultLabel(undefined, 'kirocrew-conductor')).toBe('Conductor · default')
    expect(agentOrDefaultLabel(undefined, 'kirocrew')).toBe('kirocrew · default')
  })

  it('translates the display name through the locale catalogs', async () => {
    await i18next.changeLanguage('fr')
    expect(agentOrDefaultLabel('kirocrew-conductor', 'kirocrew', 'template')).toBe('Conducteur')
  })
})
