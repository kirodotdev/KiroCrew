import { describe, it, expect } from 'vitest'
import { buildFileLabels, resolveFileSegment } from '../utils/fileTokens'

// Sent-message attachment cards and inline chips take their labels from
// buildFileLabels on the raw paths a Windows gateway reports.
describe('buildFileLabels on Windows-shaped paths', () => {
  it('labels a drive-rooted path by its file name', () => {
    const p = 'C:\\Users\\me\\Documents\\report.docx'
    expect(buildFileLabels([p]).get(p)).toBe('report.docx')
  })

  it('labels a UNC path by its file name', () => {
    const p = '\\\\host\\share\\notes.md'
    expect(buildFileLabels([p]).get(p)).toBe('notes.md')
  })

  it('splits a drive path that mixes both separators', () => {
    const p = 'C:/repo\\src\\a.ts'
    expect(buildFileLabels([p]).get(p)).toBe('a.ts')
  })

  it('tells same-named Windows files apart by their parent folder', () => {
    const a = 'C:\\work\\q3\\report.docx'
    const b = 'C:\\work\\q4\\report.docx'
    const m = buildFileLabels([a, b])
    expect(m.get(a)).toBe('q3/report.docx')
    expect(m.get(b)).toBe('q4/report.docx')
  })

  it('widens past two segments when the last two also collide', () => {
    const a = 'D:\\a\\x\\report.docx'
    const b = 'D:\\b\\x\\report.docx'
    const m = buildFileLabels([a, b])
    expect(m.get(a)).toBe('a/x/report.docx')
    expect(m.get(b)).toBe('b/x/report.docx')
  })

  it('keeps the POSIX rules: `\\` is part of a file name there', () => {
    const odd = '/tmp/we\\ird.md'
    const m = buildFileLabels([odd, '/repo/q3/report.docx', '/repo/q4/report.docx'])
    expect(m.get(odd)).toBe('we\\ird.md')
    expect(m.get('/repo/q3/report.docx')).toBe('q3/report.docx')
    expect(m.get('/repo/q4/report.docx')).toBe('q4/report.docx')
  })
})

describe('resolveFileSegment on Windows-shaped paths', () => {
  it('labels an inline chip by the file name and keeps the full path as its target', () => {
    const p = 'C:\\Users\\me\\report.docx'
    const r = resolveFileSegment(`see [attached_file 1] ${p} please`, [p])
    expect(r.display).toBe('see @report.docx please')
    expect(r.mentionMap.get('report.docx')).toBe(p)
  })

  it('labels a chip by the file name when the path is missing from the ordered list', () => {
    const p = 'C:\\Users\\me\\report.docx'
    const r = resolveFileSegment(`see [attached_file 1] ${p} please`, [])
    expect(r.display).toBe('see @report.docx please')
    expect(r.mentionMap.get('report.docx')).toBe(p)
  })

  it('tells two same-named Windows attachments apart in one message', () => {
    const a = 'C:\\q3\\report.docx'
    const b = 'C:\\q4\\report.docx'
    const r = resolveFileSegment(`compare [attached_file 1] ${a} with [attached_file 2] ${b}`, [a, b])
    expect(r.display).toBe('compare @q3/report.docx with @q4/report.docx')
    expect(r.mentionMap.get('q3/report.docx')).toBe(a)
    expect(r.mentionMap.get('q4/report.docx')).toBe(b)
  })

  it('keeps a marker chip on its own file when a same-named file is listed but not referenced', () => {
    const marker = 'C:\\q4\\report.docx'
    const listed = 'C:/q3/report.docx'
    const r = resolveFileSegment(`see [attached_file 1] ${marker} please`, [listed])
    expect(r.display).toBe('see @report.docx please')
    expect(r.mentionMap.get('report.docx')).toBe(marker)
  })

  it.each([
    ['one path listed', ['C:\\q3\\report.docx']],
    ['no path listed', []],
  ])('keeps same-named chips apart when a path is missing from the list (%s)', (_name, ordered) => {
    const a = 'C:\\q3\\report.docx'
    const b = 'C:\\q4\\report.docx'
    const r = resolveFileSegment(`compare [attached_file 1] ${a} with [attached_file 2] ${b}`, ordered)
    expect(r.mentionMap.size).toBe(2)
    expect(new Set(r.mentionMap.values())).toEqual(new Set([a, b]))
    expect(r.mentionMap.get('report.docx')).toBe(a)
    expect(r.mentionMap.get(b)).toBe(b)
  })
})
