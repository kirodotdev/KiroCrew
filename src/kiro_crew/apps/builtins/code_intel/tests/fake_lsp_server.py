"""A minimal stdio language server for the code-intel tests.

It indexes ``def NAME`` lines in the documents it has been sent and answers
definition / references / hover / documentSymbol from that index. It also sends
the client a ``workspace/configuration`` request during initialize, so the
client's server-request handling is exercised, and records every message it
received to the path in ``FAKE_LSP_LOG`` (one JSON object per line).
"""

import json
import os
import re
import sys

_DEF = re.compile(r"^\s*def (\w+)")
docs: dict[str, str] = {}
log_path = os.environ.get("FAKE_LSP_LOG")
next_id = 1000
root_path = ""


def log(message):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(message) + "\n")


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    assert length is not None
    return json.loads(sys.stdin.buffer.read(length))


def send(payload):
    body = json.dumps(payload).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def word_at(uri, line, character):
    lines = docs.get(uri, "").split("\n")
    if line >= len(lines):
        return ""
    text = lines[line]
    for match in re.finditer(r"\w+", text):
        if match.start() <= character <= match.end():
            return match.group(0)
    return ""


def definitions(name):
    out = []
    for uri, text in docs.items():
        for n, line in enumerate(text.split("\n")):
            match = _DEF.match(line)
            if match and match.group(1) == name:
                col = line.index(name)
                out.append(
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": n, "character": col},
                            "end": {"line": n, "character": col + len(name)},
                        },
                    }
                )
    return out


def references(name):
    out = []
    for uri, text in docs.items():
        for n, line in enumerate(text.split("\n")):
            for match in re.finditer(r"\b%s\b" % re.escape(name), line):
                out.append(
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": n, "character": match.start()},
                            "end": {"line": n, "character": match.end()},
                        },
                    }
                )
    return out


def workspace_symbols(query):
    """``def`` lines in the root's ``.py`` files whose name contains *query*."""
    out = []
    for dirpath, _dirs, files in os.walk(root_path):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh.read().split("\n")):
                    match = _DEF.match(line)
                    if match and query in match.group(1):
                        col = line.index(match.group(1))
                        point = {"line": n, "character": col}
                        out.append(
                            {
                                "name": match.group(1),
                                "kind": 12,
                                "containerName": name[:-3],
                                "location": {
                                    "uri": "file://" + path,
                                    "range": {"start": point, "end": point},
                                },
                            }
                        )
    point = {"line": 0, "character": 0}
    if query == "external":
        out.append(
            {
                "name": "External",
                "kind": 5,
                "location": {"uri": "untitled:External", "range": {"start": point, "end": point}},
            }
        )
    if query in ("library", "Lib"):
        out.append(
            {
                "name": "Lib",
                "kind": 5,
                "location": {"uri": LIBRARY_CLASS, "range": {"start": point, "end": point}},
            }
        )
    return out


# A library class, as jdtls names one in a jar, and the source it serves for it.
LIBRARY_CLASS = "jdt://contents/lib.jar/x/Lib.class?=fake"
LIBRARY_SOURCE = "package x;\n\npublic class Lib {}\n"


def main():
    global next_id, root_path
    while True:
        message = read()
        if message is None:
            return
        log(message)
        method = message.get("method")
        params = message.get("params") or {}
        if method is None:
            continue  # a response to our own request
        if method == "initialize":
            root_path = params.get("rootPath") or ""
            if os.environ.get("FAKE_LSP_STDERR"):
                sys.stderr.write(os.environ["FAKE_LSP_STDERR"] + "\n")
                sys.stderr.flush()
            send(
                {
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "workspace/configuration",
                    "params": {"items": [{"section": "fake"}]},
                }
            )
            next_id += 1
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"capabilities": {"definitionProvider": True}},
                }
            )
        elif method == "textDocument/didOpen":
            doc = params["textDocument"]
            docs[doc["uri"]] = doc["text"]
        elif method == "textDocument/didChange":
            docs[params["textDocument"]["uri"]] = params["contentChanges"][-1]["text"]
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": message["id"], "result": None})
        elif method == "exit":
            return
        elif "id" in message:
            uri = params.get("textDocument", {}).get("uri", "")
            pos = params.get("position", {})
            name = word_at(uri, pos.get("line", 0), pos.get("character", 0))
            if method == "textDocument/definition":
                result = definitions(name)
                if name == "external":
                    result = [{"uri": "jdt://contents/x.class", "range": result[0]["range"]}]
                elif name == "library":
                    result = [{"uri": LIBRARY_CLASS, "range": result[0]["range"]}]
                elif name == "slowlib":
                    result = [
                        {"uri": f"jdt://contents/slow{i}.class", "range": result[0]["range"]}
                        for i in range(3)
                    ]
            elif method == "textDocument/references":
                result = references(name)
            elif method == "textDocument/hover":
                result = (
                    {"contents": {"kind": "markdown", "value": f"**{name}**"}} if name else None
                )
            elif method == "textDocument/documentSymbol":
                result = [
                    {
                        "name": d["uri"] and m.group(1),
                        "kind": 12,
                        "range": {
                            "start": {"line": n, "character": 0},
                            "end": {"line": n, "character": len(line)},
                        },
                        "selectionRange": {
                            "start": {"line": n, "character": line.index(m.group(1))},
                            "end": {"line": n, "character": len(line)},
                        },
                    }
                    for d in [{"uri": uri}]
                    for n, line in enumerate(docs.get(uri, "").split("\n"))
                    for m in [_DEF.match(line)]
                    if m
                ]
            elif method == "java/classFileContents" and params.get("uri") == LIBRARY_CLASS:
                result = LIBRARY_SOURCE
            elif method == "java/classFileContents" and params.get("uri", "").startswith(
                "jdt://contents/slow"
            ):
                # A decompile that never finishes: the client's budget must end it.
                continue
            elif method == "workspace/symbol":
                result = workspace_symbols(params.get("query", ""))
            elif method == "fake/crash":
                os._exit(3)
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "nope"},
                    }
                )
                continue
            send({"jsonrpc": "2.0", "id": message["id"], "result": result})


if __name__ == "__main__":
    main()
