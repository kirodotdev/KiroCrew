const { test } = require("node:test");
const assert = require("node:assert");
const {
  parseNetstatListenPids,
  windowsGatewayExecutablePath,
  windowsSystemToolPaths,
  windowsListenPids,
  windowsProcessCommand,
  windowsTaskkill,
} = require("../windows-port");
const {
  classifyPortOwner,
  forceStopPort,
  isKirocrewCommand,
} = require("../gateway-stop");

const TEST_TOOLS = windowsSystemToolPaths("D:\\Windows");

test("windowsSystemToolPaths resolves every utility beneath the system root", () => {
  assert.deepStrictEqual(TEST_TOOLS, {
    netstat: "D:\\Windows\\System32\\netstat.exe",
    powershell:
      "D:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    wmic: "D:\\Windows\\System32\\wbem\\wmic.exe",
    taskkill: "D:\\Windows\\System32\\taskkill.exe",
  });
  assert.throws(
    () => windowsSystemToolPaths(".\\hostile"),
    /system root must be a drive-root Windows directory/
  );
  assert.throws(
    () => windowsSystemToolPaths("D:\\hostile\\Windows"),
    /system root must be a drive-root Windows directory/
  );
});

test("windowsGatewayExecutablePath resolves the executable the launcher spawns", () => {
  assert.strictEqual(
    windowsGatewayExecutablePath(
      "D:\\KiroCrew\\resources\\backend-dist\\kirocrew-backend\\bin\\kirocrew.cmd"
    ),
    "D:\\KiroCrew\\resources\\backend-dist\\kirocrew-backend\\python.exe"
  );
  assert.strictEqual(
    windowsGatewayExecutablePath("D:\\venv\\Scripts\\kirocrew.exe"),
    "D:\\venv\\Scripts\\kirocrew.exe"
  );
  assert.strictEqual(
    windowsGatewayExecutablePath("kirocrew.exe", { pathEnv: "" }),
    ""
  );
});

test("windowsGatewayExecutablePath resolves a PATH-launched executable", () => {
  const expected = "D:\\Kiro Crew\\kirocrew.exe";
  const probed = [];
  const resolved = windowsGatewayExecutablePath("kirocrew.exe", {
    pathEnv: 'C:\\Other;"D:\\Kiro Crew"',
    accessSync: (candidate) => {
      probed.push(candidate);
      if (candidate !== expected) throw new Error("ENOENT");
    },
  });
  assert.strictEqual(resolved, expected);
  assert.deepStrictEqual(probed, [
    "C:\\Other\\kirocrew.exe",
    expected,
  ]);
});

test("parseNetstatListenPids finds IPv4 and IPv6 listeners without reading state text", () => {
  const output = [
    "  Proto  Local Address          Foreign Address        State           PID",
    "  TCP    127.0.0.1:5476        0.0.0.0:0              ABHÖREN         4242",
    "  TCP    [::1]:5476            [::]:0                 ÉCOUTE          4343",
    "  TCP    0.0.0.0:5476          0.0.0.0:0              ESCUCHANDO      4242",
  ].join("\r\n");
  assert.deepStrictEqual(parseNetstatListenPids(output, 5476), [4242, 4343]);
});

test("parseNetstatListenPids requires the exact local port and a wildcard peer", () => {
  const output = [
    "  TCP    127.0.0.1:54760       0.0.0.0:0              LISTENING       1111",
    "  TCP    127.0.0.1:5476        10.0.0.5:443           ESTABLISHED     2222",
    "  UDP    127.0.0.1:5476        *:*                                    3333",
  ].join("\r\n");
  assert.deepStrictEqual(parseNetstatListenPids(output, 5476), []);
});

test("windowsListenPids queries both address families with a bounded timeout", async () => {
  let invocation;
  const execFileFn = (command, args, options, callback) => {
    invocation = { command, args, options };
    callback(null, "TCP  [::1]:5476  [::]:0  ECOUTE  4343");
  };
  const pids = await windowsListenPids(5476, {
    execFileFn,
    timeoutMs: 1234,
    tools: TEST_TOOLS,
  });
  assert.deepStrictEqual(pids, [4343]);
  assert.deepStrictEqual(invocation, {
    command: TEST_TOOLS.netstat,
    args: ["-ano"],
    options: { timeout: 1234 },
  });
});

test("windowsListenPids rejects netstat errors and timeouts", async () => {
  const timeout = Object.assign(new Error("netstat timed out"), { code: "ETIMEDOUT" });
  const execFileFn = (_command, _args, _options, callback) => callback(timeout, "");
  await assert.rejects(
    windowsListenPids(5476, { execFileFn, tools: TEST_TOOLS }),
    /netstat timed out/
  );
});

