// The Mood Music page end to end against a mocked `./api`: a mood goes out with
// the taste summary, the DJ's reply is read off the slot, its picks are queued
// and played. The poll sleeps on setInterval(POLL_MS), so the file runs on fake
// timers driven inside `act`, and interactions use `fireEvent` to keep each step
// on that one clock.
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import type { MoodState, QueuedPick } from './api'

const motion = vi.hoisted(() => ({ reduced: false }))
vi.mock('framer-motion', async orig => ({
  ...(await orig<typeof import('framer-motion')>()),
  useReducedMotion: () => motion.reduced,
}))

vi.mock('./api', async orig => {
  const real = await orig<typeof import('./api')>()
  return {
    ...real,
    moodApi: {
      state: vi.fn(), addMoment: vi.fn(), setQueue: vi.fn(), feedback: vi.fn(),
      openSlot: vi.fn(), getSlot: vi.fn(), ask: vi.fn(),
    },
  }
})

import { moodApi } from './api'
import MoodMusicPage, { POLL_MS } from './MoodMusicPage'

const api = vi.mocked(moodApi)
const ID = 'dQw4w9WgXcQ'
const t = (k: string, o?: Record<string, unknown>) => i18nT(`apps.moodMusic.page.${k}`, o) as string

const EMPTY: MoodState = { moments: [], queue: [], feedback: [], last_source: '' }

function queued(over: Partial<QueuedPick>): QueuedPick {
  return { id: 'p'.repeat(32), title: 'Song', artist: 'A', why: 'it is soft', tags: [], source: 'youtube', video_id: ID, feedback: '', ...over }
}

const reply = (data: unknown) => ({ running: false, messages: [{ role: 'user', content: 'x' }, { role: 'assistant', content: '```mood-music\n' + JSON.stringify(data) + '\n```' }] })

