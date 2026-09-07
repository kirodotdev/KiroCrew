import type { Meta, StoryObj } from '@storybook/react-vite'
import { Badge, SourceBadge } from '../components/ui'

const meta = {
  title: 'Primitives/Badge',
  component: Badge,
  args: { variant: 'ok', children: 'running' },
  argTypes: {
    variant: { control: 'select', options: ['ok', 'err', 'warn', 'aim', 'muted'] },
  },
} satisfies Meta<typeof Badge>

export default meta
type Story = StoryObj<typeof meta>

export const Ok: Story = {}

export const Error: Story = { args: { variant: 'err', children: 'failed' } }

export const Warn: Story = { args: { variant: 'warn', children: 'pending' } }

/** Every status tone next to each other, so a palette can be checked in one glance. */
export const AllVariants: Story = {
  render: () => (
    <div className="flex flex-wrap items-center gap-3">
      <Badge variant="ok">ok</Badge>
      <Badge variant="err">err</Badge>
      <Badge variant="warn">warn</Badge>
      <Badge variant="aim">aim</Badge>
      <Badge variant="muted">muted</Badge>
    </div>
  ),
}

/** The provenance pill: one color per known source, a neutral pill for anything else. */
export const Sources: Story = {
  render: () => (
    <div className="flex flex-wrap items-center gap-3">
      <SourceBadge source="package" />
      <SourceBadge source="kirocrew" />
      <SourceBadge source="project" />
      <SourceBadge source="somewhere-else" />
    </div>
  ),
}
