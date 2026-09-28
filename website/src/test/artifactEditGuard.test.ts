/**
 * The edit-buffer registry the WS transport consults before refreshing an
 * artifact's cached content. Small enough to pin directly, and worth pinning
 * because a leak in either direction is silent: leaving a slug marked blocks live
 * refresh forever, and clearing too eagerly re-opens the lost-update it prevents.
 */
import { describe, it, expect, afterEach } from 'vitest'
import {
  setArtifactEditing,
  isArtifactEditing,
  __resetArtifactEditing,
} from '../utils/artifactEditGuard'

afterEach(() => __resetArtifactEditing())

describe('artifactEditGuard', () => {
  it('is false for an untracked slug', () => {
    expect(isArtifactEditing('cr-queue')).toBe(false)
  })

  it('marks and clears a slug', () => {
    setArtifactEditing('cr-queue', true)
    expect(isArtifactEditing('cr-queue')).toBe(true)
    setArtifactEditing('cr-queue', false)
    expect(isArtifactEditing('cr-queue')).toBe(false)
  })

  it('tracks slugs independently', () => {
    setArtifactEditing('a', true)
    expect(isArtifactEditing('a')).toBe(true)
    expect(isArtifactEditing('b')).toBe(false)
  })

  it('ignores an empty slug rather than tracking a falsy key', () => {
    setArtifactEditing('', true)
    expect(isArtifactEditing('')).toBe(false)
  })

  it('is idempotent', () => {
    setArtifactEditing('a', true)
    setArtifactEditing('a', true)
    setArtifactEditing('a', false)
    expect(isArtifactEditing('a')).toBe(false)
  })
})
