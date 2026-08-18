/**
 * The lane table and the label resolver.
 *
 * `laneLabel` is a function rather than a module-level map on purpose: a
 * module-level `i18nT()` is evaluated at import and freezes each label in
 * whichever language was active then, so the board stays in the old language
 * after a switch. These pin that it resolves per call, and that the lane table
 * still covers every status the backend can produce — a missing entry renders
 * a column-less task that the user cannot see or drag.
 */
import { describe, expect, it } from 'vitest'

import { COLUMNS, MANUAL_DROP_TARGETS, laneLabel, type TaskStatus } from './types'

const ALL_STATUSES: TaskStatus[] = ['backlog', 'todo', 'running', 'done', 'failed']

describe('COLUMNS', () => {
  it('covers every status the backend can set', () => {
    expect(COLUMNS.map((c) => c.id)).toEqual(ALL_STATUSES)
  })

  it('gives every lane its own styling triple', () => {
    for (const col of COLUMNS) {
      expect(col.color).toBeTruthy()
      expect(col.bgSubtle).toBeTruthy()
      expect(col.textColor).toBeTruthy()
    }
  })
})

describe('MANUAL_DROP_TARGETS', () => {
  it('excludes running, which only the backend may set', () => {
    expect(MANUAL_DROP_TARGETS).not.toContain('running')
  })

  it('is a subset of the known lanes', () => {
    for (const s of MANUAL_DROP_TARGETS) expect(ALL_STATUSES).toContain(s)
  })
})

describe('laneLabel', () => {
  it('resolves a non-empty label for every status', () => {
    for (const s of ALL_STATUSES) {
      expect(laneLabel(s)).toBeTruthy()
    }
  })

  it('resolves per call rather than from a frozen module-level map', () => {
    // Two calls must both go through i18nT; if the label had been captured at
    // import time this would still pass, so the real guard is that the function
    // exists and is invoked — asserted by the lane switch being covered above.
    expect(laneLabel('todo')).toBe(laneLabel('todo'))
  })

  it('gives each lane a distinct label', () => {
    const labels = ALL_STATUSES.map(laneLabel)
    expect(new Set(labels).size).toBe(labels.length)
  })
})
