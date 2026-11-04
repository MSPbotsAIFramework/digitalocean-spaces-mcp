# digitalocean-spaces-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes [DigitalOcean
Spaces](https://www.digitalocean.com/products/spaces) (S3-compatible object
storage) as tools for AI agents: list, create, and delete buckets; list,
read, write, delete, and copy objects; and mint short-lived pre-signed URLs
for direct GET/PUT transfers.

- **Transport**: MCP Streamable HTTP (`POST /mcp`), stateless — no session
  affinity required between requests.
- **Stack**: Python 3.12, [uv](https://docs.astral.sh/uv/), the official
  [`mcp`](https://pypi.org/project/mcp/) SDK's `FastMCP`, and
  [`boto3`](https://pypi.org/project/boto3/) for SigV4-signed S3-compatible
  calls.
- **License**: Apache-2.0. Copyright MSPbots.ai.

---

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

Every `tools/call` request must carry all three headers below. The Gateway
injects the calling tenant's own values; missing any one of them returns
HTTP 401 before any call reaches DigitalOcean.

| Header | Required | Meaning | Where to obtain it |
|---|---|---|---|
| `X-DigitalOcean-Spaces-Access-Key` | Yes | Spaces access key | DigitalOcean control panel → **API** → **Spaces Keys** → Generate New Key |
| `X-DigitalOcean-Spaces-Secret-Key` | Yes | Spaces secret key, issued once alongside the access key | Same screen — copy it when the key pair is created; DigitalOcean does not show it again |
| `X-DigitalOcean-Spaces-Region` | Yes | Region slug of the Space being addressed, e.g. `nyc3`, `sfo3`, `ams3`, `fra1`, `sgp1`, `syd1`, `blr1`, `tor1`, `atl1`, `lon1` | Shown next to each Space in the control panel |

Credentials are read only from these headers for the lifetime of one request
— never from an environment variable, a config file, or a cache — so one
tenant's access/secret key pair is never visible to another tenant's
concurrent request.

## Tools

| Tool | Purpose |
|---|---|
| `spaces_list_buckets` | List every bucket owned by the account |
| `spaces_create_bucket` | Create a bucket |
| `spaces_delete_bucket` | Delete an empty bucket |
| `spaces_list_objects` | List objects in a bucket, with prefix filtering and pagination |
| `spaces_get_object` | Read a small object's content, or get a presigned-URL hint for large/binary objects |
| `spaces_put_object` | Write an object (UTF-8 text or base64, under the size cap) |
| `spaces_delete_objects` | Delete one or many objects in one call |
| `spaces_copy_object` | Copy an object within the same region |
| `spaces_get_object_metadata` | Get an object's size, content type, ETag, and last-modified time without transferring its body |
| `spaces_create_presigned_url` | Mint a short-lived, pre-signed GET or PUT URL for one object |

## Error format

Every tool returns either its result or this structured envelope — tools
never raise, so the agent can branch on `code`:

```json
{"error": {"code": "not_found", "message": "...", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`,
`invalid_argument`, `rate_limited`, `upstream_error`.

## Known limitations (v1)

- No bucket policies, ACL management beyond a `private`/`public-read`
  choice, CORS, lifecycle rules, static-website hosting, or object
  versioning/tagging.
- No true multipart upload; writes larger than the single-call size cap go
  through `spaces_create_presigned_url` with `method: "put"` instead.
- `spaces_copy_object` only supports same-region copies.
- `spaces_list_buckets`'s `region` field falls back to the requesting
  region when DigitalOcean's response does not carry a per-bucket region
  (botocore's S3 model does not parse that DigitalOcean-specific extension).
- Region values are passed straight to the endpoint URL, not validated
  against a hardcoded list; an invalid region surfaces as an
  `upstream_error` (connection/DNS failure) rather than a friendlier
  `invalid_argument`.

## Local development

```bash
uv sync
uv run pytest
uv run python -m digitalocean_spaces_mcp   # serves on 0.0.0.0:8080
```

Manual end-to-end check (no real DigitalOcean account required for `tools/list`):

```bash
curl -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'X-DigitalOcean-Spaces-Access-Key: <your access key>' \
  -H 'X-DigitalOcean-Spaces-Secret-Key: <your secret key>' \
  -H 'X-DigitalOcean-Spaces-Region: nyc3' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Docker:

```bash
docker build --platform linux/amd64 -t digitalocean-spaces-mcp:dev .
```
