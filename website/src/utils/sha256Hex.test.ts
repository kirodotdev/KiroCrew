import { describe, expect, it } from 'vitest'

import { sha256Hex } from './sha256Hex'

describe('sha256Hex', () => {
  it('matches the FIPS 180-4 vectors', () => {
    expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  })

  it('hashes UTF-8 bytes, so non-ASCII input is not mangled', () => {
    // "héllo" is 6 bytes in UTF-8; hashing UTF-16 units would give a different digest.
    expect(sha256Hex('héllo')).toBe('3c48591d8d098a4538f5e013dfcf406e948eac4d3277b10bf614e295d6068179')
  })

  it('crosses the block boundaries correctly', () => {
    // 56 bytes: the length no longer fits the first block and spills into a second.
    expect(sha256Hex('a'.repeat(56))).toBe('b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a')
    // 64 bytes: an exact block, padding is a whole extra block.
    expect(sha256Hex('a'.repeat(64))).toBe('ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb')
    // 200 bytes: several blocks.
    expect(sha256Hex('a'.repeat(200))).toBe('c2a908d98f5df987ade41b5fce213067efbcc21ef2240212a41e54b5e7c28ae5')
  })
})
