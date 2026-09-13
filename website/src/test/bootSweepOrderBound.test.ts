import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const source = readFileSync(resolve(__dirname, '../App.tsx'), 'utf-8')

describe('the boot sweep uses the ordering that came with its own snapshot', () => {
  it('passes the order carried by the applied reply', () => {
    expect(source).toMatch(/gcOrphanedStorage\(appliedBootSlots\.slots, appliedBootSlots\.order\)/)
  })

  it('does not re-read the store inside the sweep effect', () => {
    const effect = source.slice(
      source.indexOf('if (appliedBootSlots)') - 400,
      source.indexOf('}, [appliedBootSlots])'),
    )

    expect(effect).not.toMatch(/store\.getState\(\)/)
  })
})
