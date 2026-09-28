import { describe, it, expect } from 'vitest'
import { groupJobsByFolder } from '../utils/cronFolders'
import type { CronJob } from '../types'
import type { CronFolder } from '../utils/cronFolders'

const mkJob = (id: string, folderId?: string): CronJob => ({
  id,
  name: `Job ${id}`,
  message: 'msg',
  enabled: true,
  schedule: 'every 1h',
  last_status: 'ok',
  folder_id: folderId || '',
} as CronJob)

const mkFolder = (id: string, name: string, order: number): CronFolder => ({ id, name, order })

describe('groupJobsByFolder', () => {
  it('returns all jobs ungrouped when no folders exist', () => {
    const jobs = [mkJob('a'), mkJob('b')]
    const result = groupJobsByFolder(jobs, [])
    expect(result).toHaveLength(1)
    expect(result[0].folder).toBeNull()
    expect(result[0].jobs).toHaveLength(2)
  })

  it('groups jobs into their folders in order', () => {
    const folders = [mkFolder('f2', 'Beta', 2), mkFolder('f1', 'Alpha', 1)]
    const jobs = [mkJob('a', 'f1'), mkJob('b', 'f2'), mkJob('c', 'f1'), mkJob('d')]
    const result = groupJobsByFolder(jobs, folders)

    expect(result).toHaveLength(3)
    // Alpha first (order=1)
    expect(result[0].folder!.name).toBe('Alpha')
    expect(result[0].jobs.map(j => j.id)).toEqual(['a', 'c'])
    // Beta second (order=2)
    expect(result[1].folder!.name).toBe('Beta')
    expect(result[1].jobs.map(j => j.id)).toEqual(['b'])
    // Ungrouped last
    expect(result[2].folder).toBeNull()
    expect(result[2].jobs.map(j => j.id)).toEqual(['d'])
  })

  it('shows empty folders by default (no omitEmpty option)', () => {
    const folders = [mkFolder('f1', 'Alpha', 1), mkFolder('f2', 'Empty', 2)]
    const jobs = [mkJob('a', 'f1')]
    const result = groupJobsByFolder(jobs, folders)
    expect(result).toHaveLength(3)
    expect(result[0].folder!.name).toBe('Alpha')
    expect(result[0].jobs).toHaveLength(1)
    expect(result[1].folder!.name).toBe('Empty')
    expect(result[1].jobs).toHaveLength(0)
    expect(result[2].folder).toBeNull()
  })

  it('omits empty folders when omitEmpty is true', () => {
    const folders = [mkFolder('f1', 'Alpha', 1), mkFolder('f2', 'Empty', 2)]
    const jobs = [mkJob('a', 'f1')]
    const result = groupJobsByFolder(jobs, folders, { omitEmpty: true })
    expect(result).toHaveLength(2) // Alpha + ungrouped
    expect(result[0].folder!.name).toBe('Alpha')
    expect(result[1].folder).toBeNull()
  })

  it('shows empty folders when omitEmpty is false', () => {
    const folders = [mkFolder('f1', 'Alpha', 1), mkFolder('f2', 'Empty', 2)]
    const jobs = [mkJob('a', 'f1')]
    const result = groupJobsByFolder(jobs, folders, { omitEmpty: false })
    expect(result).toHaveLength(3)
    expect(result[1].folder!.name).toBe('Empty')
    expect(result[1].jobs).toHaveLength(0)
  })

  it('handles jobs referencing non-existent folder as ungrouped', () => {
    const folders = [mkFolder('f1', 'Alpha', 1)]
    const jobs = [mkJob('a', 'ghost-folder'), mkJob('b', 'f1')]
    const result = groupJobsByFolder(jobs, folders)
    // ghost-folder job goes to ungrouped
    expect(result[1].folder).toBeNull()
    expect(result[1].jobs.map(j => j.id)).toEqual(['a'])
  })
})
