import { describe, it, expect } from 'vitest'
import { REMOTE_CREW_SETTINGS_PATH, remoteCrewAddFromPath } from './remoteCrewAddLink'

describe('remoteCrewAddFromPath', () => {
  it('reads the route the desktop resolver builds for a remote-crew add link', () => {
    expect(remoteCrewAddFromPath('/settings/instances?host=me%40devbox.example.com&port=5476&name=Dev%20box')).toEqual({
      host: 'me@devbox.example.com',
      port: '5476',
      name: 'Dev box',
    })
    expect(remoteCrewAddFromPath('/settings/instances?host=devbox')).toEqual({ host: 'devbox', port: '', name: '' })
    expect(remoteCrewAddFromPath(`/settings/instances?host=h&name=${encodeURIComponent('Café & co')}`)?.name).toBe('Café & co')
  })

  it('leaves every ordinary navigation alone, a bare Remote Crew visit included', () => {
    for (const path of [
      REMOTE_CREW_SETTINGS_PATH,
      '/settings',
      '/settings/about',
      '/settings/instances?port=5476',
      '/chat?sid=cos-quick-chat',
      '/apps/detail/mochi',
      '/settings/instances/extra?host=h',
      '/settingsxinstances?host=h',
    ]) {
      expect(remoteCrewAddFromPath(path), path).toBeNull()
    }
  })

  it('refuses a route the resolver would not have built', () => {
    for (const path of [
      '/settings/instances?host=',
      '/settings/instances?host=a&host=b',
      '/settings/instances?host=h&token=abc',
      '/settings/instances?host=h&remote_bin=%2Ftmp%2Fx',
      '/settings/instances?host=h&port=0',
      '/settings/instances?host=h&port=65536',
      '/settings/instances?host=h&port=80abc',
      `/settings/instances?host=${'a'.repeat(256)}`,
      `/settings/instances?host=h&name=${'a'.repeat(201)}`,
    ]) {
      expect(remoteCrewAddFromPath(path), path).toBeNull()
    }
    expect(remoteCrewAddFromPath(`/settings/instances?host=h&name=${'a'.repeat(200)}`)?.name).toHaveLength(200)
  })
})
