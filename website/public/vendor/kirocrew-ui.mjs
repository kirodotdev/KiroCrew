// Vendor stub: re-exports @kirocrew/ui from the host.
const m = window.__kirocrew_modules?.['@kirocrew/ui']
if (!m) throw new Error('[vendor/kirocrew-ui] Host modules not initialized.')
export const {
  Card, CardTitle, Btn, SendBtn, Input, SearchInput,
  Badge, AimBadge, StatCard, Skeleton, ContentSkeleton,
  EmptyState, PageHeader, Toggle, InfoTip, SegmentedControl,
  MarkdownRenderer,
} = m
