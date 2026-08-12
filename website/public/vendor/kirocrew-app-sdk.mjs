// Vendor stub: re-exports @kirocrew/app-sdk from the host.
const m = window.__kirocrew_modules?.['@kirocrew/app-sdk']
if (!m) throw new Error('[vendor/kirocrew-app-sdk] Host modules not initialized.')
export const {
  useAppApi, useAppEvents, useTheme, useAppInfo, useNavigate, useNotify,
  useNavBadge, useChatLauncher, AppApiProvider,
  // Marker protocol. Naming an export the host does not provide is a load-time failure for the
  // whole app, so this list is checked against the protocol barrel by chatProtocolBoundary.test.ts.
  parseOptions, deriveFollowUpOptions, extractSteeringAcks, stripPartialOptionMarker,
  // Chat surfaces and the transcript row registry.
  useChatSession, ChatPanel, ChatEmbed, ChatMessageList,
  defaultMessageRenderers, mergeRenderers, resolveRenderer, ToolCallPill,
} = m
