import { describe, expect, it } from 'vitest'
import { isAndroidWebView } from './download'

describe('isAndroidWebView', () => {
  it('is true only for an Android WebView user agent', () => {
    expect(isAndroidWebView(
      'Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0 Mobile Safari/537.36',
    )).toBe(true)
    // Android Chrome itself saves blob downloads and keeps that path.
    expect(isAndroidWebView(
      'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36',
    )).toBe(false)
    expect(isAndroidWebView(
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
    )).toBe(false)
    expect(isAndroidWebView('')).toBe(false)
  })
})
