import type { Meta, StoryObj } from '@storybook/react-vite'
import { Plus, Trash2 } from 'lucide-react'
import { Btn } from '../components/ui'

const meta = {
  title: 'Primitives/Btn',
  component: Btn,
  args: { children: 'Save changes' },
  argTypes: {
    primary: { control: 'boolean' },
    danger: { control: 'boolean' },
    disabled: { control: 'boolean' },
  },
} satisfies Meta<typeof Btn>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}

export const Primary: Story = { args: { primary: true } }

export const Danger: Story = { args: { danger: true, children: 'Delete session' } }

export const Disabled: Story = { args: { disabled: true } }

/**
 * Specimen sheet: one variant per row, labelled, so every tone can be compared
 * under one theme. One button per row on purpose — a row of peer buttons is
 * capped at two in this codebase, and a specimen sheet is a list, not a toolbar.
 */
export const AllVariants: Story = {
  render: () => (
    <div className="grid grid-cols-[120px_auto] items-center gap-x-6 gap-y-3 text-[13px] text-muted">
      <span>default</span>
      <div><Btn>Default</Btn></div>
      <span>primary</span>
      <div><Btn primary>Primary</Btn></div>
      <span>danger</span>
      <div><Btn danger>Danger</Btn></div>
      <span>disabled</span>
      <div><Btn disabled>Disabled</Btn></div>
      <span>with icon</span>
      <div><Btn primary><Plus className="lucide-inline" /> New session</Btn></div>
      <span>danger + icon</span>
      <div><Btn danger><Trash2 className="lucide-inline" /> Remove</Btn></div>
    </div>
  ),
}
