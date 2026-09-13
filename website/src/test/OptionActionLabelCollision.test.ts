/**
 * A label appearing in BOTH markers must render once, as the action.
 *
 * `parseOptions` scans the two markers independently, so a model emitting
 * `[OPTIONS: That's all | Keep going]` beside `[OPTION-ACTIONS: close=That's all]` produced two
 * chips with identical text -- and the content one SENDS A MODEL TURN, the exact cost the
 * action exists to avoid. The action wins.
 */
import { describe, it, expect } from 'vitest'

import { optionsExcludingAction } from '../app-sdk/protocol/options'
import type { OptionAction } from '../app-sdk/protocol/options'

const close = (label: string): OptionAction => ({ action: 'close', label })

describe('the action owns its label', () => {
  it('drops the colliding content option', () => {
    expect(optionsExcludingAction(["That's all", 'Keep going'], close("That's all")))
      .toEqual(['Keep going'])
  })

  it('leaves every non-colliding option alone', () => {
    // The control: without it, a filter that dropped everything would satisfy the test above.
    expect(optionsExcludingAction(['Alpha', 'Bravo'], close('Shut it')))
      .toEqual(['Alpha', 'Bravo'])
  })

  it('is unchanged when there is no action at all', () => {
    expect(optionsExcludingAction(['Alpha', 'Bravo'], null)).toEqual(['Alpha', 'Bravo'])
  })

  it('folds case and padding, because two such chips read as the same choice', () => {
    expect(optionsExcludingAction(['  thats ALL  ', 'Keep going'], close('Thats all')))
      .toEqual(['Keep going'])
  })

  it('drops every duplicate of the owned label, not just the first', () => {
    expect(optionsExcludingAction(['Close', 'close', 'Keep going'], close('Close')))
      .toEqual(['Keep going'])
  })

  it('returns a copy, so a caller cannot mutate the derived list', () => {
    const options = ['Alpha']
    expect(optionsExcludingAction(options, null)).not.toBe(options)
  })
})
