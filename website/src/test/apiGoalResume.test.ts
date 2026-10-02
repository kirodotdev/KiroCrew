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

describe('api.autonudgeDismiss', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  afterEach(() => { fetchSpy.mockRestore() })

  it('deletes only with the dismiss intent and the rendered generation', async () => {
    await api.autonudgeDismiss('goal/1', 4)
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/autonudge/goal%2F1?intent=dismiss&expected_generation=4')
    expect(init.method).toBe('DELETE')
  })

  it('surfaces a changed suggestion without retrying', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      error: 'This suggestion changed since it was shown.',
      code: 'goal_changed',
    }), { status: 409, headers: { 'Content-Type': 'application/json' } }))
    await expect(api.autonudgeDismiss('goal-1', 4)).rejects.toThrow('This suggestion changed')
    expect(fetchSpy).toHaveBeenCalledOnce()
  })
})
