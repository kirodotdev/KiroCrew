import { describe, expect, it } from 'vitest'
import { parseDjReply, replySource } from './replyParse'

const ID = 'dQw4w9WgXcQ'
const block = (data: unknown) => 'Here you go.\n```mood-music\n' + JSON.stringify(data) + '\n```\n'

describe('parseDjReply', () => {
  it('reads picks, keeps a YouTube id only, and drops bad links', () => {
    const r = parseDjReply(block({
      mood_tags: ['想念'],
      picks: [
        { title: 'A', artist: 'x', source: 'youtube', url: `https://www.youtube.com/watch?v=${ID}`, why: 'soft' },
        { title: 'B', source: 'bilibili', url: 'https://www.bilibili.com/video/BV1xx411c7mD' },
        { title: 'C', source: 'youtube', url: `https://www.youtube.com/watch?v=${ID}Q` },
        { title: 'D', source: 'bilibili', url: 'https://bilibili.com.evil.com/x' },
        { title: 'E', source: 'bilibili', url: 'http://www.bilibili.com/video/BV1' },
        { title: '', source: 'youtube', video_id: ID },
      ],
    }))
    expect(r?.moodTags).toEqual(['想念'])
    expect(r?.picks).toEqual([
      { title: 'A', artist: 'x', why: 'soft', tags: [], source: 'youtube', video_id: ID },
      { title: 'B', artist: '', why: '', tags: [], source: 'bilibili', url: 'https://www.bilibili.com/video/BV1xx411c7mD' },
    ])
    expect(r?.dropped).toBe(4)
  })

  it('reads a question with no picks', () => {
    expect(parseDjReply(block({ question: 'Quiet or loud?' }))).toEqual({ question: 'Quiet or loud?', moodTags: [], picks: [], dropped: 0 })
  })

  it('uses the last block and returns null when none parses', () => {
    const two = block({ question: 'old' }) + block({ question: 'new' })
    expect(parseDjReply(two)?.question).toBe('new')
    expect(parseDjReply('no block here')).toBeNull()
    expect(parseDjReply('```mood-music\n{bad json\n```')).toBeNull()
    expect(parseDjReply(block([1, 2]))).toBeNull()
  })

  it('names a reply by its content', () => {
    expect(replySource('a')).toBe(replySource('a'))
    expect(replySource('a')).not.toBe(replySource('b'))
  })
})
