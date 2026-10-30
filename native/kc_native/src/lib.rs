//! Native fast path for `kiro_crew.security.redaction_patterns._might_contain_credential`.
//!
//! The Python gate runs 21 substring searches plus 6 regex searches over the same
//! text, one after another. This does the same test in ONE linear-time scan with
//! the `regex` crate, and releases the GIL while it scans.
//!
//! EXACTNESS: the answer must equal the Python gate's for every string. The parts
//! where the two regex engines differ are spelled out by hand:
//!   * Python `\s` (str patterns) is `str.isspace()`: 29 code points, listed in
//!     `PY_WS_ASCII` plus `PY_WS_WIDE` (Rust's `\s` is Unicode White_Space, which
//!     lacks U+001C..U+001F).
//!   * Python `(?i:Authorization)` also folds U+0130 / U+0131 onto `i`, which
//!     Unicode simple case folding does not; each letter is an explicit class.
//!
//! The Python side enables this module only when `GATE_FINGERPRINT` equals the
//! hash of its own gate definition, so editing the Python gate without updating
//! this file silently falls back to Python instead of drifting.
//!
//! MEMORY: the text is read from the str object's own storage. It is never
//! converted to UTF-8, because `PyString::to_str` (PyUnicode_AsUTF8AndSize)
//! keeps that UTF-8 copy INSIDE the str for the rest of its life: every non-ASCII
//! transcript row the gateway holds would carry a second buffer as large as the
//! text. Instead the scan runs over bytes, one byte per code point:
//!   * a 1-byte (latin-1) str is scanned in place; byte == code point;
//!   * a 2- or 4-byte str is mapped into a scratch buffer. Every non-ASCII code
//!     point becomes one of three bytes that are never ASCII: WIDE_WS for a
//!     Python whitespace code point, WIDE_I for U+0130 / U+0131, WIDE_OTHER for
//!     the rest. That is exact because the gate gives non-ASCII code points
//!     exactly those three roles: inside `\s`, inside the `i` classes, or
//!     "matches a negated class and nothing else". Counted repeats (`{6,}`,
//!     `{30}`, `{22,30}`) only span ASCII classes, so one byte per code point
//!     does not change any count that matters.

use pyo3::exceptions::PyUnicodeEncodeError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::PyString;
use regex::bytes::Regex;
use std::cell::RefCell;
use std::sync::OnceLock;

/// sha256(repr(gate spec))[:16], computed by `redaction_patterns._native_gate_fingerprint()`.
const GATE_FINGERPRINT: &str = "e5abd59852daa8cf";

const LITERALS: &[&str] = &[
    "AKIA",
    "ASIA",
    "AccessKey",
    "aws_secret_access_key",
    "aws_session_token",
    "aws_access_key_id",
    "SessionToken",
    "PRIVATE KEY-----",
    "xox",
    "github_pat_",
    "glpat-",
    "k_live_",
    "k_test_",
    "SG.",
    "sk-proj-",
    "sk-ant-",
    "npm_",
    "pypi-",
    "_v1_",
    "GOCSPX-",
    "eyJ",
];

/// Python `re` `\s` for str patterns (== str.isspace()), Unicode 15.0: the ASCII
/// members as a byte class body, and the members above U+007F (`map_wide` must
/// send each of these to WIDE_WS; a test holds it to this list).
const PY_WS_ASCII: &str = r"\x09-\x0D\x1C-\x20";
#[cfg(test)]
const PY_WS_WIDE: &[u32] = &[
    0x85, 0xA0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008,
    0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
];

/// Bytes a mapped 2-/4-byte str uses for its non-ASCII code points.
const WIDE_WS: u8 = 0x80;
const WIDE_I: u8 = 0x81;
const WIDE_OTHER: u8 = 0x82;

/// Mapped strings above this many code points use a buffer freed on return, so
/// no thread keeps a scratch buffer as large as the biggest string it ever saw.
const SCRATCH_KEEP_MAX: usize = 1 << 20;

