import { describe, it, expect } from 'vitest'
import { offlineProps } from '../utils/offline'

describe('offlineProps', () => {
  it('returns only aria-disabled:false when online, leaving caller title/label intact', () => {
    expect(offlineProps(true, 'send', 'Send')).toEqual({ 'aria-disabled': false })
  })

  it('returns the offline tooltip + aria-disabled when offline (no label)', () => {
    expect(offlineProps(false, 'switch sessions')).toEqual({
      'aria-disabled': true,
      title: 'Gateway offline — reconnect to switch sessions',
    })
  })

  it('adds a disabled aria-label when a label is supplied and offline', () => {
    expect(offlineProps(false, 'send', 'Send')).toEqual({
      'aria-disabled': true,
      title: 'Gateway offline — reconnect to send',
      'aria-label': 'Send disabled — gateway offline',
    })
  })

  it('omits aria-label when no label is supplied', () => {
    expect(offlineProps(false, 'optimize')['aria-label']).toBeUndefined()
  })

  it('interpolates the verb into the tooltip', () => {
    expect(offlineProps(false, 'resume sessions').title).toBe(
      'Gateway offline — reconnect to resume sessions',
    )
  })
})
