import type { Meta, StoryObj } from '@storybook/react-vite'
import { Inbox, Plus } from 'lucide-react'
import { Btn, EmptyState, FilteredEmpty } from '../components/ui'

const meta = {
  title: 'Primitives/EmptyState',
  component: EmptyState,
  args: {
    icon: <Inbox className="lucide-inline" />,
    title: 'No sessions yet',
    subtitle: 'Start a conversation and it will show up here.',
  },
} satisfies Meta<typeof EmptyState>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}

export const WithAction: Story = {
  args: {
    action: (
      <Btn primary>
        <Plus className="lucide-inline" /> New session
      </Btn>
    ),
  },
}

export const TitleOnly: Story = { args: { subtitle: undefined } }

/**
 * The lighter sibling for "your data exists, your filter hid it": echoes the query
 * back and offers to clear it. No large icon on purpose.
 */
export const Filtered: Story = {
  render: () => <FilteredEmpty query="deploy" noun="sessions" onClear={() => {}} />,
}