/// The gate over bytes. `ws_extra` / `i_extra` are the byte-class members that
/// stand for the non-ASCII whitespace and the dotted / dotless i.
fn build(ws_extra: &str, i_extra: &str) -> Regex {
    let mut parts: Vec<String> = LITERALS.iter().map(|l| regex::escape(l)).collect();
    // The Python gate spells the Telegram, Discord and Authorization anchors as
    // literal-led look-behinds for `re`'s speed; each accepts exactly the plain
    // spelling used here, which this crate's `regex` scans in one pass anyway.
    parts.push(r"gh[opsur]_".to_string());
    parts.push(r"[0-9]{6,}:[A-Za-z0-9_\-]{30}".to_string());
    parts.push(r"[MNO][A-Za-z0-9_\-]{22,30}\.".to_string());
    let ws = format!("{PY_WS_ASCII}{ws_extra}");
    parts.push(format!(r"://[^{ws}:/@]*:[^{ws}/]+@"));
    // (?i:Authorization) exactly as Python's sre folds it.
    let i = format!("[Ii{i_extra}]");
    parts.push(format!(
        "[Aa][Uu][Tt][Hh][Oo][Rr]{i}[Zz][Aa][Tt]{i}[Oo][Nn]"
    ));
    // (?-u): match bytes, so a class can name a byte >= 0x80 and a negated class
    // matches any byte not listed.
    Regex::new(&format!("(?-u:{})", parts.join("|"))).expect("gate regex compiles")
}

/// For a latin-1 str scanned in place: U+0085 and U+00A0 are whitespace, and
/// U+0130 / U+0131 cannot occur.
fn gate_latin1() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| build(r"\x85\xA0", ""))
}

/// For a 2-/4-byte str mapped through `map_wide`.
fn gate_wide() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| build(r"\x80", r"\x81"))
}

#[inline(always)]
fn map_wide(cp: u32) -> u8 {
    match cp {
        0..=0x7F => cp as u8,
        0x85 | 0xA0 | 0x1680 | 0x2000..=0x200A | 0x2028 | 0x2029 | 0x202F | 0x205F | 0x3000 => {
            WIDE_WS
        }
        0x130 | 0x131 => WIDE_I,
        _ => WIDE_OTHER,
    }
}

/// Marks a lone surrogate in `bmp_table`; never stored in a mapped buffer.
const SURROGATE: u8 = 0xFF;

/// `map_wide` for every BMP code point, with SURROGATE for U+D800..U+DFFF. One
/// load per code point instead of a chain of range tests.
fn bmp_table() -> &'static [u8; 0x10000] {
    static T: OnceLock<Box<[u8; 0x10000]>> = OnceLock::new();
    T.get_or_init(|| {
        let mut t = Box::new([0u8; 0x10000]);
        for (cp, slot) in t.iter_mut().enumerate() {
            let cp = cp as u32;
            *slot = if (0xD800..=0xDFFF).contains(&cp) {
                SURROGATE
            } else {
                map_wide(cp)
            };
        }
        t
    })
}

/// Map a 2-/4-byte str's code points into `out`. Returns false on a lone
/// surrogate, which the UTF-8 path this replaces could not encode.
fn fill_wide<T: Copy + Into<u32>>(units: &[T], out: &mut Vec<u8>) -> bool {
    let table = bmp_table();
    out.clear();
    let mut surrogate = false;
    out.extend(units.iter().map(|&u| {
        let cp: u32 = u.into();
        let b = if cp < 0x10000 {
            table[cp as usize]
        } else {
            WIDE_OTHER
        };
        surrogate |= b == SURROGATE;
        b
    }));
    !surrogate
}

