import type { ComputerUseSetupDoc, ComputerUseSetupState } from '../api/client'

type Overrides = Partial<Omit<ComputerUseSetupDoc, 'driver' | 'session' | 'permissions' | 'capabilities'>> & {
  driver?: Partial<ComputerUseSetupDoc['driver']>
  session?: Partial<ComputerUseSetupDoc['session']>
  permissions?: Partial<ComputerUseSetupDoc['permissions']>
  capabilities?: Partial<ComputerUseSetupDoc['capabilities']>
}

/** A complete setup document for tests: Windows, upstream driver installed and
 *  healthy, in an interactive desktop session, unless the overrides say
 *  otherwise. (The upstream driver is not offered on macOS.) */
export function setupDoc(state: ComputerUseSetupState, overrides: Overrides = {}): ComputerUseSetupDoc {
  const { driver, session, permissions, capabilities, ...rest } = overrides
  return {
    state,
    platform: 'windows',
    driver: { kind: 'upstream', version: '0.47.8', installed: true, healthy: true, ...driver },
    session: { kind: 'windows_interactive', detail: '', ...session },
    permissions: {
      accessibility: 'not_applicable',
      screen_recording: 'not_applicable',
      owner_name: '',
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
