const { test, describe } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

// The launcher asks two different questions of a local port: whether anything
// holds it, and who holds it. Only the second can rest on the holder's own
// command line, because argv is what its owner chooses to present. This file
// asserts the two questions stay separate -- that nothing deciding mere
// occupancy reaches its verdict through a process's claim about what it is.
//
// The decisions are listed out below rather than discovered by parsing the
// source. A list is honest about what it covers, and it is safe here because of
// the guard that follows it: every comparison against one of these sentinels
// must be CLAIMED by an entry in a list, and one that is not claimed fails the
// pin. So a fifth decision cannot join the file quietly -- it reddens this test
// and someone has to decide which list it belongs in. Unclassifiable means deny,
// not allow, which is the same rule the rest of this surface follows.

const ROOT = path.join(__dirname, "..");

/** Source with comments removed: this file asserts on code, and the code it
 *  guards names both probes in prose. The `[^:]` guard keeps `http://` inside a
 *  string literal from reading as the start of a line comment. */
function code(file) {
  return fs.readFileSync(path.join(ROOT, file), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const SUPERVISOR = code("gateway-supervisor.js");
const STOP = code("gateway-stop.js");

const OCCUPANCY_PROBE = "probeGatewayPortBinding";
const IDENTITY_PROBE = "probeGatewayPortOwner";

// Every sentinel either probe can answer with, plus the owner vocabulary. An
// occurrence of any of these as a string LITERAL is a port judgement until a
// list says otherwise. Literals rather than `===` comparisons on purpose: a
// `switch` writes `case "none":` and an array writes a bare `"none"`, so a guard
// that only understood `===` would let exactly the shapes it exists to catch
// through.
const SENTINELS = ["free", "bound", "none", "unknown", "kirocrew", "service", "foreign"];

// THE FOUR OCCUPANCY DECISIONS. Each entry is the exact source text and the
// exact number of times it appears, so a site that is reworded stops matching
// and the guard below then reports it as unclaimed.
const OCCUPANCY_SITES = [
  {
    what: "waitForPortFree takes one reading of the port",
    code: 'const binding = await probeGatewayPortBinding(PORT);',
    times: 1,
  },
  {
    what: "waitForPortFree: the port has cleared",
    code: 'if (binding === "free") return true;',
    times: 1,
  },
  {
    what: "waitForPortFree: the probe could not look, so fall back to HTTP",
    code: 'if (binding === "unknown") {',
    times: 1,
  },
  {
    what: "the boot and liveness service-rebind waits, textually identical",
    code: 'isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",',
    times: 2,
  },
  {
    what: "drain completion; carries an identity comparison on the same line",
    code: 'if (localOwner !== "service" || (await probeGatewayPortBinding(PORT)) === "free") {',
    times: 1,
  },
];

// The identity judgements. These MAY rest on the holder's command line -- that
// is the weakness #13826 records and #13862 must close -- and they are listed
// so the guard can tell them from an occupancy decision that has drifted.
const IDENTITY_SITES = [
  {
    what: "the exported primary-port probe hands the verdict to the IPC gate",
    code: 'return probeGatewayPortOwner(PORT);',
    times: 1,
  },
  {
    what: "boot: probe the holder, unless a configured remote host makes it a tunnel",
    code: 'const localOwner = remoteHost ? "foreign" : await probeGatewayPortOwner(PORT);',
    times: 1,
  },
  {
    what: "boot: a service-managed holder may be rebound by its manager",
    code: 'if (localOwner === "service") {',
    times: 1,
  },
  {
    what: "liveness: re-validate whoever rebound the port through the boot decision",
    code: 'const owner = await probeGatewayPortOwner(PORT);',
    times: 1,
  },
  {
    what: "token prompt: same short-circuit, for the port that returned the 403",
    code: 'const localOwner = remoteHost\n          ? "foreign"\n          : await probeGatewayPortOwner(promptPort);',
    times: 1,
  },
];

// Sentinel literals that are not about a port at all. Listed, because the guard
// cannot tell them apart by shape and must not guess.
const NON_PORT_SITES = [
  {
    what: "the gatewayOwnership vocabulary's initial value, not a port reading",
    code: 'let gatewayOwnership = "none";',
    times: 1,
  },
  {
    what: "the HTTP readiness probe's own error result",
    code: 'req.on("error", () => resolve("unknown"));',
    times: 1,
  },
  {
    what: "the HTTP readiness probe's own timeout result",
    code: 'req.on("timeout", () => { req.destroy(); resolve("unknown"); });',
    times: 1,
  },
  {
    what: "readiness, short-circuited for a remote host; not an owner verdict",
    code: 'const readiness = remoteHost ? "unknown" : await fetchGatewayReadiness();',
    times: 1,
  },
  {
    what: "classifyStartFailure's own verdict vocabulary, unrelated to any port",
    code: 'if (verdict === "none") {',
    times: 1,
  },
];

const ALL_SITES = [...OCCUPANCY_SITES, ...IDENTITY_SITES, ...NON_PORT_SITES];

/** Byte spans of every occurrence of `needle` in the supervisor. */
function spansOf(needle) {
  const out = [];
  for (let at = SUPERVISOR.indexOf(needle); at !== -1; at = SUPERVISOR.indexOf(needle, at + 1)) {
    out.push([at, at + needle.length]);
  }
  return out;
}

/** 1-based line number of `offset`. */
function lineOf(offset) {
  return SUPERVISOR.slice(0, offset).split("\n").length;
}

/** Every sentinel string literal in the supervisor, whatever syntax holds it. */
function sentinelLiterals() {
  const out = [];
  const re = new RegExp(`"(${SENTINELS.join("|")})"`, "g");
  for (let m = re.exec(SUPERVISOR); m; m = re.exec(SUPERVISOR)) {
    out.push({ sentinel: m[1], offset: m.index, line: lineOf(m.index) });
  }
  return out;
}

/**
 * Every place either probe is CALLED, excluding the declarations.
 *
 * This is the guard that no syntax can slip. A verdict can only enter the file
 * by calling a probe, and a call has exactly one shape -- where the comparison
 * that follows has many, which is how an object-map lookup on bare keys walked
 * past a scan that read string literals.
 */
function probeCalls() {
  const out = [];
  const re = new RegExp(`\\b(${OCCUPANCY_PROBE}|${IDENTITY_PROBE})\\s*\\(`, "g");
  for (let m = re.exec(SUPERVISOR); m; m = re.exec(SUPERVISOR)) {
    if (/\bfunction\s+$/.test(SUPERVISOR.slice(Math.max(0, m.index - 16), m.index))) continue;
    out.push({ probe: m[1], offset: m.index, line: lineOf(m.index) });
  }
  return out;
}

/** The body of `name` in `source`. The parameter list is skipped by paren
 *  balance first: both probes destructure their dependencies, so the first brace
 *  after the name belongs to a parameter, not to the body being asserted. */
function bodyOf(source, name) {
  const start = source.search(new RegExp(`\\bfunction\\s+${name}\\s*\\(`));
  assert.notStrictEqual(start, -1, `${name} must exist`);
  const paramsOpen = source.indexOf("(", start);
  let parens = 0;
  let paramsClose = -1;
  for (let i = paramsOpen; i < source.length; i += 1) {
    if (source[i] === "(") parens += 1;
    else if (source[i] === ")") {
      parens -= 1;
      if (parens === 0) { paramsClose = i; break; }
    }
  }
  assert.notStrictEqual(paramsClose, -1, `${name}'s parameter list must close`);
  const open = source.indexOf("{", paramsClose);
  assert.notStrictEqual(open, -1, `${name} must have a body`);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open, i + 1);
    }
  }
  throw new Error(`${name}'s body is unbalanced`);
}

