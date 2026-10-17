import React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'
import { Toggle } from '../components/ui'

/**
 * `Toggle` is controlled, so a story that only passes `checked` would be frozen.
 * Each story owns the state and reports flips through the `onChange` action.
 */
function StatefulToggle(props: React.ComponentProps<typeof Toggle>) {
  const [checked, setChecked] = React.useState(props.checked)
  return (
    <Toggle
      {...props}
      checked={checked}
      onChange={(v) => {
        setChecked(v)
        props.onChange(v)
      }}
    />
  )
}

const meta = {
  title: 'Primitives/Toggle',
  component: Toggle,
  render: (args) => <StatefulToggle {...args} />,
  args: { checked: true, onChange: fn(), label: 'Enable notifications', disabled: false, tone: 'accent' },
  argTypes: {
    tone: { control: 'inline-radio', options: ['accent', 'muted'] },
  },
} satisfies Meta<typeof Toggle>

export default meta
type Story = StoryObj<typeof meta>

export const On: Story = {}

export const Off: Story = { args: { checked: false } }

export const Disabled: Story = { args: { disabled: true } }

/** The `muted` tone is for a list of switches where an accent fill on every row shouts. */
export const MutedList: Story = {
  render: () => (
    <div className="flex flex-col gap-3 max-w-[320px]">
      {['Sound', 'Badge count', 'Desktop banner'].map((name, i) => (
        <div key={name} className="flex items-center justify-between text-[13px]">
          <span>{name}</span>
          <StatefulToggle checked={i !== 1} onChange={() => {}} label={name} tone="muted" />
        </div>
      ))}
    </div>
  ),
}
