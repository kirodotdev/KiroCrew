import { describe, expect, it } from 'vitest'
import { bilibiliUrl, validVideoId, youtubeEmbedUrl, youtubeVideoId } from './links'

const ID = 'dQw4w9WgXcQ'

describe('mood-music links', () => {
  it('accepts an 11-char id and nothing else', () => {
    expect(validVideoId(ID)).toBe(ID)
    for (const bad of [ID + 'Q', ID.slice(0, 10), 'dQw4w9WgXc!', '', null, 12345678901, `${ID}\n`]) {
      expect(validVideoId(bad)).toBeNull()
    }
  })

  it('reads the id from watch and share links only', () => {
    for (const url of [`https://www.youtube.com/watch?v=${ID}`, `https://m.youtube.com/watch?v=${ID}&t=3`, `https://youtu.be/${ID}`]) {
      expect(youtubeVideoId(url)).toBe(ID)
    }
    for (const url of [
      `http://www.youtube.com/watch?v=${ID}`,
      `https://www.youtube.com/watch?v=${ID}Q`,
      `https://www.youtube.com/embed/${ID}`,
      `https://www.youtube.com.evil.com/watch?v=${ID}`,
      `https://www.youtube.com:8443/watch?v=${ID}`,
      `https://www.youtube.com\\@evil.com/watch?v=${ID}`,
    ]) {
      expect(youtubeVideoId(url)).toBeNull()
    }
  })

  it('builds the embed URL only on the nocookie host, from a checked id', () => {
    expect(youtubeEmbedUrl(ID, false)).toBe(`https://www.youtube-nocookie.com/embed/${ID}?rel=0`)
    expect(youtubeEmbedUrl(ID, true)).toBe(`https://www.youtube-nocookie.com/embed/${ID}?rel=0&autoplay=1`)
    expect(youtubeEmbedUrl(`${ID}/../x`, true)).toBeNull()
    expect(youtubeEmbedUrl(ID + 'Q', true)).toBeNull()
  })

  it('keeps an https bilibili link on an exact host and refuses the rest', () => {
    expect(bilibiliUrl('https://www.bilibili.com/video/BV1xx411c7mD?p=2#t')).toBe('https://www.bilibili.com/video/BV1xx411c7mD?p=2')
    expect(bilibiliUrl('https://B23.TV/abc')).toBe('https://b23.tv/abc')
    for (const url of [
      'http://www.bilibili.com/video/BV1',
      'https://bilibili.com.evil.com/video/BV1',
      'https://www.bilibili.com.evil.com/',
      'https://evil.com/?www.bilibili.com',
      'https://space.bilibili.com/1',
      'https://u:p@www.bilibili.com/video/BV1',
      'https://www.bilibili.com:444/video/BV1',
      'https://www.bilibili.com./video/BV1',
      'javascript:alert(1)',
      '',
    ]) {
      expect(bilibiliUrl(url)).toBeNull()
    }
  })
})
