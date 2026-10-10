import * as React from 'react'

/**
 * Plain-element stand-ins for a Radix menu family's submenu and radio
 * primitives, for suites that render a radio submenu under happy-dom (which
 * cannot open a real Radix submenu: no PointerEvent). Keeps Radix's radio
 * contract: the group hands `onValueChange` the clicked item's `value`, and
 * each item reports `aria-checked` against the group's current value.
 *
 * Use from a `vi.mock` factory:
 *   ...(await import('../test/stubRadioMenu')).stubRadioMenu('DropdownMenu')
 */
const Group = React.createContext<{ value?: string; onValueChange?: (v: string) => void }>({})

function Pass({ children }: { children?: React.ReactNode }) {
  return <div>{children}</div>
}

function RadioGroup({ children, value, onValueChange }: {
  children?: React.ReactNode
  value?: string
  onValueChange?: (v: string) => void
}) {
  return (
    <Group.Provider value={{ value, onValueChange }}>
      <div role="group">{children}</div>
    </Group.Provider>
  )
}

function RadioItem({ children, value, onSelect }: {
  children?: React.ReactNode
  value: string
  onSelect?: (event: Event) => void
}) {
  const group = React.useContext(Group)
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={group.value === value}
      onClick={() => {
        const event = new Event('select', { cancelable: true })
        onSelect?.(event)
        group.onValueChange?.(value)
        // Exposed so a test can assert the item asked the menu to stay open.
        document.body.dataset.lastRadioSelectPrevented = String(event.defaultPrevented)
      }}
    >
      {children}
    </button>
  )
}

function MenuItem({ children, onSelect, ...props }: {
  children?: React.ReactNode
  onSelect?: (event: Event) => void
  'aria-describedby'?: string
}) {
  return (
    <button type="button" role="menuitem" onClick={() => onSelect?.(new Event('select'))} {...props}>
      {children}
    </button>
  )
}

export function stubRadioMenu(prefix: 'DropdownMenu' | 'ContextMenu') {
  return {
    [`${prefix}Sub`]: Pass,
    [`${prefix}SubTrigger`]: Pass,
    [`${prefix}SubContent`]: Pass,
    [`${prefix}RadioGroup`]: RadioGroup,
    [`${prefix}RadioItem`]: RadioItem,
    [`${prefix}Item`]: MenuItem,
  }
}
