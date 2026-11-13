import { useMainWindowSetting } from './useMainWindowSetting'

// Close-button bridge exposed by electron/preload.js.
const closeActionAPI = (): CloseActionAPI | undefined => window.closeActionAPI

const canonical = (v: unknown): CloseAction => (v === 'quit' ? 'quit' : 'background')

/** Read/write what the desktop app's main-window close button does. */
export function useCloseAction() {
  // The shipped default, so a first paint matches an untouched install.
  const s = useMainWindowSetting(closeActionAPI, canonical, 'background')
  return {
    closeAction: s.value,
    closeActionSupported: s.supported,
    closeActionPending: s.pending,
    closeActionFailed: s.failed,
    setCloseAction: s.set,
  }
}
