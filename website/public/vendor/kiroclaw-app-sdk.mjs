// Legacy vendor stub (KiroClaw -> KiroCrew rename): re-exports the app SDK for
// already-installed app bundles that import '@kiroclaw/app-sdk'. Reads the
// legacy global first, falling back to the current one — both point at the SAME
// host module instance (registered in shared-modules.ts), so no duplicate React.
const m =
  window.__kiroclaw_modules?.['@kiroclaw/app-sdk'] ||
  window.__kirocrew_modules?.['@kirocrew/app-sdk']
if (!m) throw new Error('[vendor/kiroclaw-app-sdk] Host modules not initialized.')
export const {
  useAppApi, useAppEvents, useTheme, useAppInfo, useNavigate, useNotify,
  useNavBadge, useChatLauncher, AppApiProvider,
} = m
