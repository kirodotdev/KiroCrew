"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  isValidDistroName,
  decodeWslOutput,
  normalizeWslState,
  parseWslListOutput,
  detectWsl2,
} = require("../wsl-detection");

describe("WSL distro name validation (isValidDistroName)", () => {
  it("accepts valid alphanumeric distribution names", () => {
    assert.equal(isValidDistroName("Ubuntu"), true);
    assert.equal(isValidDistroName("Ubuntu-24.04"), true);
    assert.equal(isValidDistroName("Debian"), true);
    assert.equal(isValidDistroName("kali-linux"), true);
    assert.equal(isValidDistroName("docker-desktop-data"), true);
    assert.equal(isValidDistroName("OracleLinux_9_1"), true);
    assert.equal(isValidDistroName("Arch.WSL"), true);
  });

  it("rejects command flags or leading dashes", () => {
    assert.equal(isValidDistroName("-d"), false);
    assert.equal(isValidDistroName("--distribution"), false);
    assert.equal(isValidDistroName("-Ubuntu"), false);
  });

  it("rejects shell metacharacters and injection attempts", () => {
    assert.equal(isValidDistroName("Ubuntu; rm -rf /"), false);
    assert.equal(isValidDistroName("Ubuntu && whoami"), false);
    assert.equal(isValidDistroName("Ubuntu | cat"), false);
    assert.equal(isValidDistroName("Ubuntu`id`"), false);
    assert.equal(isValidDistroName("Ubuntu$(id)"), false);
    assert.equal(isValidDistroName("Ubuntu\nmalicious"), false);
    assert.equal(isValidDistroName("Ubuntu\""), false);
    assert.equal(isValidDistroName("Ubuntu'"), false);
  });

  it("rejects whitespace and path traversals", () => {
    assert.equal(isValidDistroName("Ubuntu 24.04"), false);
    assert.equal(isValidDistroName("../Ubuntu"), false);
    assert.equal(isValidDistroName(".."), false);
    assert.equal(isValidDistroName(""), false);
    assert.equal(isValidDistroName("   "), false);
    assert.equal(isValidDistroName(null), false);
    assert.equal(isValidDistroName(undefined), false);
    assert.equal(isValidDistroName(123), false);
  });
});

describe("WSL output decoder (decodeWslOutput)", () => {
  it("decodes UTF-8 buffers, including one with a UTF-8 BOM", () => {
    // The child runs with WSL_UTF8=1, so UTF-8 is the primary encoding.
    assert.equal(decodeWslOutput(Buffer.from("Ubuntu   Running   2", "utf8")), "Ubuntu   Running   2");
    assert.equal(
      decodeWslOutput(Buffer.from("\uFEFFUbuntu   Running   2", "utf8")),
      "Ubuntu   Running   2"
    );
    assert.equal(decodeWslOutput("Ubuntu"), "Ubuntu");
    assert.equal(decodeWslOutput(""), "");
    assert.equal(decodeWslOutput(null), "");
  });

  it("decodes a UTF-16LE buffer carrying a BOM (legacy WSL builds that ignore WSL_UTF8)", () => {
    const text = "\uFEFFUbuntu   En cours d'exécution   2\n";
    const utf16Buffer = Buffer.from(text, "utf16le");
    assert.equal(decodeWslOutput(utf16Buffer), "Ubuntu   En cours d'exécution   2");

    const multiLine = Buffer.from("\uFEFFUbuntu\nDebian", "utf16le");
    assert.equal(decodeWslOutput(multiLine), "Ubuntu\nDebian");
  });

  it("decodes a BOM-less UTF-16LE buffer — some legacy builds omit the marker", () => {
    // Without the null-interleave check this decodes as UTF-8 garbage, every
    // name fails isValidDistroName, and a working WSL install silently reads
    // as "no WSL2 distros".
    const text = "  NAME      STATE    VERSION\r\n* Ubuntu    Running  2\r\n";
    const utf16Buffer = Buffer.from(text, "utf16le");
    const distros = parseWslListOutput(utf16Buffer);
    assert.equal(distros.length, 1);
    assert.equal(distros[0].name, "Ubuntu");
    assert.equal(distros[0].state, "running");
  });

  it("preserves non-ASCII characters from UTF-8 output", () => {
    const text = "  NAME      STATE    VERSION\n* Übüntü    Läuft    2\n";
    assert.equal(decodeWslOutput(Buffer.from(text, "utf8")), text.trim());
  });
});

