import { describe, it, expect } from 'vitest'
import { i18nT } from '../i18n/t'
import { gatewayAutoUpdateCopy, gatewayAutoUpdateEffect, updateSwitchesShown } from './updateSwitches'

const shell = (over: Partial<UpdateAPI> = {}): UpdateAPI => ({
  onState: () => () => {},
  check: async () => ({ ok: true }),
  install: async () => ({ ok: true }),
  getInfo: async () => ({}),
  setAutoDownload: async () => ({ ok: true }),
  ...over,
})

describe('gatewayAutoUpdateEffect', () => {
  it.each(['install', 'notify', 'mandatory', 'unknown'])('takes the gateway’s %s', effect => {
    expect(gatewayAutoUpdateEffect(effect)).toBe(effect)
  })

  it.each([undefined, null, '', 'later', 3])('reads %j as unknown', raw => {
    expect(gatewayAutoUpdateEffect(raw)).toBe('unknown')
  })
})

describe('updateSwitchesShown', () => {
  it('keeps the gateway row in a browser window whatever the effect', () => {
    for (const effect of ['install', 'notify', 'mandatory', 'unknown'] as const) {
      expect(updateSwitchesShown({ bridge: undefined, info: undefined, effect })).toEqual({ app: false, gateway: true })
    }
  })

  it('draws the gateway row in a desktop window only where it is known to install', () => {
    const gateway = (effect: 'install' | 'notify' | 'mandatory' | 'unknown') =>
      updateSwitchesShown({ bridge: shell(), info: {}, effect }).gateway
    expect(gateway('install')).toBe(true)
    expect(gateway('mandatory')).toBe(true)
    expect(gateway('notify')).toBe(false)
    expect(gateway('unknown')).toBe(false)
  })

  it('draws the gateway row in a desktop window for a reused (non-bundled) gateway', () => {
    // Topology 2 of GH #15797: a gateway the app reuses (CLI service, SSH tunnel)
    // manages its own bytes; its switch is shown even when the effect is not yet
    // a definite install, because the app's updater does not cover it.
    for (const effect of ['notify', 'unknown'] as const) {
      expect(
        updateSwitchesShown({ bridge: shell(), info: {}, effect, bundledByApp: false }).gateway,
      ).toBe(true)
    }
  })

  it('still hides the gateway row in a desktop window for the bundled gateway', () => {
    // bundledByApp: true => the app's own gateway, which ignores the switch.
    for (const effect of ['notify', 'unknown'] as const) {
      expect(
        updateSwitchesShown({ bridge: shell(), info: {}, effect, bundledByApp: true }).gateway,
      ).toBe(false)
    }
  })

  it('treats an unresolved bundled status conservatively (same as before)', () => {
    // bundledByApp: undefined => status not yet loaded, keep the old rule.
    expect(
      updateSwitchesShown({ bridge: shell(), info: {}, effect: 'notify', bundledByApp: undefined }).gateway,
    ).toBe(false)
  })

  it('offers the app switch on a shell that can read and write it, unless the updater is disabled', () => {
    const app = (bridge: UpdateAPI | undefined, info: { disabled?: string } | null | undefined) =>
      updateSwitchesShown({ bridge, info, effect: 'notify' }).app
    expect(app(shell(), undefined)).toBe(true)
    expect(app(shell(), {})).toBe(true)
    expect(app(shell(), { disabled: 'dev' })).toBe(false)
    expect(app(shell({ setAutoDownload: undefined }), {})).toBe(false)
    expect(app(shell({ getInfo: undefined }), {})).toBe(false)
  })

  it('hides the app switch when policy governs so settings search and About agree', () => {
    // A policy provider owns updates: the app's auto-download switch is not actionable
    // and must not appear in settings search (leading to an absent target on About).
    expect(
      updateSwitchesShown({ bridge: shell(), info: {}, effect: 'install', policyGoverned: true }).app,
    ).toBe(false)
    // Without governance the switch still shows normally.
    expect(
      updateSwitchesShown({ bridge: shell(), info: {}, effect: 'install', policyGoverned: false }).app,
    ).toBe(true)
  })

  it.each(['dev', 'translocated', 'externally-managed'])(
    'keeps a disabled (%s) updater out of the gateway switch’s rule',
    disabled => {
      for (const effect of ['install', 'mandatory'] as const) {
        expect(updateSwitchesShown({ bridge: shell(), info: { disabled }, effect })).toEqual({ app: false, gateway: true })
      }
    },
  )
})

describe('gatewayAutoUpdateCopy', () => {
  it('holds the switch where it cannot install, saying which kind of install this is', () => {
    expect(gatewayAutoUpdateCopy('notify', { canApply: false })).toEqual({
      disabled: true,
      hint: i18nT('pages.settings.aboutPanel.auto_update_notify_only_on_this_install'),
    })
    // A checkout off its primary branch: Update applies by hand.
    expect(gatewayAutoUpdateCopy('notify', { canApply: true })).toEqual({
      disabled: true,
      hint: i18nT('pages.settings.aboutPanel.auto_update_notify_manual_apply'),
    })
  })

  it('notes the policy override, and the missing verdict, beside a live switch', () => {
    expect(gatewayAutoUpdateCopy('mandatory', { canApply: true })).toEqual({
      disabled: false,
      hint: i18nT('pages.settings.aboutPanel.gateway_auto_update_mandatory_note'),
    })
    expect(gatewayAutoUpdateCopy('unknown', { canApply: false })).toEqual({
      disabled: false,
      hint: i18nT('pages.settings.aboutPanel.gateway_auto_update_unknown_hint'),
    })
  })

  it('adds nothing where the description already says what happens', () => {
    expect(gatewayAutoUpdateCopy('install', { canApply: true })).toEqual({ disabled: false })
  })
})
