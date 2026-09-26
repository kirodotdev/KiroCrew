import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'

describe('api.autonudgeResume', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, loop: {} }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  afterEach(() => { fetchSpy.mockRestore() })

  it.each([0, 7])('sends captured generation %s with a typed-goal resume', async generation => {
    await api.autonudgeResume('goal/1', generation)
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/autonudge/goal%2F1')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(init.body as string)).toEqual({ active: true, expected_generation: generation })
  })

  it('preserves the goal-less caller payload when no generation is supplied', async () => {
    await api.autonudgeResume('legacy-1')
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(init.body as string)).toEqual({ active: true })
  })

  it('surfaces a stale generation without retrying against a newer one', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      error: 'The goal changed; refresh before resuming.',
      code: 'autonudge_update_refused',
    }), { status: 409, headers: { 'Content-Type': 'application/json' } }))
    await expect(api.autonudgeResume('goal-1', 7)).rejects.toThrow('The goal changed')
    expect(fetchSpy).toHaveBeenCalledOnce()
  })
})
