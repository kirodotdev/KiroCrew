import { describe, expect, it } from 'vitest'
import { lessonFacts, scopeIsAdmissible } from '../pages/overview/memoryPage/memoryListView'
import type { MemoryRecord } from '../types/memoryEditing'

const lesson = (scope: unknown) => ({ kind: 'directive', id: 'lesson.a', key: 'lesson.a', value_json: JSON.stringify({ rule: 'r', repo_scope: scope }) }) as unknown as MemoryRecord

describe('lesson scope', () => {
  it('keeps a real relative scope and marks one the gate refuses as unusable', () => {
    expect(['src/pkg', 'src/pkg/', ' web ', 'a\\b'].every(scopeIsAdmissible)).toBe(true)
    expect(['.', '..', 'a/./b', '/etc', 'C:/x', '', '  '].some(scopeIsAdmissible)).toBe(false)
    expect(lessonFacts(lesson('src/pkg')).scope).toBe('src/pkg')
    expect(lessonFacts(lesson('.')).scope).toBe(':unusable')
    expect(lessonFacts(lesson('')).scope).toBe(':unusable')
    expect(lessonFacts(lesson(['x'])).scope).toBe(':unusable')
    expect(lessonFacts(lesson(null)).scope).toBe('')
  })
})
