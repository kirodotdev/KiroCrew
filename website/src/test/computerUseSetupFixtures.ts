import type { ComputerUseSetupDoc, ComputerUseSetupState } from '../api/client'

type Overrides = Partial<Omit<ComputerUseSetupDoc, 'driver' | 'session' | 'permissions' | 'capabilities'>> & {
  driver?: Partial<ComputerUseSetupDoc['driver']>
  session?: Partial<ComputerUseSetupDoc['session']>
  permissions?: Partial<ComputerUseSetupDoc['permissions']>
  capabilities?: Partial<ComputerUseSetupDoc['capabilities']>
}

/** A complete setup document for tests: macOS, upstream driver installed and
 *  healthy, both grants given, unless the overrides say otherwise. */
export function setupDoc(state: ComputerUseSetupState, overrides: Overrides = {}): ComputerUseSetupDoc {
  const { driver, session, permissions, capabilities, ...rest } = overrides
  return {
    state,
    platform: 'macos',
    driver: { kind: 'upstream', version: '0.47.8', installed: true, healthy: true, ...driver },
    session: { kind: 'aqua', detail: '', ...session },
    permissions: {
      accessibility: 'granted',
      screen_recording: 'granted',
      owner_name: 'CuaDriver',
      previously_granted: false,
      ...permissions,
    },
    capabilities: {
      observe_tree: true,
      screenshot: true,
      background_input: true,
      foreground_input: true,
      window_capture: true,
      ...capabilities,
    },
    limitations: [],
    steps: [],
    user_action: '',
    progress: { last_step: '' },
    ...rest,
  }
}
