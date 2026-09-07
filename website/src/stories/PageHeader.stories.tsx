import type { Meta, StoryObj } from '@storybook/react-vite'
import { Plus, Settings } from 'lucide-react'
import { Badge, Btn, IconButton, PageHeader, PanelSectionHeader } from '../components/ui'

const meta = {
  title: 'Primitives/PageHeader',
  component: PageHeader,
  args: { title: 'Schedule', subtitle: 'Recurring jobs and one-shot reminders.' },
} satisfies Meta<typeof PageHeader>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}

export const TitleOnly: Story = { args: { subtitle: undefined } }

/** Header actions cap at two controls, so the right edge stays scannable. */
export const WithActions: Story = {
  args: {
    actions: (
      <>
        <IconButton aria-label="Settings"><Settings className="lucide-inline" /></IconButton>
        <Btn primary><Plus className="lucide-inline" /> New job</Btn>
      </>
    ),
  },
}

export const RichTitle: Story = {
  args: {
    title: (
      <span className="inline-flex items-center gap-2">
        Schedule <Badge variant="ok">12 active</Badge>
      </span>
    ),
  },
}

/** The one idiom for a counted list-section header inside a side panel. */
export const PanelSection: Story = {
  render: () => (
    <div className="flex flex-col gap-4 max-w-[280px]">
      <PanelSectionHeader label="Files" count={4} />
      <PanelSectionHeader label="Artifacts" />
      <PanelSectionHeader label="Pinned" count={0} trailing={<Btn>Clear</Btn>} />
    </div>
  ),
}
