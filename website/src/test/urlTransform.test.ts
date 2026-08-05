import { describe, it, expect } from 'vitest'
import { urlTransform } from '../utils/urlTransform'

describe('urlTransform', () => {
  it('allows vscode remote SSH URL', () => {
    const url = 'vscode://vscode-remote/ssh-remote+dev-host.example.com/home/user/workspace/KiroCrew'
    expect(urlTransform(url)).toBe(url)
  })

  it('allows vscode file URL', () => {
    const url = 'vscode://file/home/user/project'
    expect(urlTransform(url)).toBe(url)
  })

  it('allows vscode-insiders:// URL', () => {
    const url = 'vscode-insiders://vscode-remote/ssh-remote+host/path'
    expect(urlTransform(url)).toBe(url)
  })

  it('preserves vscode URL with query params', () => {
    const url = 'vscode://vscode-remote/ssh-remote+host/path?windowId=1'
    expect(urlTransform(url)).toBe(url)
  })

  it('rejects bare vscode://', () => {
    expect(urlTransform('vscode://')).toBe('')
  })

  it('rejects bare vscode-insiders://', () => {
    expect(urlTransform('vscode-insiders://')).toBe('')
  })

  it('falls back to default for malformed URL', () => {
    expect(urlTransform('vscode://[invalid')).toBe('')
  })

  it('passes http URLs through default sanitizer', () => {
    expect(urlTransform('https://example.com')).toBe('https://example.com')
  })

  it('strips javascript: URLs', () => {
    expect(urlTransform('javascript:alert(1)')).toBe('')
  })
})
