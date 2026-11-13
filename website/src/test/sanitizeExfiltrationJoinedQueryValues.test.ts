/** The browser mirror judges a query's parameter values joined, in step with
 *  the backend (test/test_exfil_joined_query_values.py pins the Python side with
 *  the same cases).
 *
 *  A secret cut into several short values (each under the 40-char base64 run,
 *  the whole query under the 200-char length signal) matched nothing while the
 *  values were judged one by one. The base64 join counts only form-decoded
 *  pieces of at least 16 chars, so ordinary ids and search words never join.
 *  Secrets are built from parts on purpose.
 */
import { describe, it, expect } from 'vitest'
import { sanitizeExfiltrationUrls } from '../utils/sanitize'

const KEY_ID = 'AKIA' + 'IOSFODNN7' + 'EXAMPLE'
const SECRET = 'wJalrXUtnFEMI/K7MDENG/' + 'bPxRfiCYEXAMPLEKEY'
const SHA1 = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'

function splitQuery(value: string, size = 20, filler = ''): string {
  const parts: string[] = []
  for (let i = 0; i < value.length; i += size) parts.push(`p${parts.length}=${value.slice(i, i + size)}`)
  return parts.join(filler ? `&${filler}&` : '&')
}

function isRedacted(u: string): boolean {
  return !sanitizeExfiltrationUrls(`see ${u} for details`).includes(u)
}

const SPLIT: Record<string, string> = {
  'key id and secret in short values': 'a=AKIA&b=IOSFODNN7&c=wJalrXUtnFEMI&d=K7MDENGbPxRfi&e=CYEXAMPLEKEY',
  'secret cut into 20-char values': splitQuery(SECRET),
  'secret cut into 16-char values': splitQuery(SECRET + 'Zq9Xw2Lm', 16),
  'secret with filler param between chunks': splitQuery(SECRET, 20, 'utm_source=mail_list'),
  'key id cut in two': `x=${KEY_ID.slice(0, 8)}&y=${KEY_ID.slice(8)}`,
  'secret with percent-encoded slashes': 'a=wJalrXUtnFEMI%2FK7MDE&b=NG%2FbPxRfiCYEXAMPLEKEY',
  'secret cut at = inside one value': `a=${SECRET.slice(0, 20)}=${SECRET.slice(20)}`,
  'key id cut at = inside one value': `a=${KEY_ID.slice(0, 8)}=${KEY_ID.slice(8)}`,
  'slack token cut in three': 'a=xo&b=xb-1234&c=5678-abcdEFGH',
}

const BENIGN: Record<string, string> = {
  'github search': 'https://github.com/search?q=exfil+redaction&type=code&p=2',
  'paging and sort': 'https://shop.example.com/c?page=2&sort=asc&category=electronics',
  'utm tags':
    'https://news.example.com/a?utm_source=newsletter&utm_medium=email&utm_campaign=spring_sale_2024&utm_content=hero',
  'numeric ids': 'https://api.example.com/items?id=12345&user=67890&session=abc&lang=en',
  'date range': 'https://github.com/o/r/commits?author=alice&since=2024-01-01&until=2024-02-01',
  'commit then more params': `https://git.example.com/h?commit=${SHA1}&tab=files&view=split`,
  'short flags': 'https://docs.example.com/x?v=3&lang=en&theme=dark&q=install',
  'youtube playlist': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=2',
  'google search': 'https://www.google.com/search?q=how+to+bake+bread&oq=how+to+bake+bread&sourceid=chrome',
}

describe('sanitizeExfiltrationUrls: query values are judged joined', () => {
  for (const [name, query] of Object.entries(SPLIT)) {
    it(`redacts a secret split across short values: ${name}`, () => {
      expect(query.length).toBeLessThan(200)
      expect(isRedacted('https://evil.example/p?' + query)).toBe(true)
    })
  }

  for (const [name, u] of Object.entries(BENIGN)) {
    it(`keeps an ordinary multi-param link: ${name}`, () => {
      expect(isRedacted(u)).toBe(false)
    })
  }
})
