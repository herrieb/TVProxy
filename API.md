# TVProxy Management API

The versioned management API is available at:

```text
https://tv.berrie.uk/api/v1/
```

It is separate from viewer IPTV tokens. Management requests use:

```http
Authorization: Bearer YOUR_API_TOKEN
```

Create the first token on the Pi. The complete value is printed once only:

```bash
sudo -u tvproxy /opt/tv-proxy/venv/bin/python -m app.cli \
  create-api-token "Main website" --description "Website integration"
```

Interactive documentation is at `/api/docs`; the machine-readable document is
at `/api/openapi.json`.

## Responses

List endpoints use `?page=1&per_page=50` (maximum 200) and return `items` plus
`pagination`. Errors use:

```json
{"error":{"code":"USER_NOT_FOUND","message":"User not found"}}
```

## Endpoints

### API

- `GET /api/v1` returns API information and is public.
- `GET /api/v1/health` returns authenticated health status.

### API tokens

- `GET /api/v1/api-tokens` lists token metadata, never token values.
- `POST /api/v1/api-tokens` creates a token and returns its complete value once.
- `DELETE /api/v1/api-tokens/{id}` revokes a token.

Failed authentication is lightly rate-limited to 20 attempts per minute per
source IP. Video delivery is not subject to this limit.

### Users

- `GET /api/v1/users`
- `POST /api/v1/users` creates a viewer and returns its viewer token once.
- `GET /api/v1/users/{id}`
- `PATCH /api/v1/users/{id}`
- `DELETE /api/v1/users/{id}`
- `POST /api/v1/users/{id}/regenerate-token` returns a new viewer token once.

User updates support `name`, `description`, `allowed_channel_ids`,
`max_connections`, `expires_at`, and `disabled`.

### Channels

- `GET /api/v1/channels`
- `POST /api/v1/channels`
- `GET /api/v1/channels/{id}`
- `PATCH /api/v1/channels/{id}`
- `DELETE /api/v1/channels/{id}`
- `POST /api/v1/channels/{id}/test`

### Connections

- `GET /api/v1/connections`
- `POST /api/v1/connections`
- `GET /api/v1/connections/{id}`
- `PATCH /api/v1/connections/{id}`
- `DELETE /api/v1/connections/{id}`
- `POST /api/v1/connections/{id}/test`

Connection responses omit authorization headers and remove URL query
credentials. Channel responses replace absolute upstream paths with
`[configured]`, because provider credentials may be embedded in path segments.
Upstream URL validation and the existing SSRF allowlist remain enforced.

### Sessions and read-only data

- `GET /api/v1/sessions`
- `DELETE /api/v1/sessions/{id}` terminates an active viewer session.
- `GET /api/v1/logs`
- `GET /api/v1/system/status`

### Settings

- `GET /api/v1/settings`
- `PATCH /api/v1/settings`

### Recordings and storage

- `GET /api/v1/storage` returns `total_bytes`, `used_bytes`, `free_bytes`,
  `reserve_bytes`, and `recording_space_available`.
- `GET /api/v1/recordings` lists schedules. Optional `viewer_id` limits results.
- `POST /api/v1/recordings` schedules a recording. Required fields are
  `viewer_id`, `channel_id`, `start_at`, and `end_at`; optional fields are
  `title` and `timezone`. Naive ISO times use the supplied IANA timezone.
- `GET /api/v1/recordings/{id}` returns schedule and processing state.
- `PATCH /api/v1/recordings/{id}` changes title or a scheduled time.
- `DELETE /api/v1/recordings/{id}` deletes a non-active schedule.
- `POST /api/v1/recordings/{id}/retry` retries an `upload_failed` recording.

The worker records MPEG-TS with FFmpeg stream copy, uploads to the configured
OneDrive `rclone` remote, verifies the upload, and deletes the local file only
after verification. Configure the remote with `TVPROXY_ONEDRIVE_REMOTE` and
enable `tv-proxy-recorder.service` after rclone OAuth setup.

Only known non-secret settings are accepted.

## Request and Response Details

All JSON requests require `Content-Type: application/json`. All protected
requests require the bearer header. IDs are decimal integers. Timestamps are
Unix seconds. Successful list responses have this shape:

```json
{"items": [], "pagination": {"page": 1, "per_page": 50, "total": 0, "pages": 0}}
```

### API token examples

Create a management token:

```bash
curl -X POST https://tv.berrie.uk/api/v1/api-tokens \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"name":"Dashboard integration","description":"Website access"}'
```

The response includes `token` exactly once and `token_info` containing id,
name, description, prefix, enabled state, expiry, creation time, and last-use
time. `GET /api-tokens` never includes the token or its hash.

### User example

```json
POST /api/v1/users
{"name":"Alice","description":"Main viewer","allowed_channel_ids":[12,13],"max_connections":2}
```

The `201` response includes a one-time `viewer_token`. User reads omit it.
`allowed_channel_ids: []` means all enabled channels. `disabled: true` blocks
stream access without deleting the user.

### Channel example

```json
POST /api/v1/channels
{"slug":"news-1","display_name":"News 1","connection_id":2,"upstream_path":"/live/news.m3u8","enabled":true}
```

The response contains channel metadata and health-check fields. Absolute
upstream paths are returned as `[configured]` so provider credentials cannot
leak through the API.

### Connection example

```json
POST /api/v1/connections
{"name":"Provider","base_url":"https://provider.example/list.m3u8","allowed_host":"provider.example","auth_header":"Bearer PROVIDER_SECRET","timeout_connect":5,"timeout_read":30}
```

`auth_header` is write-only. Connection reads return `auth_configured: true`
instead of the secret. `POST /connections/{id}/test` returns `status`,
`http_status`, and `response_time_ms`.

### Test response

```json
{"status":"online","http_status":200,"response_time_ms":82,"playlist_type":"hls"}
```

Channel tests additionally detect `mpegts` for direct MPEG-TS streams.

### Settings request

```json
PATCH /api/v1/settings
{"public_url":"https://tv.berrie.uk","default_max_connections":"1"}
```

Unknown keys are rejected with `422`; environment variables and secrets cannot
be edited through this endpoint.

### Common errors

`401` means the bearer token is missing, invalid, disabled, or expired.
`404` means the requested resource does not exist. `409` means the operation
conflicts with existing data, such as deleting a connection still referenced
by channels. `422` means the JSON fields failed validation. `429` means the
source exceeded the failed-authentication limit.

## Security

- Viewer stream tokens and management API tokens are different systems.
- API tokens are stored as SHA-256 hashes and are never returned after creation.
- Viewer tokens are omitted from normal user reads.
- Upstream authorization headers and credentials are omitted from API output.
- No query-string authentication is accepted by the management API.
- CORS is allowlist-only. Set `API_ALLOWED_ORIGINS` to a comma-separated list
  of website origins when browser calls are needed; server-to-server requests
  do not require CORS.
- Streaming routes remain unchanged and continue using viewer tokens.
