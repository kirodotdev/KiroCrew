import { describe, it, expect } from 'vitest'
import { File, FileCode, FileJson, FileText, Image, Paintbrush, Settings, Terminal } from 'lucide-react'
import { FILE_COLORS, fileIcon, colorForExt } from '../utils/fileIcons'

describe('fileIcon', () => {
  it('returns FileCode for code extensions', () => {
    expect(fileIcon('foo.ts')).toBe(FileCode)
    expect(fileIcon('foo.tsx')).toBe(FileCode)
    expect(fileIcon('foo.js')).toBe(FileCode)
    expect(fileIcon('foo.py')).toBe(FileCode)
    expect(fileIcon('foo.rs')).toBe(FileCode)
    expect(fileIcon('foo.go')).toBe(FileCode)
    expect(fileIcon('foo.java')).toBe(FileCode)
    expect(fileIcon('foo.kt')).toBe(FileCode)
    expect(fileIcon('foo.cpp')).toBe(FileCode)
  })

  it('returns FileJson only for json (not for yaml)', () => {
    expect(fileIcon('config.json')).toBe(FileJson)
    expect(fileIcon('config.yaml')).toBe(Settings)
    expect(fileIcon('config.yml')).toBe(Settings)
    expect(fileIcon('config.toml')).toBe(Settings)
  })

  it('returns FileText for docs', () => {
    expect(fileIcon('README.md')).toBe(FileText)
    expect(fileIcon('CHANGELOG.mdx')).toBe(FileText)
    expect(fileIcon('notes.txt')).toBe(FileText)
    expect(fileIcon('rows.csv')).toBe(FileText)
    expect(fileIcon('out.log')).toBe(FileText)
  })

  it('returns Paintbrush for stylesheets', () => {
    expect(fileIcon('main.css')).toBe(Paintbrush)
    expect(fileIcon('vars.scss')).toBe(Paintbrush)
    expect(fileIcon('vars.less')).toBe(Paintbrush)
  })

  it('returns Image for image extensions', () => {
    expect(fileIcon('logo.png')).toBe(Image)
    expect(fileIcon('photo.jpg')).toBe(Image)
    expect(fileIcon('photo.jpeg')).toBe(Image)
    expect(fileIcon('anim.gif')).toBe(Image)
    expect(fileIcon('icon.svg')).toBe(Image)
    expect(fileIcon('photo.webp')).toBe(Image)
  })

  it('returns Terminal for shell scripts', () => {
    expect(fileIcon('run.sh')).toBe(Terminal)
    expect(fileIcon('run.bash')).toBe(Terminal)
    expect(fileIcon('run.zsh')).toBe(Terminal)
  })

  it('falls back to generic File for unknown extensions', () => {
    expect(fileIcon('mystery.xyz')).toBe(File)
    expect(fileIcon('binary.bin')).toBe(File)
  })

  it('handles paths with no extension', () => {
    expect(fileIcon('Makefile')).toBe(File)
    expect(fileIcon('LICENSE')).toBe(File)
    expect(fileIcon('')).toBe(File)
  })

  it('is case-insensitive', () => {
    expect(fileIcon('Foo.TS')).toBe(FileCode)
    expect(fileIcon('IMAGE.PNG')).toBe(Image)
  })

  it('handles full paths', () => {
    expect(fileIcon('/abs/path/to/file.ts')).toBe(FileCode)
    expect(fileIcon('relative/path/to/file.css')).toBe(Paintbrush)
  })
})

describe('colorForExt', () => {
  it('returns blue for typescript', () => {
    expect(colorForExt('foo.ts')).toBe('text-blue-400')
    expect(colorForExt('foo.tsx')).toBe('text-blue-400')
  })

  it('returns yellow for javascript', () => {
    expect(colorForExt('foo.js')).toBe('text-yellow-400')
    expect(colorForExt('foo.jsx')).toBe('text-yellow-400')
  })

  it('returns green for python and shell', () => {
    expect(colorForExt('foo.py')).toBe('text-green-500')
    expect(colorForExt('run.sh')).toBe('text-green-400')
    expect(colorForExt('run.bash')).toBe('text-green-400')
  })

  it('returns orange for rust and html', () => {
    expect(colorForExt('foo.rs')).toBe('text-orange-500')
    expect(colorForExt('foo.html')).toBe('text-orange-400')
  })

  it('returns muted for unknown and plain text', () => {
    expect(colorForExt('mystery.xyz')).toBe('text-muted')
    expect(colorForExt('notes.txt')).toBe('text-muted')
    expect(colorForExt('out.log')).toBe('text-muted')
  })

  it('returns a defined color for every key in FILE_ICONS that also has a color entry', () => {
    // Sanity check: any extension we color must produce a non-empty class string.
    for (const cls of Object.values(FILE_COLORS)) {
      expect(cls).toMatch(/^text-/)
    }
  })

  it('handles empty paths and bare names', () => {
    expect(colorForExt('')).toBe('text-muted')
    expect(colorForExt('Makefile')).toBe('text-muted')
  })

  it('is case-insensitive', () => {
    expect(colorForExt('Foo.TS')).toBe('text-blue-400')
    expect(colorForExt('Foo.PY')).toBe('text-green-500')
  })
})
