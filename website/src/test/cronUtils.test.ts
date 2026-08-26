import { describe, it, expect } from 'vitest'
import { expandDow, fmtCron } from '../utils/cronUtils'

describe('expandDow', () => {
  it('expands a range', () => {
    expect(expandDow('1-5')).toEqual([1, 2, 3, 4, 5])
  })
  it('passes through comma-separated values', () => {
    expect(expandDow('0,6')).toEqual([0, 6])
  })
  it('handles mixed range and values', () => {
    expect(expandDow('1-3,5')).toEqual([1, 2, 3, 5])
  })
  it('handles single value', () => {
    expect(expandDow('3')).toEqual([3])
  })
  it('handles reversed range (wrap-around)', () => {
    expect(expandDow('5-1')).toEqual([5, 6, 0, 1])
  })
  it('handles named day (MON)', () => {
    expect(expandDow('MON')).toEqual([1])
  })
  it('handles named range (MON-FRI)', () => {
    expect(expandDow('MON-FRI')).toEqual([1, 2, 3, 4, 5])
  })
  it('handles named comma-separated (MON,WED,FRI)', () => {
    expect(expandDow('MON,WED,FRI')).toEqual([1, 3, 5])
  })
  it('handles named wrap-around (FRI-MON)', () => {
    expect(expandDow('FRI-MON')).toEqual([5, 6, 0, 1])
  })
  it('handles lowercase named days', () => {
    expect(expandDow('mon-fri')).toEqual([1, 2, 3, 4, 5])
  })
  it('handles mixed named and numeric', () => {
    expect(expandDow('MON,3,FRI')).toEqual([1, 3, 5])
  })
  it('returns empty for invalid named input', () => {
    expect(expandDow('INVALID')).toEqual([])
  })
  it('returns empty for empty string', () => {
    expect(expandDow('')).toEqual([])
  })
  it('clamps out-of-bounds range to 0-6 wrap', () => {
    expect(expandDow('5-2')).toEqual([5, 6, 0, 1, 2])
  })
  it('normalizes dow 7 to 0 (Sunday)', () => {
    expect(expandDow('7')).toEqual([0])
  })
  it('normalizes range with 7 endpoint', () => {
    expect(expandDow('5-7')).toEqual([5, 6, 0])
  })
  it('deduplicates after normalization', () => {
    expect(expandDow('0,7')).toEqual([0])
  })
  it('expands 0-7 as every day', () => {
    expect(expandDow('0-7')).toEqual([0, 1, 2, 3, 4, 5, 6])
  })
  it('returns empty for step expressions', () => {
    expect(expandDow('*/2')).toEqual([])
    expect(expandDow('1-5/2')).toEqual([])
  })
})

describe('fmtCron', () => {
  it('includes day-of-month when not wildcard', () => {
    expect(fmtCron('0 9 1-3 * 1-5')).toBe('Mon,Tue,Wed,Thu,Fri 09:00 (days 1-3)')
  })
  it('formats Mon-Fri range correctly', () => {
    expect(fmtCron('0 9 * * 1-5')).toBe('Mon,Tue,Wed,Thu,Fri 09:00')
  })
  it('formats comma-separated days', () => {
    expect(fmtCron('30 15 * * 2,4')).toBe('Tue,Thu 15:30')
  })
  it('formats daily', () => {
    expect(fmtCron('0 8 * * *')).toBe('daily 08:00')
  })
  it('formats weekend', () => {
    expect(fmtCron('0 10 * * 0,6')).toBe('Sun,Sat 10:00')
  })
  it('formats reversed range (wrap-around)', () => {
    expect(fmtCron('0 9 * * 5-1')).toBe('Fri,Sat,Sun,Mon 09:00')
  })
  it('falls back to raw dow for step expressions', () => {
    expect(fmtCron('0 9 * * */2')).toBe('*/2 09:00')
  })
  it('formats named DOW range (MON-FRI)', () => {
    expect(fmtCron('0 13 * * MON-FRI')).toBe('Mon,Tue,Wed,Thu,Fri 13:00')
  })
  it('formats named DOW comma list', () => {
    expect(fmtCron('30 9 * * MON,WED,FRI')).toBe('Mon,Wed,Fri 09:30')
  })
})
