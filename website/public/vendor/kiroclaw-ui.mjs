// Legacy vendor stub (KiroClaw -> KiroCrew rename): re-exports the UI kit for
// already-installed app bundles that import '@kiroclaw/app-sdk/ui'. Reads the
// legacy global first, falling back to the current one — both point at the SAME
// host module instance (registered in shared-modules.ts).
const m =
  window.__kiroclaw_modules?.['@kiroclaw/ui'] ||
  window.__kirocrew_modules?.['@kirocrew/ui']
if (!m) throw new Error('[vendor/kiroclaw-ui] Host modules not initialized.')
export const {
  Card, CardTitle, Btn, SendBtn, Input, SearchInput,
  Badge, AimBadge, StatCard, Skeleton, ContentSkeleton,
  EmptyState, PageHeader, Toggle, InfoTip, SegmentedControl,
  MarkdownRenderer,
} = m
