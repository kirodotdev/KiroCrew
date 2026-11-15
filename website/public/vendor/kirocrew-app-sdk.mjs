// Vendor stub: re-exports @kirocrew/app-sdk from the host.
const m = window.__kirocrew_modules?.['@kirocrew/app-sdk']
if (!m) throw new Error('[vendor/kirocrew-app-sdk] Host modules not initialized.')
export const {
  useAppApi, useAppEvents, useTheme, useAppInfo, useNavigate, useNotify,
  useNavBadge, useAppNotifications, useChatLauncher, AppApiProvider,
  useImeGuard, useLanguageGeneration, activeLocale,
  fmtNumber, fmtCurrency, fmtDate, fmtTime, fmtDateTime, fmtRelative, compareText,
  // The App Kit seam level, so an app can feature-detect what this host provides.
  APP_SDK_LEVEL,
  // An app's own origin on the dashboard's port (level 8), and why it has one or not (level 11).
  appWebOrigin, appWebOriginStatus,
  // Marker protocol. Naming an export the host does not provide is a load-time failure for the
  // whole app, so this list is checked against the protocol barrel by chatProtocolBoundary.test.ts.
  parseOptions, deriveFollowUpOptions, extractSteeringAcks, stripPartialOptionMarker,
  // Chat surfaces and the transcript row registry.
  useChatSession, ChatPanel, ChatEmbed, ChatMessageList,
  // The dashboard's pull-request panel, behind the app's declared source paths (level 2).
  PullRequestPanel,
  // The dashboard's diff viewer, and a session's changed files with their diffs behind the
  // app's declared session-file paths (level 12).
  DiffView, SessionChanges,
  // Issue lists behind the app's declared `/api/source/issues` (level 6).
  useIssues, IssueList, IssueCard,
  // The dashboard's project picker behind the app's declared listing paths (level 7).
  ProjectPicker,
  // The dashboard's live session list and session acts behind the app's declared chat paths (level 8).
  useHostSessions,
  // Media buttons on the tab the app's own agent browses (level 10).
  useBrowserMedia, BROWSER_MEDIA_PATH,
  // The live view of the app's own slot, take over and hand back, coordinates (level 11).
  BrowserView, useBrowserView, useBrowserCoords,
  defaultMessageRenderers, mergeRenderers, resolveRenderer, ToolCallPill, GROUPED_ROLES,
  // WS event scope prediction, so a subscription that will never be delivered warns at
  // development time instead of silently receiving nothing.
  checkSubscribeAllowed,
  // Ask the owner to change which memory Crew uses; the host sheet does the switch (level 10).
  requestMemoryProviderChange,
  // Ask the owner to move memory between providers; the host sheet runs it (level 10).
  requestMemoryMove,
  // Ask the owner to benchmark memory providers; the host sheet starts the run (level 10).
  requestMemoryBench,
} = m
