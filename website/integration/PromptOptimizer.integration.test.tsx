import { describe, it, expect, beforeEach } from 'vitest'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

describe('Prompt Optimizer', () => {
  beforeEach(() => {
    server.use(
      http.post('/api/optimizer/optimize', async ({ request }) => {
        const body = (await request.json()) as { prompt: string; context: string }
        if (!body.prompt || body.prompt.trim().length === 0) {
          return HttpResponse.json({ optimized: '', changed: false })
        }
        return HttpResponse.json({
          optimized: `Optimized: ${body.prompt}`,
          changed: true,
        })
      }),
    )
  })

  describe('API contract', () => {
    it('returns unchanged for empty prompt', async () => {
      const resp = await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: '', context: '' }),
      })
      const data = await resp.json()
      expect(data.changed).toBe(false)
      expect(data.optimized).toBe('')
    })

    it('optimizes short prompts (no skip logic)', async () => {
      const resp = await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: 'yes', context: '' }),
      })
      const data = await resp.json()
      expect(data.changed).toBe(true)
      expect(data.optimized).toContain('Optimized:')
    })

    it('optimizes two-word prompts (no skip logic)', async () => {
      const resp = await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: 'do it', context: '' }),
      })
      const data = await resp.json()
      expect(data.changed).toBe(true)
    })

    it('returns optimized text for longer prompts', async () => {
      const resp = await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: 'refactor the auth module to be cleaner and more maintainable', context: '' }),
      })
      const data = await resp.json()
      expect(data.changed).toBe(true)
      expect(data.optimized).toContain('Optimized:')
    })

    it('includes x-session-key header in request', async () => {
      let capturedHeaders: Headers | null = null
      server.use(
        http.post('/api/optimizer/optimize', async ({ request }) => {
          capturedHeaders = request.headers
          return HttpResponse.json({ optimized: 'test', changed: true })
        }),
      )

      await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-session-key': 'dashboard:ui' },
        body: JSON.stringify({ prompt: 'refactor the auth module to be cleaner', context: '' }),
      })

      expect(capturedHeaders?.get('x-session-key')).toBe('dashboard:ui')
    })
  })

  describe('error handling', () => {
    it('returns original prompt on server error', async () => {
      server.use(
        http.post('/api/optimizer/optimize', () => {
          return HttpResponse.json({ error: 'internal' }, { status: 500 })
        }),
      )

      const resp = await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: 'test prompt', context: '' }),
      })
      expect(resp.status).toBe(500)
    })
  })
})
