/**
 * @kirocrew/ui — shared component library for KiroCrew apps.
 *
 * Apps import themed components from this module. In the host app, these
 * resolve to the same components used by core pages. When published as a
 * app bundle, the import map resolves `@kirocrew/app-sdk/ui` to the
 * host's vendored copy — ensuring a single set of components and styles.
 *
 * Usage in apps:
 *   import { Card, Btn, Badge, PageHeader } from '@kirocrew/app-sdk/ui'
 */

// Core primitives
export {
  Card,
  CardTitle,
  Btn,
  SendBtn,
  Input,
  SearchInput,
  Badge,
  SourceBadge,
  StatCard,
  Skeleton,
  ContentSkeleton,
  EmptyState,
  PageHeader,
  Toggle,
} from '../components/ui'

// Extended components
export { default as InfoTip } from '../components/InfoTip'
export { default as SegmentedControl } from '../components/SegmentedControl'
export type { Segment } from '../components/SegmentedControl'
export { default as MarkdownRenderer } from '../components/MarkdownRenderer'

export { default as Clickable } from '../components/Clickable'
export { default as Modal } from '../components/Modal'
export { default as ErrorNotice } from '../components/ErrorNotice'
export { SettingsSection, SettingsCard, SettingsInput, SettingsToggle, SettingsSelect } from '../components/settings'