describe("WSL state normalization (normalizeWslState)", () => {
  it("maps known localized states onto the stable enum, preserving the original label", () => {
    assert.deepEqual(normalizeWslState("Running"), { state: "running", stateLabel: "Running" });
    assert.deepEqual(normalizeWslState("Stopped"), { state: "stopped", stateLabel: "Stopped" });
    assert.deepEqual(
      normalizeWslState("En cours d'exécution"),
      { state: "running", stateLabel: "En cours d'exécution" }
    );
    assert.deepEqual(
      normalizeWslState("Wird ausgeführt"),
      { state: "running", stateLabel: "Wird ausgeführt" }
    );
    assert.deepEqual(normalizeWslState("Arrêté"), { state: "stopped", stateLabel: "Arrêté" });
    assert.deepEqual(normalizeWslState("Beendet"), { state: "stopped", stateLabel: "Beendet" });
    assert.deepEqual(normalizeWslState("Detenido"), { state: "stopped", stateLabel: "Detenido" });
  });

  it("degrades an unrecognized locale to unknown instead of leaking a pseudo-enum", () => {
    // Italian is not in the recognized set: consumers must see "unknown", never
    // a localized string where a value they branch on is expected.
    assert.deepEqual(
      normalizeWslState("In esecuzione"),
      { state: "unknown", stateLabel: "In esecuzione" }
    );
    assert.deepEqual(normalizeWslState(""), { state: "unknown", stateLabel: "" });
  });
});

describe("WSL list output parser (parseWslListOutput)", () => {
  it("parses standard wsl.exe --list --verbose output", () => {
    const output = `
  NAME                   STATE           VERSION
* Ubuntu                 Running         2
  Ubuntu-24.04           Stopped         2
  Debian                 Running         2
`;
    const distros = parseWslListOutput(output);
    assert.equal(distros.length, 3);

    assert.deepEqual(distros[0], {
      name: "Ubuntu",
      state: "running",
      stateLabel: "Running",
      version: 2,
      isDefault: true,
    });

    assert.deepEqual(distros[1], {
      name: "Ubuntu-24.04",
      state: "stopped",
      stateLabel: "Stopped",
      version: 2,
      isDefault: false,
    });

    assert.deepEqual(distros[2], {
      name: "Debian",
      state: "running",
      stateLabel: "Running",
      version: 2,
      isDefault: false,
    });
  });

  it("filters out WSL version 1 distributions", () => {
    const output = `
  NAME           STATE           VERSION
* Ubuntu         Running         2
  LegacyV1       Running         1
  Debian         Stopped         2
`;
    const distros = parseWslListOutput(output);
    assert.equal(distros.length, 2);
    assert.equal(distros.some((d) => d.name === "LegacyV1"), false);
  });

  it("parses UTF-16LE buffer output with a BOM directly", () => {
    const text = "  NAME      STATE    VERSION\r\n* Ubuntu    Running  2\r\n";
    const utf16Buffer = Buffer.from("\uFEFF" + text, "utf16le");
    const distros = parseWslListOutput(utf16Buffer);
    assert.equal(distros.length, 1);
    assert.equal(distros[0].name, "Ubuntu");
    assert.equal(distros[0].state, "running");
    assert.equal(distros[0].version, 2);
    assert.equal(distros[0].isDefault, true);
  });

  it("returns empty array for empty, malformed, or header-only output", () => {
    assert.deepEqual(parseWslListOutput(""), []);
    assert.deepEqual(parseWslListOutput("   \n   \n"), []);
    assert.deepEqual(parseWslListOutput("NAME STATE VERSION"), []);
    assert.deepEqual(parseWslListOutput("Malformed single column"), []);
  });

  it("parses localized Windows output with multi-word states", () => {
    const frenchOutput = `
  NAME                   STATE                       VERSION
* Ubuntu                 En cours d'exécution        2
  Debian                 Arrêté                      2
  SUSE                   Wird ausgeführt             2
`;
    const distros = parseWslListOutput(frenchOutput);
    assert.equal(distros.length, 3);
    assert.equal(distros[0].name, "Ubuntu");
    assert.equal(distros[0].state, "running");
    assert.equal(distros[0].version, 2);
    assert.equal(distros[0].isDefault, true);

    assert.equal(distros[1].name, "Debian");
    assert.equal(distros[1].state, "stopped");
    assert.equal(distros[1].version, 2);

    assert.equal(distros[2].name, "SUSE");
    assert.equal(distros[2].state, "running");
    assert.equal(distros[2].version, 2);
  });

  it("keeps a distro whose state is in an unrecognized locale, marked unknown", () => {
    const italianOutput = `
  NAME              STATE            VERSION
  Ubuntu            In esecuzione    2
`;
    const distros = parseWslListOutput(italianOutput);
    assert.equal(distros.length, 1);
    assert.equal(distros[0].name, "Ubuntu");
    assert.equal(distros[0].state, "unknown");
    assert.equal(distros[0].stateLabel, "In esecuzione");
  });

  it("ignores rows with invalid distro identifiers", () => {
    const output = `
  NAME                   STATE           VERSION
  ValidUbuntu            Running         2
  -bad--flag             Running         2
  bad;inject             Running         2
`;
    const distros = parseWslListOutput(output);
    assert.equal(distros.length, 1);
    assert.equal(distros[0].name, "ValidUbuntu");
  });
});

