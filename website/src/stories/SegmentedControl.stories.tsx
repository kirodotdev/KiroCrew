import React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'
import { CheckCircle2, Clock, LayoutGrid, List, XCircle } from 'lucide-react'
import SegmentedControl, { type Segment } from '../components/SegmentedControl'

type Filter = 'all' | 'running' | 'done' | 'failed'
type Props = React.ComponentProps<typeof SegmentedControl<Filter>>

const FILTERS: Segment<Filter>[] = [
  { key: 'all', label: 'All', count: 24 },
  { key: 'running', label: 'Running', icon: <Clock className="lucide-inline" />, count: 3 },
  { key: 'done', label: 'Done', icon: <CheckCircle2 className="lucide-inline" />, count: 19 },
  { key: 'failed', label: 'Failed', icon: <XCircle className="lucide-inline" />, count: 2, tooltip: 'Jobs whose last run exited non-zero' },
]

/** `SegmentedControl` is controlled; the story owns the selection and reports it through `onChange`. */
function StatefulControl<T extends string>(props: React.ComponentProps<typeof SegmentedControl<T>>) {
  const [value, setValue] = React.useState(props.value)
  return (
    <SegmentedControl
      {...props}
      value={value}
      onChange={(v) => {
        setValue(v)
        props.onChange(v)
      }}
    />
  )
}

// The component is generic over its segment key, which `Meta<typeof Component>`
// cannot carry; typing the meta on the instantiated props keeps `args` checked.
const meta: Meta<Props> = {
  title: 'Primitives/SegmentedControl',
  render: (args) => <StatefulControl {...args} />,
  args: { segments: FILTERS, value: 'all', onChange: fn(), layoutId: 'story', collapse: false },
}

export default meta
type Story = StoryObj<Props>

/** A filter over one view: which subset am I looking at. Not navigation. */
export const Default: Story = {}

/** Every segment keeps its icon; only the selected one keeps its label. */
export const Compact: Story = { args: { compact: true } }

/** A row of icon buttons; each label moves to `aria-label` and `title`. */
export const IconOnly: Story = {
  render: () => (
    <StatefulControl<'grid' | 'list'>
      iconOnly
      collapse={false}
      layoutId="story-icon-only"
      value="grid"
      onChange={fn()}
      segments={[
        { key: 'grid', label: 'Grid', icon: <LayoutGrid className="lucide-inline" /> },
        { key: 'list', label: 'List', icon: <List className="lucide-inline" /> },
      ]}
    />
  ),
}

/** A disabled segment stays reachable and keeps its tooltip, which carries the reason. */
export const WithDisabledSegment: Story = {
  args: {
    segments: FILTERS.map((s) => (s.key === 'failed' ? { ...s, disabled: true, tooltip: 'Coming soon' } : s)),
  },
}

/**
 * Responsive collapse measures the PARENT, so the parent must own its width. Resize
 * the viewport: the control steps from full labels to icons to a dropdown.
 */
export const CollapsingInNarrowParent: Story = {
  render: (args) => (
    <div style={{ width: 220, border: '1px dashed var(--border)', padding: 8 }}>
      <StatefulControl {...args} collapse />
    </div>
  ),
}
