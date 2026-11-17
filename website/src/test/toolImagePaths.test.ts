import { describe, it, expect } from 'vitest'
import { extractToolImagePaths, MAX_TOOL_IMAGE_THUMBNAILS } from '../utils/toolImagePaths'

const op = (paths: unknown, mode: unknown = 'Image') => JSON.stringify({ operations: [{ mode, image_paths: paths }] })

describe('extractToolImagePaths', () => {
  it('reads image_paths from an image-mode read operation, in order', () => {
    expect(extractToolImagePaths(op(['/a/one.png', '/b/two.jpg']))).toEqual(['/a/one.png', '/b/two.jpg'])
  })

  it('ignores an image operation outside operations[]', () => {
    expect(extractToolImagePaths(JSON.stringify({ mode: 'Image', image_paths: ['/x.png'] }))).toEqual([])
  })

  it('matches the mode case-insensitively', () => {
    expect(extractToolImagePaths(op(['/x.png'], 'image'))).toEqual(['/x.png'])
  })

  it('collects across several image operations and skips other modes', () => {
    const input = JSON.stringify({ operations: [
      { mode: 'Line', path: '/notes.md' },
      { mode: 'Image', image_paths: ['/a.png'] },
      { mode: 'Image', image_paths: ['/b.png', '/a.png'] },
    ] })
    expect(extractToolImagePaths(input)).toEqual(['/a.png', '/b.png'])
  })

  it('returns nothing for a non-image read or another tool', () => {
    expect(extractToolImagePaths(JSON.stringify({ operations: [{ mode: 'Line', path: '/a.png' }] }))).toEqual([])
    expect(extractToolImagePaths(JSON.stringify({ command: 'ls' }))).toEqual([])
    expect(extractToolImagePaths(JSON.stringify({ operations: [{ image_paths: ['/a.png'] }] }))).toEqual([])
  })

  it('returns nothing for empty or invalid input', () => {
    expect(extractToolImagePaths('')).toEqual([])
    expect(extractToolImagePaths('{not json')).toEqual([])
    expect(extractToolImagePaths('null')).toEqual([])
    expect(extractToolImagePaths(op('/a.png'))).toEqual([])
  })

  it('keeps Windows drive and UNC paths', () => {
    expect(extractToolImagePaths(op(['C:\\Users\\me\\shot.png', 'D:/pics/a.png', '\\\\host\\share\\b.png'])))
      .toEqual(['C:\\Users\\me\\shot.png', 'D:/pics/a.png', '\\\\host\\share\\b.png'])
  })

  it('drops relative paths, URLs, blanks and non-strings', () => {
    expect(extractToolImagePaths(op(['shots/a.png', './b.png', 'https://example.com/c.png', '  ', 7, null, '/ok.png'])))
      .toEqual(['/ok.png'])
  })

  it('drops traversal and credential-directory paths', () => {
    expect(extractToolImagePaths(op(['/home/me/../etc/x.png', '/home/me/.ssh/key.png', '/home/me/.aws/a.png', '/fine.png'])))
      .toEqual(['/fine.png'])
  })

  it('trims surrounding whitespace', () => {
    expect(extractToolImagePaths(op(['  /a.png  ']))).toEqual(['/a.png'])
  })

  it('caps the number of paths', () => {
    const many = Array.from({ length: MAX_TOOL_IMAGE_THUMBNAILS + 5 }, (_, i) => `/p${i}.png`)
    const got = extractToolImagePaths(op(many))
    expect(got).toHaveLength(MAX_TOOL_IMAGE_THUMBNAILS)
    expect(got[0]).toBe('/p0.png')
  })
})

/** Claude Code's Read, as claude-agent-acp's refinement sends it
 *  (test/fixtures/acp_frames/claude/read-image.jsonl). */
const claudeRead = (args: Record<string, unknown>) => JSON.stringify(args, null, 2)

describe('extractToolImagePaths on a row the backend marks with read_path_arg', () => {
  it('reads the image a Claude Code Read names in file_path', () => {
    expect(extractToolImagePaths(claudeRead({ file_path: '/work/website/public/icon-192.png' }), 'file_path'))
      .toEqual(['/work/website/public/icon-192.png'])
  })

  it('keeps every image extension the viewer serves, in any case', () => {
    for (const ext of ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'PNG', 'JPeG']) {
      expect(extractToolImagePaths(claudeRead({ file_path: `/a/pic.${ext}` }), 'file_path')).toEqual([`/a/pic.${ext}`])
    }
  })

  it('ignores the offset and limit a Read may carry', () => {
    expect(extractToolImagePaths(claudeRead({ file_path: '/a/pic.png', offset: 1, limit: 10 }), 'file_path'))
      .toEqual(['/a/pic.png'])
  })

  it('reads nothing from file_path without the mark: Write and Edit carry it too', () => {
    expect(extractToolImagePaths(claudeRead({ file_path: '/a/pic.png' }))).toEqual([])
    expect(extractToolImagePaths(claudeRead({ file_path: '/a/pic.png', content: '<svg/>' }))).toEqual([])
  })

  it('reads only the argument the mark names', () => {
    expect(extractToolImagePaths(claudeRead({ path: '/a/pic.png' }), 'file_path')).toEqual([])
    expect(extractToolImagePaths(claudeRead({ file_path: '/a/pic.png' }), 'path')).toEqual([])
  })

  it('skips a file whose name is not an image', () => {
    for (const p of ['/a/notes.md', '/a/doc.pdf', '/a/pic.png.txt', '/a/png', '/a/.png.bak']) {
      expect(extractToolImagePaths(claudeRead({ file_path: p }), 'file_path')).toEqual([])
    }
  })

  it('applies the same path refusals as an image-mode read', () => {
    for (const p of ['pics/a.png', './a.png', 'https://example.com/a.png', '/home/me/.ssh/key.png', '/home/me/../etc/a.png', '  ', 7, null, ['/a.png']]) {
      expect(extractToolImagePaths(claudeRead({ file_path: p }), 'file_path')).toEqual([])
    }
  })

  it('keeps Windows drive and UNC paths and trims whitespace', () => {
    expect(extractToolImagePaths(claudeRead({ file_path: 'C:\\Users\\me\\shot.png' }), 'file_path')).toEqual(['C:\\Users\\me\\shot.png'])
    expect(extractToolImagePaths(claudeRead({ file_path: '\\\\host\\share\\b.png' }), 'file_path')).toEqual(['\\\\host\\share\\b.png'])
    expect(extractToolImagePaths(claudeRead({ file_path: '  /a.png  ' }), 'file_path')).toEqual(['/a.png'])
  })

  it('still reads an image-mode operation list on a marked row, without a duplicate', () => {
    const input = JSON.stringify({ file_path: '/a.png', operations: [{ mode: 'Image', image_paths: ['/a.png', '/b.png'] }] })
    expect(extractToolImagePaths(input, 'file_path')).toEqual(['/a.png', '/b.png'])
  })

  it('returns nothing for invalid input', () => {
    expect(extractToolImagePaths('', 'file_path')).toEqual([])
    expect(extractToolImagePaths('{"file_path": "/a.png"', 'file_path')).toEqual([])
    expect(extractToolImagePaths('"/a.png"', 'file_path')).toEqual([])
  })
})