// Anything through which a process's own account of itself can enter a verdict.
// `getCommand` reads the holder's command line; the predicates judge it.
const SELF_ASSERTED_IDENTITY = [
  "getCommand",
  "isKirocrew",
  "isKirocrewCommand",
  "isTrustedWindowsGatewayCommand",
  "classifyPortOwner",
  "windowsProcessCommand",
  "psCommand",
];

describe("port occupancy is decided without a process's self-asserted identity", () => {
  test("every listed site is present in the source exactly as often as claimed", () => {
    const wrong = ALL_SITES
      .map((s) => ({ what: s.what, found: spansOf(s.code).length, want: s.times }))
      .filter((s) => s.found !== s.want);
    assert.deepStrictEqual(
      wrong,
      [],
      "a listed site no longer matches the source. Update the list and re-read what "
      + "the site now decides: "
      + wrong.map((s) => `${s.what} (found ${s.found}, expected ${s.want})`).join("; "),
    );
  });

  test("no occupancy decision names the identity probe", () => {
    const wrong = OCCUPANCY_SITES.filter((s) => s.code.includes(IDENTITY_PROBE));
    assert.deepStrictEqual(
      wrong.map((s) => s.what),
      [],
      "an occupancy decision reads the identity probe, whose POSIX verdict comes "
      + "from the holder's argv, so a same-user process can move it",
    );
    const reading = OCCUPANCY_SITES.filter((s) => s.code.includes(OCCUPANCY_PROBE));
    assert.ok(
      reading.length >= 3,
      `only ${reading.length} listed occupancy sites read ${OCCUPANCY_PROBE}; the list `
      + "has lost its subject",
    );
  });

  // THE GUARD, in two nets. The first is the one that cannot be evaded.
  test("no probe CALL is unclaimed by a list", () => {
    const claimed = ALL_SITES.flatMap((s) => spansOf(s.code).map((span) => span));
    const unclaimed = probeCalls().filter(
      (c) => !claimed.some((span) => c.offset >= span[0] && c.offset < span[1]),
    );
    assert.deepStrictEqual(
      unclaimed.map((c) => `line ${c.line}: ${c.probe}`),
      [],
      "a probe is called somewhere no list claims. A verdict can only enter this "
      + "file through a call, so this is the net that holds whatever syntax the "
      + "comparison is written in. Add the site to OCCUPANCY_SITES or "
      + "IDENTITY_SITES; if it decides mere occupancy it must read " + OCCUPANCY_PROBE,
    );
    assert.ok(probeCalls().length > 0, "the call scan found no probe calls at all");
  });

  test("no sentinel literal is unclaimed by a list", () => {
    const claimed = ALL_SITES.flatMap((s) => spansOf(s.code).map((span) => span));
    const unclaimed = sentinelLiterals().filter(
      (c) => !claimed.some((span) => c.offset >= span[0] && c.offset < span[1]),
    );
    assert.deepStrictEqual(
      unclaimed.map((c) => `line ${c.line}: "${c.sentinel}"`),
      [],
      "the port vocabulary appears somewhere no list claims. This pin cannot tell "
      + "what that use is, so it FAILS rather than passing it: add it to "
      + "OCCUPANCY_SITES, IDENTITY_SITES or NON_PORT_SITES",
    );
    assert.ok(sentinelLiterals().length > 0, "the literal scan found nothing at all");
  });

  test("the occupancy probe reads no command line", () => {
    const body = bodyOf(STOP, "probePortBinding");
    const leaked = SELF_ASSERTED_IDENTITY.filter((n) => new RegExp(`\\b${n}\\b`).test(body));
    assert.deepStrictEqual(
      leaked,
      [],
      `probePortBinding must decide from the pid list alone, but names ${leaked.join(", ")}`,
    );
  });

  test("the occupancy probe is wired with no identity dependency", () => {
    const body = bodyOf(SUPERVISOR, OCCUPANCY_PROBE);
    const leaked = SELF_ASSERTED_IDENTITY.filter((n) => new RegExp(`\\b${n}\\b`).test(body));
    assert.deepStrictEqual(
      leaked,
      [],
      `${OCCUPANCY_PROBE} must inject only a pid probe, but passes ${leaked.join(", ")}`,
    );
  });

  test("the identity classifier still refuses an unreadable probe", () => {
    const body = bodyOf(STOP, "classifyPortOwner");
    assert.match(
      body,
      /return "unknown"/,
      "classifyPortOwner must keep answering unknown when it cannot look",
    );
  });
});
