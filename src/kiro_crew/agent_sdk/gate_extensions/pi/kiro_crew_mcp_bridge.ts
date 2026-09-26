/**
 * Kiro Crew MCP bridge for the pi coding agent.
 *
 * pi-acp accepts a session `mcpServers` array but does not forward it to the pi
 * process, so Crew's stdio MCP tools (kirocrew-core, kirocrew-cron, …) never
 * appear. This extension is the compensating channel: it connects to a
 * host-side broker over the local endpoint named by
 * `KIROCREW_PI_MCP_BROKER_SOCK` (unix socket / named pipe). The broker — which
 * runs unsandboxed in the ACP client — holds secret-bearing server config and
 * spawns the real MCP children. This process never sees per-server env, never
 * reads a servers JSON file, and never spawns credentialed MCP servers itself
 * (GPT F1).
 *
 * Tools are `registerTool`ed as `mcp__{server}__{tool}` so governance sees the
 * same names other backends use. Names containing `__` are refused.
 *
 * Loaded by Kiro Crew on pi's command line (`--extension <this file>`) beside
 * the tool gate, never from the operator's own extension directories, and
 * verified after load through the probe command registered below.
 *
 * On connect / list failure: log to stderr and continue with an empty tool
 * set — do not crash pi.
 */

import * as net from "node:net";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

/** Registered so the host can ask pi's command registry whether this file loaded. */
const PROBE_COMMAND = "kiro-crew-mcp-bridge";

/**
 * Address of the host broker endpoint. On POSIX a filesystem unix-socket path
 * under pi-gate; on Windows a `\\.\pipe\…` name. Carries no credentials.
 */
const BROKER_SOCK_ENV = "KIROCREW_PI_MCP_BROKER_SOCK";

/** Cap unterminated receive so a wedged broker cannot OOM pi. */
const MAX_RECEIVE_BUFFER = 8 * 1024 * 1024;

/** Per-request timeout (ms) for bridge/list and bridge/call. */
const REQUEST_TIMEOUT_MS = 120_000;

const McpArgs = Type.Object({}, { additionalProperties: true });

type JsonRpcResponse = {
	jsonrpc?: string;
	id?: number | string | null;
	result?: unknown;
	error?: { code?: number; message?: string; data?: unknown };
};

type BridgedTool = {
	server: string;
	name: string;
	description?: string;
	inputSchema?: unknown;
};

function parametersFor(tool: BridgedTool): typeof McpArgs {
	const schema = tool.inputSchema;
	if (schema && typeof schema === "object" && !Array.isArray(schema)) {
		return schema as typeof McpArgs;
	}
	return McpArgs;
}

function contentFor(result: unknown): Array<
  { type: "text"; text: string } | { type: "image"; data: string; mimeType: string }
> {
  const body = result && typeof result === "object" ? result as Record<string, unknown> : {};
  if (!Array.isArray(body.content)) {
    return [{ type: "text", text: JSON.stringify(result ?? null) }];
  }
  const content = body.content.map((block: unknown) => {
    const item = block && typeof block === "object" ? block as Record<string, unknown> : {};
    if (item.type === "text" && typeof item.text === "string") {
      return { type: "text" as const, text: item.text };
    }
    if (item.type === "image" && typeof item.data === "string" && typeof item.mimeType === "string") {
      return { type: "image" as const, data: item.data, mimeType: item.mimeType };
    }
    return { type: "text" as const, text: JSON.stringify(block ?? null) };
  });
  if (body.structuredContent !== undefined) {
    content.push({ type: "text" as const, text: JSON.stringify(body.structuredContent) });
  }
  return content;
}

function log(...parts: unknown[]): void {
	console.error("[kiro-crew-mcp-bridge]", ...parts);
}

class BrokerClient {
	private socket: net.Socket | null = null;
	private buffer = "";
	private nextId = 1;
	private readonly pending = new Map<
		number,
		{
			resolve: (v: JsonRpcResponse) => void;
			reject: (e: Error) => void;
			timer: ReturnType<typeof setTimeout>;
		}
	>();

	async connect(address: string): Promise<void> {
		await new Promise<void>((resolve, reject) => {
			const sock = net.connect({ path: address }, () => {
				this.socket = sock;
				resolve();
			});
			sock.setEncoding("utf8");
			sock.on("data", (chunk: string | Buffer) => {
				this.onData(typeof chunk === "string" ? chunk : chunk.toString("utf8"));
			});
			sock.on("error", (err) => {
				if (!this.socket) {
					reject(err);
					return;
				}
				this.failAll(err instanceof Error ? err : new Error(String(err)));
			});
			sock.on("close", () => {
				this.failAll(new Error("broker connection closed"));
				this.socket = null;
			});
		});
	}

	private failAll(err: Error): void {
		for (const [, p] of this.pending) {
			clearTimeout(p.timer);
			p.reject(err);
		}
		this.pending.clear();
	}

	private onData(chunk: string): void {
		this.buffer += chunk;
		if (this.buffer.length > MAX_RECEIVE_BUFFER) {
			log(`receive buffer overflow (${this.buffer.length} bytes); closing`);
			this.buffer = "";
			this.failAll(new Error("broker receive buffer overflow"));
			this.close();
			return;
		}
		for (;;) {
			const nl = this.buffer.indexOf("\n");
			if (nl < 0) break;
			const line = this.buffer.slice(0, nl).trim();
			this.buffer = this.buffer.slice(nl + 1);
			if (!line) continue;
			let msg: JsonRpcResponse;
			try {
				msg = JSON.parse(line) as JsonRpcResponse;
			} catch {
				log("non-JSON line from broker:", line.slice(0, 200));
				continue;
			}
			if (msg.id == null) continue;
			const id = typeof msg.id === "number" ? msg.id : Number(msg.id);
			const pending = this.pending.get(id);
			if (!pending) continue;
			this.pending.delete(id);
			clearTimeout(pending.timer);
			pending.resolve(msg);
		}
	}

