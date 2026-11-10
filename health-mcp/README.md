# health-mcp

Pixel Watch / Health Connect data on your own server, readable by Claude.

    phone (HC Webhook) --POST /ingest--> this container (SQLite) <--MCP + OIDC login-- Claude

Data types stored: steps, heart rate, sleep, weight, blood pressure, nutrition, workouts, calories
burned. Manual logging through chat: blood pressure, meals, weight.

## Install (once)
1. `mkdir -p /mnt/user/appdata/health-mcp/data && cd /mnt/user/appdata/health-mcp`
2. Save `docker-compose.yml` and `.env.example` from this folder there; `cp .env.example .env`.
3. Fill in `.env`: `INGEST_TOKEN` and `OIDC_CLIENT_SECRET` with `openssl rand -hex 32`, plus your URLs.
   `chmod 600 .env && chown 1000:1000 data && chmod 700 data`
4. `docker compose up -d` and check `curl localhost:9102/healthz`.

Updates after that are automatic: see "Automatic updates" in the repository README.

## Reverse proxy (Pangolin)
Public resource to `http://<unraid-ip>:9102`. **Turn off the proxy's own SSO for this resource**: the app
does its own login (OIDC for `/mcp`, the ingest key for `/ingest`). Health check: GET `/healthz`, expect 200.
If you run CrowdSec, allowlist Anthropic's outbound ranges (they call your server for MCP):
https://platform.claude.com/docs/en/api/ip-addresses

## Authelia client
```yaml
      - client_id: 'health-mcp'
        client_name: 'Health MCP'
        client_secret: '<pbkdf2 digest of OIDC_CLIENT_SECRET>'   # authelia crypto hash generate pbkdf2
        public: false
        authorization_policy: 'two_factor'
        consent_mode: 'implicit'
        redirect_uris:
          - 'https://claude.ai/api/mcp/auth_callback'
        audience:                                   # Claude sends resource=<PUBLIC_URL>/mcp and Authelia
          - 'https://health.example.com/mcp'        # only allows resources listed here
        requested_audience_mode: 'implicit'
        access_token_signed_response_alg: 'RS256'
        scopes: ['openid', 'profile', 'email', 'address', 'phone', 'groups', 'offline_access']
        response_types: ['code']
        grant_types: ['authorization_code', 'refresh_token']
        token_endpoint_auth_method: 'client_secret_post'   # what Claude uses
```
Lessons from the first setup: Claude requests every scope Authelia advertises, and sends the client secret
in the request body (`client_secret_post`). `OIDC_AUDIENCE` in `.env` must be the same URL as the audience above.

## Claude connector
Settings, Connectors, Add custom connector: URL `https://health.example.com/mcp`; under Advanced enter client id
`health-mcp` and the plain client secret. After an update that adds or changes tools, disconnect and reconnect the
connector (or start a new chat) so Claude refreshes its tool list.

## Phone
HC Webhook: URL `https://health.example.com/ingest`, header `x-api-key: <INGEST_TOKEN>`, 15 minute interval,
battery optimisation off. Enable every data type above and grant the Health Connect permissions. Do **not** press
the app's test button: it inserts sample records. If `?key=` in the URL is the only option the app offers,
set `ALLOW_QUERY_KEY=true` (weaker: the key then appears in proxy logs).
Each sync logs the types it contained, plus any top-level key the server does not recognise.

## Development
    pip install -r requirements.txt -r requirements-dev.txt
    pytest tests && python tests/e2e.py
