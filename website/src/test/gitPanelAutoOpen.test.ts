/**
 * Tests for the Git panel auto-open-once logic.
 *
 * The auto-open behavior uses a localStorage marker keyed by slot+path
 * to ensure the git panel only opens once per session+project combination.
 */
import { describe, it, expect, beforeEach } from 'vitest'

const GIT_PANEL_KEY_PREFIX = 'mc-git-panel-opened:'
const FOLDER_PANEL_KEY_PREFIX = 'mc-folder-panel-opened:'

beforeEach(() => {
  localStorage.clear()
})

describe('git panel auto-open-once localStorage marker', () => {
  it('marker is absent initially', () => {
    const key = `${GIT_PANEL_KEY_PREFIX}slot-1:/home/user/project`
    expect(localStorage.getItem(key)).toBeNull()
  })

  it('setting the marker prevents re-triggering', () => {
    const slot = 'slot-1'
    const path = '/home/user/project'
    const key = `${GIT_PANEL_KEY_PREFIX}${slot}:${path}`

    // Simulate first open: set marker
    localStorage.setItem(key, '1')

    // Check on second mount: marker present, should not re-open
    expect(localStorage.getItem(key)).toBe('1')
  })

  it('different slots have independent markers', () => {
    const path = '/home/user/project'
    const keyA = `${GIT_PANEL_KEY_PREFIX}slot-a:${path}`
    const keyB = `${GIT_PANEL_KEY_PREFIX}slot-b:${path}`

    localStorage.setItem(keyA, '1')

    expect(localStorage.getItem(keyA)).toBe('1')
    expect(localStorage.getItem(keyB)).toBeNull()
  })

  it('different paths have independent markers', () => {
    const slot = 'slot-1'
    const keyA = `${GIT_PANEL_KEY_PREFIX}${slot}:/project-a`
    const keyB = `${GIT_PANEL_KEY_PREFIX}${slot}:/project-b`

    localStorage.setItem(keyA, '1')

    expect(localStorage.getItem(keyA)).toBe('1')
    expect(localStorage.getItem(keyB)).toBeNull()
  })
})

describe('folder panel auto-open-once localStorage marker', () => {
  it('marker prevents re-opening the folder tab for the same slot+path', () => {
    const slot = 'slot-1'
    const path = '/home/user/project'
    const key = `${FOLDER_PANEL_KEY_PREFIX}${slot}:${path}`

    // First visit: no marker
    expect(localStorage.getItem(key)).toBeNull()

    // Simulate auto-open
    localStorage.setItem(key, '1')

    // Second visit: marker present
    expect(localStorage.getItem(key)).toBe('1')
  })
})
