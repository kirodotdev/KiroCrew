const IMAGE_MIME_EXT: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/bmp': 'bmp',
  'image/svg+xml': 'svg',
}

export function nameClipboardImage(file: File, batchIndex: number): File {
  const ext = IMAGE_MIME_EXT[file.type]
  if (!ext) return file
  const generic = !file.name || file.name === `image.${ext}` || file.name === 'image.png'
  if (!generic) return file
  const date = new Date()
  const pad = (value: number, width = 2) => String(value).padStart(width, '0')
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}${pad(date.getMilliseconds(), 3)}`
  const suffix = batchIndex > 0 ? `-${batchIndex + 1}` : ''
  return new File([file], `pasted-image-${stamp}${suffix}.${ext}`, {
    type: file.type,
    lastModified: file.lastModified,
  })
}

export function clipboardFiles(data: DataTransfer): File[] {
  let renamedCount = 0
  return Array.from(data.items || [])
    .filter(item => item.kind === 'file')
    .map(item => item.getAsFile())
    .filter((file): file is File => file !== null)
    .map(file => {
      const named = nameClipboardImage(file, renamedCount)
      if (named !== file) renamedCount += 1
      return named
    })
}

export function hasPlainClipboardText(data: DataTransfer): boolean {
  return Array.from(data.types || []).includes('text/plain')
}

export function stripTrailingBlankLines(value: string): string {
  let index = value.length - 1
  let sawNewline = false
  while (index >= 0) {
    const code = value.charCodeAt(index)
    if (code === 10 || code === 13) {
      sawNewline = true
      index -= 1
      continue
    }
    if (code === 32 || code === 9) {
      index -= 1
      continue
    }
    break
  }
  return sawNewline ? value.slice(0, index + 1) : value
}
