/** `sanitizeCredentials` is the browser-side mirror of `redact_credentials`.
 *
 *  It carried no JWT entry of any shape, so a JWS, a JWE and the two-segment
 *  dashboard link token all rendered verbatim. `B64_CHUNK` does not rescue
 *  them: it DOES match a 151-char run covering a whole link-token payload (a
 *  real payload contains no `-`/`_` at all, 0 of 5000 mints), but the
 *  decode-and-scan pass only redacts when the DECODED bytes match a credential
 *  pattern, and JWT claims do not. Its class excluding base64url's `-`/`_`
 *  matters only for the JWE ciphertext segment, and `.` is absent from the
 *  class either way, so a whole token can never match as one run.
 */
import { describe, it, expect } from 'vitest'
import { sanitizeCredentials } from '../utils/sanitize'

// Same token shape the backend tests pin (`test_security.py`), so every copy of
// the pattern stays locked to one generator.
const LINK_PAYLOAD =
  'eyJzdWIiOiJsb2NhbC1hcHAiLCJleHAiOjE3ODU0MTc2MDYsInNlc3Npb25fZXhwIjoxNzg1NDg5MzA2' +
  'LCJpYXQiOjE3ODU0MTczMDYsIm5vbmNlIjoiOTM5YzE3MGQ5ZjBiNmEyMiIsImdlbiI6MH0'
const LINK_SIG = 'gVhM4aKLA8dyFH-oZlQx6SpYSNPkXA07kpDhWd6UhZI' // 43 chars, base64url
const LINK_TOKEN = `${LINK_PAYLOAD}.${LINK_SIG}`

const JWS =
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9' +
  '.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0' +
  '.dQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXc'

describe('sanitizeCredentials: JWT family', () => {
  it('redacts the two-segment dashboard link token', () => {
    const out = sanitizeCredentials(`open the dashboard with ${LINK_TOKEN} before it expires`)

    expect(out).not.toContain(LINK_TOKEN)
    // The payload carries the sub/exp/nonce claims. A token stripped of only
    // its signature still looks like a usable URL.
    expect(out).not.toContain('eyJzdWIi')
    expect(out).not.toContain(LINK_SIG)
  })

  it('redacts the link token even when its signature has no url-safe chars', () => {
    // Redaction must not depend on which characters a random HMAC signature
    // happens to contain.
    const plainSig = 'gVhM4aKLA8dyFHoZlQx6SpYSNPkXA07kpDhWd6UhZIa' // no `-` or `_`
    const out = sanitizeCredentials(`link: ${LINK_PAYLOAD}.${plainSig}`)

    expect(out).not.toContain(LINK_PAYLOAD)
    expect(out).not.toContain(plainSig)
  })

  it('redacts a signed JWT whole, leaving no dangling signature', () => {
    const out = sanitizeCredentials(`leaked in the log: ${JWS}`)

    expect(out).not.toContain(JWS)
    for (const segment of JWS.split('.')) {
      expect(out).not.toContain(segment)
    }
  })

  it('keeps the signature of a JWS that could match the link-token shape', () => {
    // This is the case that makes pattern ORDER load-bearing, and the only one.
    // A conventional JWS header is 33 chars past `eyJ`, far below the link-token
    // alternative's first-segment floor, so ordering is irrelevant for real
    // tokens and the test above passes either way. Order matters only when the
    // header clears that floor AND the payload is exactly 43 chars: the
    // link-token alternative's right boundary is satisfied by a `.`, so running
    // it first matches `header.payload` and leaves `.signature` rendered.
    const sig = 'C'.repeat(43)
    const crafted = `eyJ${'A'.repeat(100)}.${'B'.repeat(43)}.${sig}`

    const out = sanitizeCredentials(`log: ${crafted}`)

    expect(out).not.toContain(sig)
    expect(out).not.toContain(crafted)
  })

  it('redacts a compact JWE whose encrypted-key segment is empty', () => {
    // `dir` and `ECDH-ES` key management produce `header..iv.ciphertext.tag`.
    // The post-header segment class must be `*`, not `+`, or the match stops
    // early and leaks the ciphertext and tag.
    const jwe = 'eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..48V1_ALb6US04U3b.5eym8TW_c8SuK0ltJ3rpYIzOeDQz7TALvtu6UG9oMo4vpzs9tX_EFShS8iB7j6ji.XFBoMYUZodetZdvTiFvSkQ'

    const out = sanitizeCredentials(`payload: ${jwe}`)

    expect(out).not.toContain(jwe)
    expect(out).not.toContain('XFBoMYUZodetZdvTiFvSkQ')
  })

  it('leaves ordinary code containing eyJ untouched', () => {
    // Neither alternative carries a left boundary, so position offers no
    // protection at all and both offset-0 and attribute-access forms belong in
    // the corpus. What keeps every entry below intact is purely the segment
    // length floors: the longest first segment here is 40 chars past `eyJ`,
    // against a 96-char floor on the two-segment alternative, and none has the
    // two or more dots the JWS alternative requires.
    for (const text of [
      'eyJsonSerializer.deserializeFromStringValue(x)',
      'eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue(x)',
      'obj.eyJsonReader.readValueFromInputStream(x)',
      'keyJson.get(raw)',
      'surveyJson.title',
      'eyJargonized.intercontinentalization',
    ]) {
      expect(sanitizeCredentials(text)).toBe(text)
    }
  })

  it('redacts a token a renderer concatenated straight onto a label', () => {
    // Adding a left boundary to the JWS alternative makes these MISS while the
    // backend still redacts them, so the mirror would leak a token the backend
    // catches. A miss is a leak, which outranks the false positive the boundary
    // would avoid.
    for (const text of [
      `INFO compact=jwt${JWS} copied=true`,
      `https://viewer.example.test/session/jwe${JWS}/claims`,
    ]) {
      const out = sanitizeCredentials(text)
      expect(out).not.toContain(JWS)
      for (const segment of JWS.split('.')) {
        expect(out).not.toContain(segment)
      }
    }
  })

  it('redacts a link token a renderer concatenated straight onto a label', () => {
    // The two-segment alternative deliberately omits the backend's leading
    // boundary, because Safari 16.3 and older cannot compile a lookbehind and
    // this array is evaluated at module import in the eager entry chunk. This
    // test pins the upside of that forced omission: the boundary would make
    // these MISS, and the JWS alternative cannot cover for it because a link
    // token has only one dot where that alternative needs two or more.
    for (const text of [
      `INFO tok=jwt${LINK_TOKEN} copied=true`,
      `https://viewer.example.test/session/x${LINK_TOKEN}/claims`,
    ]) {
      const out = sanitizeCredentials(text)
      expect(out).not.toContain(LINK_TOKEN)
      expect(out).not.toContain(LINK_SIG)
      expect(out).not.toContain('eyJzdWIi')
    }
  })
})