test("windowsProcessCommand prefers PowerShell command-line output", async () => {
  const calls = [];
  const execFileFn = (command, args, options, callback) => {
    calls.push({ command, args, options });
    callback(null, '"C:\\Program Files\\KiroCrew\\kirocrew.exe" gateway\r\n');
  };
  const command = await windowsProcessCommand(4242, {
    execFileFn,
    tools: TEST_TOOLS,
  });
  assert.strictEqual(command, '"C:\\Program Files\\KiroCrew\\kirocrew.exe" gateway');
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].command, TEST_TOOLS.powershell);
  assert.match(calls[0].args.at(-1), /ProcessId = 4242/);
  assert.match(calls[0].args.at(-1), /ExecutablePath/);
});

test("windowsProcessCommand falls back to WMIC and unwraps its value", async () => {
  const calls = [];
  const execFileFn = (command, args, _options, callback) => {
    calls.push({ command, args });
    if (command === TEST_TOOLS.powershell) {
      callback(new Error("PowerShell unavailable"), "");
      return;
    }
    callback(
      null,
      "\r\nExecutablePath=C:\\Python\\python.exe\r\n"
        + "CommandLine=C:\\Python\\python.exe -m kiro_crew gateway\r\n\r\n"
    );
  };
  const command = await windowsProcessCommand(4242, {
    execFileFn,
    tools: TEST_TOOLS,
  });
  assert.strictEqual(
    command,
    '"C:\\Python\\python.exe" C:\\Python\\python.exe -m kiro_crew gateway'
  );
  assert.deepStrictEqual(
    calls.map(({ command: name }) => name),
    [TEST_TOOLS.powershell, TEST_TOOLS.wmic]
  );
  assert.deepStrictEqual(calls[1].args, [
    "process", "where", "ProcessId=4242", "get",
    "ExecutablePath,CommandLine", "/FORMAT:LIST",
  ]);
});

test("windowsProcessCommand fails closed when neither identity probe works", async () => {
  const execFileFn = (_command, _args, _options, callback) =>
    callback(new Error("not available"), "");
  assert.strictEqual(
    await windowsProcessCommand(4242, { execFileFn, tools: TEST_TOOLS }),
    ""
  );
});

test("windowsProcessCommand fails closed when WMIC cannot prove the executable path", async () => {
  const execFileFn = (command, _args, _options, callback) => {
    if (command === TEST_TOOLS.powershell) {
      callback(new Error("PowerShell unavailable"), "");
      return;
    }
    callback(null, "CommandLine=kirocrew gateway\r\n");
  };
  assert.strictEqual(
    await windowsProcessCommand(4242, { execFileFn, tools: TEST_TOOLS }),
    ""
  );
});

test("isKirocrewCommand accepts Windows executable and module shapes", () => {
  const trustedCli = "C:\\Program Files\\KiroCrew\\kirocrew.exe";
  const trustedBackend = "C:\\bundle\\kirocrew-backend.exe";
  const trustedPython = "C:\\Python\\python.exe";
  assert.strictEqual(
    isKirocrewCommand(`"${trustedCli}" gateway`, {
      trustedExecutablePaths: [trustedCli],
    }),
    true
  );
  assert.strictEqual(
    isKirocrewCommand(`${trustedBackend} gateway`, {
      trustedExecutablePaths: [trustedBackend],
    }),
    true
  );
  assert.strictEqual(
    isKirocrewCommand(`${trustedPython} -s -m kiro_crew gateway`, {
      trustedExecutablePaths: [trustedPython],
    }),
    true
  );
  assert.strictEqual(
    isKirocrewCommand(
      `${trustedPython} C:\\venv\\Scripts\\kirocrew gateway`,
      { trustedExecutablePaths: [trustedPython] }
    ),
    true
  );
  assert.strictEqual(
    isKirocrewCommand(
      `"${trustedPython}" ${trustedPython} -s -m kiro_crew gateway`,
      { trustedExecutablePaths: [trustedPython] }
    ),
    true
  );
});

test("isKirocrewCommand rejects Windows path substrings and unrelated Python", () => {
  assert.strictEqual(
    isKirocrewCommand("C:\\Users\\kirocrew\\OtherApp\\server.exe --port 5476"),
    false
  );
  assert.strictEqual(
    isKirocrewCommand("C:\\Users\\kirocrew\\python.exe -m http.server 5476"),
    false
  );
  assert.strictEqual(
    isKirocrewCommand("C:\\Python\\python.exe app.py kirocrew"),
    false
  );
  assert.strictEqual(
    isKirocrewCommand("C:\\Python\\python.exe app.py C:\\tmp\\kirocrew"),
    false
  );
  assert.strictEqual(
    isKirocrewCommand("C:\\Python\\python.exe app.py -m kiro_crew"),
    false
  );
  assert.strictEqual(
    isKirocrewCommand("C:\\Temp\\kirocrew.exe gateway", {
      trustedExecutablePaths: [
        "C:\\Program Files\\KiroCrew\\kirocrew.exe",
      ],
    }),
    false
  );
});

