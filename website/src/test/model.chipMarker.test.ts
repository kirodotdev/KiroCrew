import { describe, it, expect, vi, afterEach } from 'vitest'
import { createElement, type ReactNode } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import { useSettingsDefaultModel } from '../hooks/useSettingsDefaultModel'
import { displayModel, modelChipMarker, reportedModel, reportedModelMarker } from '../lib/model'

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

/** Both chat hosts read the chip's default through this hook, so a failed read
 *  must reach them as `failed` rather than as a silently missing marker. */
function run(agentName: string, stripEffort = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
  return renderHook(() => useSettingsDefaultModel(agentName, false, stripEffort), { wrapper })
}

afterEach(() => vi.restoreAllMocks())

describe('useSettingsDefaultModel', () => {
  it('reports a failed config read instead of claiming no default', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('503'))
    const { result } = run('')
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.settingsDefault).toBeNull()
  })

  it('reports a failed agent pin read and claims no default until it lands', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { model: 'claude-sonnet-5' } } as never)
    vi.spyOn(api, 'agentResolvedModel').mockRejectedValue(new Error('500'))
    const { result } = run('kirocrew')
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.settingsDefault).toBeNull()
  })

  it('returns the default and the agent pin once both reads land', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { model: 'gpt-5-codex[high]' } } as never)
    vi.spyOn(api, 'agentResolvedModel').mockResolvedValue({ pinned: true } as never)
    const { result } = run('kirocrew', true)
    await waitFor(() => expect(result.current.settingsDefault).toBe('gpt-5-codex'))
    expect(result.current).toEqual({ settingsDefault: 'gpt-5-codex', agentPinned: true, failed: false })
  })
})

/** A chip naming what the live session REPORTS: the model the harness said ran,
 *  the selection in the picker's spelling, and no `auto` marker for a pin it
 *  merely respells. */
describe('reportedModel / reportedModelMarker', () => {
  const list = [{ name: 'auto' }, { name: 'claude-opus-4-8' }]

  it('names what the harness said ran over what the session selected', () => {
    expect(reportedModel('global.anthropic.claude-opus-5[1m]', 'global.anthropic.claude-fable-5[1m]', list)).toEqual({
      selected: 'global.anthropic.claude-fable-5[1m]',
      chip: 'global.anthropic.claude-opus-5[1m]',
    })
    expect(reportedModel('', 'global.anthropic.claude-fable-5[1m]', list).chip).toBe('global.anthropic.claude-fable-5[1m]')
  })

  it("keeps the selection in the picker row's spelling, and the harness report as reported", () => {
    // The picker highlights the selection on an exact name; the chip names the
    // concrete id the harness reported even when a registry fold equates them.
    expect(reportedModel('claude-opus-4-8', 'claude-opus-4.8', [{ name: 'claude-opus-4.8' }])).toEqual({
      selected: 'claude-opus-4.8',
      chip: 'claude-opus-4-8',
    })
  })

  it('reports nothing for Auto, whose per-turn choice is not on the wire', () => {
    expect(reportedModel('', 'auto', list)).toEqual({ selected: '', chip: '' })
    expect(reportedModel('', '', list)).toEqual({ selected: '', chip: '' })
  })

  it('keeps `auto` only on the backend withhold verdict', () => {
    // A pin and the wire id the harness reports for it compare as different
    // spellings, which modelChipMarker reads as "chosen for you".
    const respelt = modelChipMarker('claude-opus-5.5', 'global.anthropic.claude-opus-5-5[1m]', 'claude-opus-5.5', '')
    expect(respelt).toBe('auto')
    expect(reportedModelMarker(respelt, false)).toBeNull()
    expect(reportedModelMarker(respelt, true)).toBe('auto')
    expect(reportedModelMarker('default', null)).toBe('default')
  })
})