thread_local! {
    static SCRATCH: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

enum Raw<'a> {
    Latin1(&'a [u8]),
    Ucs2(&'a [u16]),
    Ucs4(&'a [u32]),
}

/// Scan a 2-/4-byte str. None means it holds a lone surrogate.
fn scan_wide(raw: &Raw<'_>) -> Option<bool> {
    let run = |buf: &mut Vec<u8>| -> Option<bool> {
        let ok = match raw {
            Raw::Ucs2(u) => fill_wide(u, buf),
            Raw::Ucs4(u) => fill_wide(u, buf),
            Raw::Latin1(_) => unreachable!("latin-1 is scanned in place"),
        };
        ok.then(|| gate_wide().is_match(buf))
    };
    let len = match raw {
        Raw::Ucs2(u) => u.len(),
        Raw::Ucs4(u) => u.len(),
        Raw::Latin1(b) => b.len(),
    };
    if len > SCRATCH_KEEP_MAX {
        return run(&mut Vec::new());
    }
    SCRATCH.with(|cell| match cell.try_borrow_mut() {
        Ok(mut buf) => run(&mut buf),
        Err(_) => run(&mut Vec::new()),
    })
}

fn scan(raw: &Raw<'_>) -> Option<bool> {
    match raw {
        Raw::Latin1(b) => Some(gate_latin1().is_match(b)),
        _ => scan_wide(raw),
    }
}

/// Exactly `_might_contain_credential(text)`. Raises UnicodeEncodeError for a
/// str holding lone surrogates; the Python caller falls back on that.
#[pyfunction]
fn might_contain_credential(py: Python<'_>, text: &Bound<'_, PyString>) -> PyResult<bool> {
    let op = text.as_ptr();
    // SAFETY: `text` is a live, exact-or-subclass str held for this whole call, and
    // a str is immutable, so its storage stays valid and unchanged while it is read
    // here -- also with the GIL released below. On 3.12+ every str is "ready"
    // (canonical kind + data); the kind decides the element width.
    let raw = unsafe {
        let len = ffi::PyUnicode_GET_LENGTH(op) as usize;
        let data = ffi::PyUnicode_DATA(op);
        match ffi::PyUnicode_KIND(op) {
            ffi::PyUnicode_1BYTE_KIND => {
                Raw::Latin1(std::slice::from_raw_parts(data as *const u8, len))
            }
            ffi::PyUnicode_2BYTE_KIND => {
                Raw::Ucs2(std::slice::from_raw_parts(data as *const u16, len))
            }
            _ => Raw::Ucs4(std::slice::from_raw_parts(data as *const u32, len)),
        }
    };
    let units = match &raw {
        Raw::Latin1(b) => b.len(),
        Raw::Ucs2(u) => u.len(),
        Raw::Ucs4(u) => u.len(),
    };
    let found = if units < 4096 {
        scan(&raw)
    } else {
        py.detach(|| scan(&raw))
    };
    match found {
        Some(hit) => Ok(hit),
        // Same error class the UTF-8 path raised, so the caller's fallback holds.
        None => Err(PyUnicodeEncodeError::new_err((
            "utf-8",
            text.clone().unbind(),
            0usize,
            1usize,
            "surrogates not allowed",
        ))),
    }
}

#[pymodule]
fn kc_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("GATE_FINGERPRINT", GATE_FINGERPRINT)?;
    m.add_function(wrap_pyfunction!(might_contain_credential, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wide_bytes_are_not_ascii() {
        for b in [WIDE_WS, WIDE_I, WIDE_OTHER] {
            assert!(b >= 0x80);
        }
    }

    #[test]
    fn mapping_roles() {
        for &cp in PY_WS_WIDE {
            assert_eq!(map_wide(cp), WIDE_WS, "U+{cp:04X}");
        }
        assert_eq!(map_wide('a' as u32), b'a');
        assert_eq!(map_wide(0x3000), WIDE_WS);
        assert_eq!(map_wide(0x130), WIDE_I);
        assert_eq!(map_wide(0x4E2D), WIDE_OTHER);
        let mut v = Vec::new();
        assert!(!fill_wide(&[0x61u16, 0xD800], &mut v));
    }
}