test("isKirocrewCommand rejects SSH aliases and remote gateway commands", () => {
  assert.strictEqual(
    isKirocrewCommand("C:\\Windows\\System32\\OpenSSH\\ssh.exe -NL 5476:localhost:5476 kirocrew"),
    false
  );
  assert.strictEqual(
    isKirocrewCommand("ssh.exe host python.exe -m kiro_crew gateway"),
    false
  );
});

test("Windows owner classification returns unknown when netstat cannot run", async () => {
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => { throw new Error("netstat unavailable"); },
    getCommand: async () => "",
  });
  assert.strictEqual(owner, "unknown");
});

test("Windows force-stop refuses an SSH holder even when its alias is kirocrew", async () => {
  const killed = [];
  const result = await forceStopPort(5476, {
    getListenPids: async () => [909],
    getCommand: async () =>
      "C:\\Windows\\System32\\OpenSSH\\ssh.exe -NL 5476:localhost:5476 kirocrew",
    kill: async (pid) => killed.push(pid),
    sleep: async () => {},
    failClosedOnProbeError: true,
  });
  assert.deepStrictEqual(killed, []);
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.foreignHolder, true);
});

test("Windows force-stop refuses unrelated Python with a later kirocrew path", async () => {
  const killed = [];
  const result = await forceStopPort(5476, {
    getListenPids: async () => [910],
    getCommand: async () => "C:\\Python\\python.exe app.py C:\\tmp\\kirocrew",
    kill: async (pid) => killed.push(pid),
    sleep: async () => {},
    failClosedOnProbeError: true,
  });
  assert.deepStrictEqual(killed, []);
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.foreignHolder, true);
});

test("Windows force-stop refuses an untrusted kirocrew.exe basename", async () => {
  const killed = [];
  const trustedCli = "C:\\Program Files\\KiroCrew\\kirocrew.exe";
  const result = await forceStopPort(5476, {
    getListenPids: async () => [911],
    getCommand: async () => "C:\\Temp\\kirocrew.exe gateway",
    kill: async (pid) => killed.push(pid),
    sleep: async () => {},
    isKirocrew: (command) => isKirocrewCommand(command, {
      trustedExecutablePaths: [trustedCli],
    }),
    failClosedOnProbeError: true,
  });
  assert.deepStrictEqual(killed, []);
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.foreignHolder, true);
});

test("windowsTaskkill revalidates identity before using force on the PID", async () => {
  let invocation;
  const trustedCli = "C:\\Program Files\\KiroCrew\\kirocrew.exe";
  const execFileFn = (command, args, options, callback) => {
    invocation = { command, args, options };
    callback(null, "SUCCESS");
  };
  await windowsTaskkill(4242, {
    execFileFn,
    timeoutMs: 2345,
    tools: TEST_TOOLS,
    getCommandFn: async () => `"${trustedCli}" gateway`,
    isTrustedCommand: (command) => isKirocrewCommand(command, {
      trustedExecutablePaths: [trustedCli],
    }),
  });
  assert.deepStrictEqual(invocation, {
    command: TEST_TOOLS.taskkill,
    args: ["/F", "/PID", "4242"],
    options: { timeout: 2345 },
  });
});

test("windowsTaskkill refuses a PID reused by an unrelated process", async () => {
  let invoked = false;
  const trustedCli = "C:\\Program Files\\KiroCrew\\kirocrew.exe";
  await assert.rejects(
    windowsTaskkill(4242, {
      execFileFn: () => { invoked = true; },
      tools: TEST_TOOLS,
      getCommandFn: async () => "C:\\Windows\\System32\\notepad.exe",
      isTrustedCommand: (command) => isKirocrewCommand(command, {
        trustedExecutablePaths: [trustedCli],
      }),
    }),
    /process identity changed/
  );
  assert.strictEqual(invoked, false);
});

test("Windows force-stop cannot taskkill a PID that changes owners", async () => {
  const trustedCli = "C:\\Program Files\\KiroCrew\\kirocrew.exe";
  let taskkillInvoked = false;
  const result = await forceStopPort(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => `"${trustedCli}" gateway`,
    kill: (pid) => windowsTaskkill(pid, {
      execFileFn: () => { taskkillInvoked = true; },
      tools: TEST_TOOLS,
      getCommandFn: async () => "C:\\Windows\\System32\\notepad.exe",
      isTrustedCommand: (command) => isKirocrewCommand(command, {
        trustedExecutablePaths: [trustedCli],
      }),
    }),
    sleep: async () => {},
    isKirocrew: (command) => isKirocrewCommand(command, {
      trustedExecutablePaths: [trustedCli],
    }),
    failClosedOnProbeError: true,
  });
  assert.strictEqual(taskkillInvoked, false);
  assert.strictEqual(result.killed, 0);
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.foreignHolder, true);
});

test("Windows process tools reject invalid numeric identifiers", async () => {
  assert.throws(() => parseNetstatListenPids("", 0), /port must be a positive integer/);
  await assert.rejects(windowsProcessCommand("4; Stop-Process", {}), /pid must be a positive integer/);
  await assert.rejects(windowsTaskkill(-1, {}), /pid must be a positive integer/);
});
