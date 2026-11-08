import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError } from '../api/client'
import { launchPreviewErrorKey, readLaunchPreviews } from '../pages/settings/mcpManagementModel'

afterEach(() => vi.restoreAllMocks())

function preview(name: string) {
  return { name, commands: [['npx', name]], envs: [[]], complete: true, expected_launch: `${name}-id` }
}

describe('readLaunchPreviews', () => {
  it('reads every candidate in one request and keeps each name apart', async () => {
    const read = vi.spyOn(api, 'mcpGatewayLaunchPreviews').mockResolvedValue({
      launches: { 'a-mcp': preview('a-mcp') },
      errors: { 'b-mcp': 'launch_over_cap' },
    } as never)

    const review = await readLaunchPreviews(['a-mcp', 'b-mcp', 'c-mcp'])

    expect(read).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenCalledWith(['a-mcp', 'b-mcp', 'c-mcp'])
    expect(review).toEqual([
      { name: 'a-mcp', preview: preview('a-mcp') },
      { name: 'b-mcp', error: { code: 'launch_over_cap' } },
      { name: 'c-mcp', error: { code: 'launch_resolve_failed' } },
    ])
    expect(launchPreviewErrorKey(review[1].error)).toBe('pages.mcpManagement.launch_preview_error_over_cap')
  })

  it('does not read a name that matches an inherited object key', async () => {
    vi.spyOn(api, 'mcpGatewayLaunchPreviews').mockResolvedValue({
      launches: { 'a-mcp': preview('a-mcp') },
      errors: { constructor: 'launch_unresolved' },
    } as never)

    const review = await readLaunchPreviews(['a-mcp', 'constructor', 'toString'])

    expect(review).toEqual([
      { name: 'a-mcp', preview: preview('a-mcp') },
      { name: 'constructor', error: { code: 'launch_unresolved' } },
      { name: 'toString', error: { code: 'launch_resolve_failed' } },
    ])
  })

  it('splits a list larger than the server cap and fails only the chunk that failed', async () => {
    const names = Array.from({ length: 250 }, (_, i) => `s${i}-mcp`)
    const refused = new ApiError(503, 'down', JSON.stringify({ code: 'launch_resolve_failed' }))
    const read = vi.spyOn(api, 'mcpGatewayLaunchPreviews').mockImplementation((async (chunk: string[]) => {
      if (chunk.length === 50) throw refused
      return { launches: Object.fromEntries(chunk.map(n => [n, preview(n)])), errors: {} }
    }) as never)

    const review = await readLaunchPreviews(names)

    expect(read).toHaveBeenCalledTimes(2)
    expect(read.mock.calls.map(([chunk]) => (chunk as string[]).length)).toEqual([200, 50])
    expect(review.map(r => r.name)).toEqual(names)
    expect(review.slice(0, 200).every(r => r.preview)).toBe(true)
    expect(review.slice(200).every(r => r.error === refused)).toBe(true)
  })
})
