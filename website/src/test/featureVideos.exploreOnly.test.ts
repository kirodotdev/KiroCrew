/**
 * Feature videos show only in the Explore panel's video card (RFC "Milestones
 * for New-User Feature Discovery", 8.11). There is no startup popup: nothing in
 * the app shell mounts one, and the client has no route to the old startup pick.
 */
import { describe, it, expect } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { api } from '../api/client'

const SRC = path.resolve(__dirname, '..')
const read = (rel: string) => readFileSync(path.join(SRC, rel), 'utf-8')

describe('feature videos live only in Explore', () => {
  it('mounts no startup video popup from the app shell', () => {
    for (const rel of ['App.tsx', 'shell/appShell.tsx']) {
      const source = read(rel)
      expect(source, rel).not.toMatch(/StartupVideo|startupVideo/)
    }
    expect(existsSync(path.join(SRC, 'components/StartupVideoModal.tsx'))).toBe(false)
    expect(existsSync(path.join(SRC, 'shell/boot/startupVideo.tsx'))).toBe(false)
  })

  it('has no client call to the old startup pick', () => {
    expect('featureVideoNext' in api).toBe(false)
    expect(read('api/client/featureDiscovery.ts')).not.toContain('/api/feature-videos/next')
  })

  it('renders the Explore video card at the top of the Explore sheet', () => {
    expect(read('components/milestones/MilestonesSheet.tsx')).toContain('<ExploreIntroVideo')
  })
})
