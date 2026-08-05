// Vendor stub: re-exports @kirocrew/app-sdk from the host.
const m = window.__kirocrew_modules?.['@kirocrew/app-sdk']
if (!m) throw new Error('[vendor/kirocrew-app-sdk] Host modules not initialized.')
export const {
  useAppApi, useAppEvents, useTheme, useAppInfo, useNavigate, useNotify,
  useNavBadge, useChatLauncher, AppApiProvider,
} = m