describe("WSL detection runner (detectWsl2)", () => {
  it("resolves as not available on non-Windows platforms", async () => {
    const result = await detectWsl2({ platform: "darwin" });
    assert.equal(result.available, false);
    assert.equal(result.reason, "not-windows");
    assert.deepEqual(result.distros, []);
    assert.equal(result.defaultDistro, null);
  });

  it("spawns the System32 wsl.exe with WSL_UTF8=1 and reads buffer output", async () => {
    let sawOptions = null;
    const mockExecFile = (file, args, options, callback) => {
      assert.equal(file, "C:\\Windows\\System32\\wsl.exe");
      assert.deepEqual(args, ["--list", "--verbose"]);
      sawOptions = options;
      callback(
        null,
        Buffer.from("  NAME      STATE    VERSION\n* Ubuntu    Running  2\n  Debian    Stopped  2\n", "utf8"),
        Buffer.from("")
      );
    };

    const result = await detectWsl2({
      platform: "win32",
      env: { SystemRoot: "C:\\Windows" },
      execFileFn: mockExecFile,
    });

    assert.equal(sawOptions.encoding, "buffer");
    assert.equal(sawOptions.env.WSL_UTF8, "1");
    assert.equal(result.available, true);
    assert.equal(result.distros.length, 2);
    assert.equal(result.defaultDistro, "Ubuntu");
  });

  it("refuses to run when the configured system root is not a Windows directory", async () => {
    const result = await detectWsl2({
      platform: "win32",
      env: { SystemRoot: "D:\\hostile\\Windows" },
      execFileFn: () => {
        throw new Error("execFile must not be called with an unvalidated root");
      },
    });

    assert.equal(result.available, false);
    assert.equal(result.reason, "wsl-error");
    assert.match(result.error, /system root must be a drive-root Windows directory/);
  });

  it("handles missing wsl.exe (ENOENT)", async () => {
    const mockExecFile = (file, args, options, callback) => {
      const err = new Error("spawn wsl.exe ENOENT");
      err.code = "ENOENT";
      callback(err, Buffer.from(""), Buffer.from(""));
    };

    const result = await detectWsl2({
      platform: "win32",
      execFileFn: mockExecFile,
    });

    assert.equal(result.available, false);
    assert.equal(result.reason, "wsl-not-found");
    assert.deepEqual(result.distros, []);
  });

  it("handles execution errors or non-zero exit codes", async () => {
    const mockExecFile = (file, args, options, callback) => {
      const err = new Error("Command failed");
      err.code = 1;
      callback(err, Buffer.from(""), Buffer.from("WSL is not installed or enabled.", "utf8"));
    };

    const result = await detectWsl2({
      platform: "win32",
      execFileFn: mockExecFile,
    });

    assert.equal(result.available, false);
    assert.equal(result.reason, "wsl-error");
    assert.match(result.error, /WSL is not installed/);
  });

  it("handles when WSL is present but has no WSL2 distros", async () => {
    const mockExecFile = (file, args, options, callback) => {
      callback(null, Buffer.from("  NAME      STATE    VERSION\n  Legacy1   Running  1\n", "utf8"), Buffer.from(""));
    };

    const result = await detectWsl2({
      platform: "win32",
      execFileFn: mockExecFile,
    });

    assert.equal(result.available, true);
    assert.deepEqual(result.distros, []);
    assert.equal(result.defaultDistro, null);
    assert.equal(result.reason, "no-wsl2-distros");
  });
});