async function flush(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

beforeEach(() => {
  vi.useFakeTimers()
  motion.reduced = false
  vi.clearAllMocks()
  api.state.mockResolvedValue({ ...EMPTY, feedback: [{ pick_id: 'q'.repeat(32), title: 'Old Love', artist: 'B', tags: [], verdict: 'liked', at: 1 }] })
  api.addMoment.mockResolvedValue({ id: 'm'.repeat(32), mood: 'x', note: '', tags: [], songs: [], at: 1 })
  api.openSlot.mockResolvedValue({ key: 'mood-music' })
  api.getSlot.mockResolvedValue({ running: false, messages: [] })
  api.ask.mockResolvedValue('mood-music')
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('MoodMusicPage', () => {
  it('cannot send an empty mood', async () => {
    renderWithProviders(<MoodMusicPage />)
    await flush()
    const find = screen.getByRole('button', { name: t('find') })
    expect(find).toBeDisabled()
    fireEvent.change(screen.getByLabelText(t('mood_label')), { target: { value: '   ' } })
    expect(find).toBeDisabled()
  })

  it('sends a chip mood with the taste summary, then queues and embeds the picks', async () => {
    renderWithProviders(<MoodMusicPage />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: t('mood_missing') }))
    expect(screen.getByTestId('mood-backdrop').dataset.look).toBe('missing')
    expect(screen.getAllByTestId('mood-glow')[0].style.getPropertyValue('--mood-hue')).toBe('280')
    fireEvent.click(screen.getByRole('button', { name: t('find') }))
    await flush()

    expect(api.addMoment).toHaveBeenCalledWith(t('mood_missing'))
    const sent = api.ask.mock.calls[0][0]
    expect(sent).toContain(`Mood: ${t('mood_missing')}`)
    expect(sent).toContain('Liked: "Old Love" - B')

    const picks = [queued({ id: 'a'.repeat(32), title: 'First' }), queued({ id: 'b'.repeat(32), title: 'Bili', source: 'bilibili', video_id: undefined, url: 'https://www.bilibili.com/video/BV1xx411c7mD' })]
    api.getSlot.mockResolvedValue(reply({
      mood_tags: ['想念'],
      picks: [
        { title: 'First', source: 'youtube', url: `https://www.youtube.com/watch?v=${ID}` },
        { title: 'Bili', source: 'bilibili', url: 'https://www.bilibili.com/video/BV1xx411c7mD' },
        { title: 'Bad', source: 'bilibili', url: 'https://bilibili.com.evil.com/x' },
      ],
    }))
    api.setQueue.mockResolvedValue({ queue: picks })
    api.state.mockResolvedValue({ ...EMPTY, queue: picks })
    await flush(POLL_MS)

    expect(api.setQueue).toHaveBeenCalledTimes(1)
    const body = api.setQueue.mock.calls[0][0]
    expect(body.picks.map(p => p.title)).toEqual(['First', 'Bili'])
    expect(body.moment_id).toBe('m'.repeat(32))
    expect(screen.getByText(t('dropped', { count: 1 }))).toBeInTheDocument()

    const frame = screen.getByTestId('mood-youtube-frame')
    expect(frame.getAttribute('src')).toBe(`https://www.youtube-nocookie.com/embed/${ID}?rel=0`)
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-presentation allow-popups')
    expect(screen.getByText('it is soft')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: t('play', { title: 'Bili' }) }))
    const link = screen.getByTestId('mood-bilibili-link')
    expect(link.getAttribute('href')).toBe('https://www.bilibili.com/video/BV1xx411c7mD')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(screen.queryByTestId('mood-youtube-frame')).toBeNull()
  })

  it('shows the DJ question and sends the answer', async () => {
    renderWithProviders(<MoodMusicPage />)
    await flush()
    fireEvent.change(screen.getByLabelText(t('mood_label')), { target: { value: 'meh' } })
    fireEvent.click(screen.getByRole('button', { name: t('find') }))
    await flush()
    api.getSlot.mockResolvedValue(reply({ question: 'Quiet or loud?' }))
    await flush(POLL_MS)
    expect(screen.getByText('Quiet or loud?')).toBeInTheDocument()
    expect(api.setQueue).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText(t('answer_placeholder')), { target: { value: 'quiet' } })
    fireEvent.click(screen.getByRole('button', { name: t('send_answer') }))
    await flush()
    expect(api.ask.mock.calls[1][0]).toContain('Answer to your question: quiet')
  })

  it('says so when the reply has no readable block', async () => {
    renderWithProviders(<MoodMusicPage />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: t('mood_calm') }))
    fireEvent.click(screen.getByRole('button', { name: t('find') }))
    await flush()
    api.getSlot.mockResolvedValue({ running: false, messages: [{ role: 'assistant', content: 'no block' }] })
    await flush(POLL_MS)
    expect(screen.getByText(t('error_unreadable'))).toBeInTheDocument()
  })

  it('skips to the next pick and records it', async () => {
    const picks = [queued({ id: 'a'.repeat(32), title: 'One' }), queued({ id: 'b'.repeat(32), title: 'Two' })]
    api.state.mockResolvedValue({ ...EMPTY, queue: picks })
    api.feedback.mockResolvedValue({ ...picks[0], feedback: 'skipped' })
    renderWithProviders(<MoodMusicPage />)
    await flush()
    expect(screen.getByRole('heading', { name: 'One' })).toBeInTheDocument()
    api.state.mockResolvedValue({ ...EMPTY, queue: [{ ...picks[0], feedback: 'skipped' }, picks[1]] })
    fireEvent.click(screen.getByRole('button', { name: t('skip') }))
    await flush()
    expect(api.feedback).toHaveBeenCalledWith('a'.repeat(32), 'skipped')
    expect(screen.getByRole('heading', { name: 'Two' })).toBeInTheDocument()
    expect(screen.getByTestId('mood-youtube-frame').getAttribute('src')).toContain('autoplay=1')
  })

  it('lists the moments with their songs on the second tab', async () => {
    api.state.mockResolvedValue({ ...EMPTY, moments: [{ id: 'm'.repeat(32), mood: '下雨的傍晚', note: '', tags: ['想念'], songs: [{ title: '晴天', artist: '周杰伦' }], at: 1 }] })
    renderWithProviders(<MoodMusicPage />)
    await flush()
    const tab = screen.queryByRole('radio', { name: t('tab_moments') })
    if (!tab) fireEvent.click(screen.getByRole('button', { name: t('tab_listen') }))
    fireEvent.click(screen.getByRole('radio', { name: t('tab_moments') }))
    expect(screen.getByText('下雨的傍晚')).toBeInTheDocument()
    expect(screen.getByText('晴天 — 周杰伦')).toBeInTheDocument()
  })

  it('pauses the backdrop drift with its control', async () => {
    renderWithProviders(<MoodMusicPage />)
    await flush()
    expect(screen.getByTestId('mood-backdrop').dataset.moving).toBe('true')
    fireEvent.click(screen.getByTestId('mood-motion-toggle'))
    expect(screen.getByTestId('mood-backdrop').dataset.moving).toBe('false')
    expect(screen.getByTestId('mood-motion-toggle')).toHaveAttribute('aria-pressed', 'true')
  })

  it('holds the backdrop still under reduced motion, with no loop to pause', async () => {
    motion.reduced = true
    renderWithProviders(<MoodMusicPage />)
    await flush()
    expect(screen.getByTestId('mood-backdrop').dataset.moving).toBe('false')
    expect(screen.queryByTestId('mood-motion-toggle')).toBeNull()
  })

  it('paints only through theme variables, so a dark theme carries through', async () => {
    document.documentElement.classList.add('dark')
    try {
      const { container } = renderWithProviders(<MoodMusicPage />)
      await flush()
      const styles = [...container.querySelectorAll<HTMLElement>('[style]')].map(el => el.getAttribute('style') ?? '')
      expect(styles.join(' ')).not.toMatch(/#[0-9a-f]{3,8}\b|rgb\(/i)
      const glows = screen.getAllByTestId('mood-glow')
      expect(glows[0].className).toContain('var(--bg)')
      expect(glows[0].style.getPropertyValue('--mood-hue')).toBe('230')
    } finally {
      document.documentElement.classList.remove('dark')
    }
  })
})