	request(method: string, params?: unknown): Promise<unknown> {
		const id = this.nextId++;
		const payload = JSON.stringify({
			jsonrpc: "2.0",
			id,
			method,
			...(params === undefined ? {} : { params }),
		});
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => {
				this.pending.delete(id);
				reject(new Error(`broker ${method} timed out after ${REQUEST_TIMEOUT_MS}ms`));
			}, REQUEST_TIMEOUT_MS);
			this.pending.set(id, {
				resolve: (msg) => {
					if (msg.error) {
						reject(
							new Error(
								msg.error.message || `broker error ${msg.error.code ?? ""}`.trim(),
							),
						);
						return;
					}
					resolve(msg.result);
				},
				reject,
				timer,
			});
			try {
				if (!this.socket) {
					clearTimeout(timer);
					this.pending.delete(id);
					reject(new Error("broker not connected"));
					return;
				}
				this.socket.write(payload + "\n");
			} catch (err) {
				clearTimeout(timer);
				this.pending.delete(id);
				reject(err instanceof Error ? err : new Error(String(err)));
			}
		});
	}

	async listTools(): Promise<BridgedTool[]> {
		const result = (await this.request("bridge/list", {})) as {
			tools?: BridgedTool[];
		};
		return Array.isArray(result?.tools) ? result.tools : [];
	}

	async callTool(
		toolCallId: string,
		server: string,
		tool: string,
		args: Record<string, unknown>,
	): Promise<unknown> {
		return this.request("bridge/call", {
			toolCallId,
			server,
			tool,
			arguments: args,
		});
	}

	close(): void {
		try {
			this.socket?.destroy();
		} catch {
			/* ignore */
		}
		this.socket = null;
	}
}

export default function (pi: ExtensionAPI) {
  const bridgedNames = new Set<string>();
  pi.on("tool_result", async (event) => {
    if (!bridgedNames.has(event.toolName)) return;
    const details = event.details as { mcpResult?: { isError?: boolean } } | undefined;
    if (details?.mcpResult?.isError === true) return { isError: true };
  });
	pi.registerCommand(PROBE_COMMAND, {
		description: "Kiro Crew's MCP bridge is loaded in this session",
		handler: async (_args, ctx) => {
			ctx.ui.notify("Kiro Crew MCP bridge: active", "info");
		},
	});

	const address = process.env[BROKER_SOCK_ENV];
	// Drop the env var so later code cannot rediscover the endpoint via env
	// inspection alone (the path under pi-gate is still reachable; peer checks
	// on the host broker are the admission gate).
	try {
		delete process.env[BROKER_SOCK_ENV];
	} catch {
		/* ignore */
	}

	if (!address || !address.trim()) {
		log("no broker endpoint advertised; continuing without Crew MCP tools");
		return;
	}

	const client = new BrokerClient();

	pi.on("session_shutdown", async () => {
		client.close();
	});

	void (async () => {
		try {
			await client.connect(address);
		} catch (err) {
			log(`failed to connect to broker at ${address}:`, err);
			return;
		}
		let tools: BridgedTool[];
		try {
			tools = await client.listTools();
		} catch (err) {
			log("bridge/list failed:", err);
			client.close();
			return;
		}
		log(`broker listed ${tools.length} tool(s)`);
		for (const tool of tools) {
			if (!tool || typeof tool.server !== "string" || !tool.server) continue;
			if (typeof tool.name !== "string" || !tool.name) continue;
			if (tool.server.includes("__") || tool.name.includes("__")) {
				log(
					`refusing tool whose fused name would be ambiguous: ` +
						`server=${JSON.stringify(tool.server)} tool=${JSON.stringify(tool.name)}`,
				);
				continue;
			}
			const toolName = `mcp__${tool.server}__${tool.name}`;
			const description =
				typeof tool.description === "string" && tool.description
					? tool.description
					: `MCP tool ${tool.name} from ${tool.server}`;
			try {
				pi.registerTool({
					name: toolName,
					label: `${tool.server}/${tool.name}`,
					description,
					parameters: parametersFor(tool),
					prepareArguments: (a) =>
						a && typeof a === "object" && !Array.isArray(a)
							? (a as Record<string, unknown>)
							: {},
					async execute(toolCallId, params) {
						try {
							const result = await client.callTool(
								toolCallId,
								tool.server,
								tool.name,
								(params && typeof params === "object"
									? params
									: {}) as Record<string, unknown>,
							);
							return { content: contentFor(result), details: { mcpResult: result } };
						} catch (err) {
							const message = err instanceof Error ? err.message : String(err);
							return {
								content: [
									{
										type: "text",
										text: JSON.stringify({ error: message }),
									},
								],
								details: { mcpResult: { isError: true }, error: message },
							};
						}
					},
				});
                bridgedNames.add(toolName);
			} catch (err) {
				log(`registerTool failed for ${toolName}:`, err);
			}
		}
	})();
}
