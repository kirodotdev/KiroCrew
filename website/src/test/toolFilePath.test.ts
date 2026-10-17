import { describe, it, expect } from 'vitest'
import { extractToolFilePath } from '../utils/toolFilePath'

describe('extractToolFilePath', () => {
  it('extracts operations[].path (multi-edit tools)', () => {
    expect(extractToolFilePath('{"operations":[{"path":"/x"}]}')).toBe('/x')
  })

  it('extracts files[].path (multi-file tools)', () => {
    expect(extractToolFilePath('{"files":[{"path":"/a/b.txt"}]}')).toBe('/a/b.txt')
  })

  it('extracts top-level path', () => {
    expect(extractToolFilePath('{"path":"/etc/hosts"}')).toBe('/etc/hosts')
  })

  it('extracts top-level file_path', () => {
    expect(extractToolFilePath('{"file_path":"src/app.ts"}')).toBe('src/app.ts')
  })

  it('extracts top-level filePath', () => {
    expect(extractToolFilePath('{"filePath":"/home/u/x.md"}')).toBe('/home/u/x.md')
  })

  it('prefers top-level path over operations[]', () => {
    expect(extractToolFilePath('{"path":"/top","operations":[{"path":"/nested"}]}')).toBe('/top')
  })

  it('returns null for bash-style args (no path field)', () => {
    expect(extractToolFilePath('{"command":"echo hi"}')).toBeNull()
  })

  it('returns null for http(s) URLs at top level', () => {
    expect(extractToolFilePath('{"path":"https://example.com/x"}')).toBeNull()
    expect(extractToolFilePath('{"file_path":"http://example.com"}')).toBeNull()
  })

  it('returns null for url-shaped inputs array', () => {
    expect(extractToolFilePath('{"inputs":["https://example.com"]}')).toBeNull()
  })

  it('returns null for empty / whitespace path', () => {
    expect(extractToolFilePath('{"path":""}')).toBeNull()
    expect(extractToolFilePath('{"path":"   "}')).toBeNull()
  })

  it('returns null for malformed JSON', () => {
    expect(extractToolFilePath('not json')).toBeNull()
    expect(extractToolFilePath('')).toBeNull()
  })

  it('returns null for non-object JSON', () => {
    expect(extractToolFilePath('"just a string"')).toBeNull()
    expect(extractToolFilePath('42')).toBeNull()
    expect(extractToolFilePath('null')).toBeNull()
  })

  it('skips non-string / url operations entries and finds the first fs path', () => {
    expect(extractToolFilePath('{"operations":[{"path":"https://x"},{"path":"/real"}]}')).toBe('/real')
  })
})
