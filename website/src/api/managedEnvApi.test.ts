import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiTransport } from './apiTransport'
import { managedEnvApi } from './managedEnvApi'

afterEach(() => vi.restoreAllMocks())

describe('managedEnvApi', () => {
  it('reads the list and posts an action to the lane routes', async () => {
    const res = {} as Response
    const get = vi.spyOn(apiTransport, 'get').mockResolvedValue(res)
    const post = vi.spyOn(apiTransport, 'post').mockResolvedValue(res)
    vi.spyOn(apiTransport, 'j').mockResolvedValue({ sandboxes: [] })
    await expect(managedEnvApi.list()).resolves.toEqual({ sandboxes: [] })
    expect(get).toHaveBeenCalledWith('/api/managed-environments/list')
    await managedEnvApi.act('env/1', 'stop')
    expect(post).toHaveBeenCalledWith('/api/managed-environments/env%2F1/stop', {})
    await managedEnvApi.sshConfig()
    expect(get).toHaveBeenLastCalledWith('/api/managed-environments/ssh-config')
    await managedEnvApi.addSshConfig()
    expect(post).toHaveBeenLastCalledWith('/api/managed-environments/ssh-config', {})
  })
})
