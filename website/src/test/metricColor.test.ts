import { describe, it, expect } from 'vitest'
// Imported from `utils/metricColor`, NOT from `../App`. Reaching this one-line
// pure function through the app root pulled the whole eager graph (router,
// store, react-query, every registered page, the i18n catalogs) into this fork
// to run three assertions — measured 104s, of which 77ms was the tests.
import { metricColor } from '../utils/metricColor'

describe('metricColor', () => {
  it('returns text-muted for normal usage at or below 70%', () => {
    expect(metricColor(0)).toBe('text-muted')
    expect(metricColor(0.5)).toBe('text-muted')
    expect(metricColor(0.69)).toBe('text-muted')
    expect(metricColor(0.7)).toBe('text-muted')
  })

  it('returns text-warn (yellow) for usage between 70-90%', () => {
    expect(metricColor(0.71)).toBe('text-warn')
    expect(metricColor(0.8)).toBe('text-warn')
    expect(metricColor(0.9)).toBe('text-warn')
  })

  it('returns text-danger (red) for usage above 90%', () => {
    expect(metricColor(0.91)).toBe('text-danger')
    expect(metricColor(0.95)).toBe('text-danger')
    expect(metricColor(1.0)).toBe('text-danger')
  })
})
