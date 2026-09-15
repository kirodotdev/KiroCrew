import React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'
import { Slider } from '../components/ui'

function StatefulSlider(props: React.ComponentProps<typeof Slider>) {
  const [value, setValue] = React.useState(props.value)
  return (
    <div className="max-w-[360px]">
      <Slider
        {...props}
        value={value}
        onChange={(v) => {
          setValue(v)
          props.onChange(v)
        }}
      />
    </div>
  )
}

const meta = {
  title: 'Primitives/Slider',
  component: Slider,
  render: (args) => <StatefulSlider {...args} />,
  args: { value: 40, onChange: fn(), min: 0, max: 100, step: 1, label: 'Volume', showValue: true, disabled: false },
} satisfies Meta<typeof Slider>

export default meta
type Story = StoryObj<typeof meta>

/** Fine steps: no tick marks, the knob tracks the pointer 1:1. */
export const Continuous: Story = {}

/** A small, even step count renders tick marks and springs to each notch. */
export const Discrete: Story = {
  args: { value: 3, min: 1, max: 5, step: 1, label: 'Reasoning effort', showValue: false },
}

/** The knob pulses an accent halo while parked at the max notch. */
export const EmphasizeMax: Story = {
  args: { value: 5, min: 1, max: 5, step: 1, label: 'Reasoning effort', emphasizeMax: true, showValue: false },
}

export const Formatted: Story = {
  args: { value: 0.7, min: 0, max: 1, step: 0.1, label: 'Temperature', formatValue: (v) => v.toFixed(1) },
}

export const Disabled: Story = { args: { disabled: true } }
