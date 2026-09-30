import { describe, it, expect } from 'vitest'
import { displayModel, modelChipMarker } from '../lib/model'

/** The composer chip's ` · default` marker must mean the Settings default, and
 *  nothing else. A model the backend or a router picked on its own (an Auto
 *  pick, a withheld pin's fallback) is named with an `auto` marker instead, so a
 *  reader can tell "the model I configured" from "a model chosen for me". */
describe('modelChipMarker', () => {
  const list = [
    { name: 'auto' },
    { name: 'claude-sonnet-5' },
    { name: 'claude-opus-4.8' },
  ]
  // Mirrors the hosts: the chip's name, and what the pin alone would say.
  const chip = (
    slotModel: string,
    served: string,
    withheld: boolean | null,
    settingsDefault: string | null,
    pin = slotModel,
    agentPinned = false,
  ) =>
    modelChipMarker(
      slotModel,
      displayModel(pin, list, false, withheld, served),
      displayModel(pin, list, false, withheld),
      settingsDefault,
      agentPinned,
    )

  it('labels a model the router chose for an Auto slot as auto, not default', () => {
    expect(chip('auto', 'claude-sonnet-5', null, 'claude-opus-4.8')).toBe('auto')
  })

  it('labels a withheld pin\'s fallback as auto when it is not the Settings default', () => {
    expect(chip('claude-opus-5', 'claude-sonnet-5', true, 'claude-opus-5')).toBe('auto')
  })

  it('says default when the served model is the Settings default', () => {
    expect(chip('auto', 'claude-opus-4.8', null, 'claude-opus-4.8')).toBe('default')
  })

  it('says default for a fresh slot resolved to the Settings default', () => {
    // A new chat stores no model; the host displays the resolved chain's model.
    expect(chip('', '', null, 'claude-opus-4.8', 'claude-opus-4.8')).toBe('default')
  })

  it('puts no marker on a fresh slot whose agent pins the same model as the default', () => {
    expect(chip('', '', null, 'claude-opus-4.8', 'claude-opus-4.8', true)).toBeNull()
  })

  it('puts no marker on a fresh slot whose agent names its own model', () => {
    expect(chip('', '', null, 'claude-opus-4.8', 'claude-sonnet-5')).toBeNull()
  })

  it('puts no marker on an unpinned slot served a model other than the default', () => {
    // The pane has no resolved pin for such a slot, so it cannot say who chose.
    expect(chip('', 'claude-sonnet-5', null, 'claude-opus-4.8')).toBeNull()
  })

  it('puts no marker on a pin, even one equal to the Settings default', () => {
    expect(chip('claude-opus-4.8', 'claude-opus-4.8', false, 'claude-opus-4.8')).toBeNull()
  })

  it('puts no marker when the chip already reads auto', () => {
    expect(chip('auto', '', null, 'claude-opus-4.8')).toBeNull()
  })

  it('claims nothing while the Settings default is unknown', () => {
    expect(chip('auto', 'claude-sonnet-5', null, null)).toBeNull()
    expect(chip('auto', 'claude-opus-4.8', null, null)).toBeNull()
  })

  it('never says default when Settings names no default', () => {
    expect(chip('auto', 'claude-sonnet-5', null, '')).toBe('auto')
    expect(chip('', '', null, '', 'claude-sonnet-5')).toBeNull()
  })
})
