"""Reference backend handler for KiroCrew One-Click Deploy.

Copy this as your app's `api/index.py` starting point. It runs on AWS Lambda
behind an API Gateway HTTP API (payload format 2.0), served at /<slug>/api/*.

CONTRACT (read this):
- Route on the path AFTER "/api/":  event["rawPath"].split("/api/", 1)[1]
- HTTP method:                      event["requestContext"]["http"]["method"]
- Request body:                     use _body(event) below. DO NOT json.loads
  event["body"] directly — API Gateway HTTP API base64-encodes the body when the
  Content-Type is not a recognized text type (or the header is absent, e.g. a
  curl -d without -H 'content-type: application/json'). You MUST decode
  isBase64Encoded first or those POSTs will 500. (Browsers sending
  application/json are unaffected, so a browser smoke test won't catch it.)
- Response:                         {"statusCode": int, "headers": {...},
                                     "body": json.dumps(payload)}
- Stateful apps (deployed with --table): the DynamoDB table name is in
  os.environ["TABLE_NAME"]; boto3 is preinstalled in the Lambda runtime.
"""
import base64
import json
import os  # noqa: F401  (TABLE_NAME available when deployed with --table)


def _body(event):
    """Return the parsed JSON body, transparently decoding base64 when API
    Gateway flagged the payload as binary."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _resp(code, payload):
    return {
        "statusCode": code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


def handler(event, context):
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    route = path.split("/api/", 1)[1].strip("/") if "/api/" in path else ""
    body = _body(event)

    # ---- your routes go here ----
    if route == "ping":
        return _resp(200, {"ok": True, "method": method, "echo": body})

    return _resp(200, {"service": "example", "routes": ["/api/ping"]})
