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

## Wellness web app (optional)
A small web app for blood pressure, a daily coach page and quick logging, for one or more people. Each person
gets their own database, and signs in with their own Authelia account. It stays off until `WEB_PUBLIC_URL` is set.

The app is built for phones. Today shows rings for steps and sleep against your goals, coach notes and tiles
for heart rate, blood pressure, weight, food and workouts. Tap a tile for a detail page with a 7, 30 or 90 day
chart, stats, notes and the entries behind them. Goals are set under Settings. To install it, open the site in
Chrome on Android (menu, Add to home screen / Install app) or in Safari on iPhone (Share, Add to Home Screen).

Add to `.env`:

    WEB_PUBLIC_URL=https://wellness.example.com
    WEB_OIDC_CLIENT_ID=wellness-web
    WEB_OIDC_CLIENT_SECRET=<openssl rand -hex 32>
    WEB_SESSION_SECRET=<openssl rand -hex 32>
    WEB_USERS=khushal:Khushal,shivani:Shivani     # Authelia usernames (lower case) : display name
    WEB_DEFAULT_USER=khushal                      # keeps using the existing health.db; others get users/<name>.db

`OIDC_ISSUER` is shared with the MCP setup. Then `docker compose up -d` (the compose file publishes the app on port 9103).

Pangolin: a second public resource `wellness.example.com` to `http://<unraid-ip>:9103`, with the proxy's own SSO **off**
(the app does the login itself). Authelia client:
```yaml
      - client_id: 'wellness-web'
        client_name: 'Wellness'
        client_secret: '<pbkdf2 digest of WEB_OIDC_CLIENT_SECRET>'
        public: false
        authorization_policy: 'one_factor'
        consent_mode: 'implicit'
        redirect_uris: ['https://wellness.example.com/callback']
        scopes: ['openid', 'profile']
        response_types: ['code']
        grant_types: ['authorization_code']
        token_endpoint_auth_method: 'client_secret_post'
        pkce_challenge_method: 'S256'
```
A person not listed in `WEB_USERS` is refused even if Authelia lets them sign in.

**GP report** (`/gp`): a printable home blood pressure record (averages with and without the first day, morning and
evening averages, highest and lowest, readings by day, completeness, a free-text context note). Use the browser's
Print or Save as PDF.

**Reminders** (Settings): morning and evening blood pressure reminders as push notifications through
[ntfy](https://ntfy.sh). Each person gets a private random topic and subscribes to it in the ntfy phone app. A
reminder is sent once per slot per day, only within three hours after its time, and is skipped when a reading has
already been logged in that half of the day. Messages never contain health data. Set `NTFY_BASE` in `.env` to use
your own ntfy server (default `https://ntfy.sh`).

**Apple Health import** (Settings, Import a file): upload the .zip or .json made by the Health Auto Export app. It adds
steps, sleep with stages, daily heart rate summaries, weight, workouts and calories to the signed-in person's own
record. Importing an overlapping export again is safe. The export holds daily summaries, so there is no
minute-by-minute heart rate for Apple data.

## Mealie (optional)
Set `MEALIE_URL` and `MEALIE_API_TOKEN` in `.env` (token from Mealie, User profile, API tokens) and restart.
The Health connector then also offers `mealie_status`, `search_recipes`, `get_recipe` (with nutrition per
serving), `get_meal_plan`, `add_to_meal_plan` and `log_recipe_as_meal` (scales the recipe's own nutrition by
the servings eaten and writes it to the food log). The token is only sent to `MEALIE_URL` and is never
returned by a tool. Also: `recipes_missing_nutrition` and `set_recipes_nutrition` (fill in per-serving nutrition in bulk, read back to verify),
`add_meals_to_plan`, `remove_meal_plan_entry`, `get_shopping_lists` and `add_recipes_to_shopping_list`. Recipes without nutrition filled in are not logged automatically. Needs Mealie v2 or later.

More Mealie tools, so common chores need no manual work in the Mealie app:
- Shopping lists: `create_shopping_list`, `delete_shopping_list` (needs the exact name), `add_shopping_items`, `update_shopping_items` (tick, untick or remove by id or by words, for example pantry staples), `clear_checked_shopping_items`. `get_shopping_lists(include_items=true)` reads the real items.
- Recipes: `create_recipe`, `update_recipe` (servings, ingredients, steps), `import_recipe_from_url`, `set_recipe_tags`, `find_duplicate_recipes`, `find_recipes_by_nutrition`, `recipes_without_ingredients`, `delete_recipes` (refuses recipes that have ingredients unless told otherwise).
- Meal plan: `get_plan_nutrition` (calories, protein and salt per meal and per day, fasting days marked), `move_meal_plan_entry`, `clear_meal_plan`, `log_planned_meal` (log a planned meal as eaten).
Writes that Mealie could silently ignore are read back and reported if they did not save.

Health settings tools (always on): `get_settings`, `set_goals`, `set_fasting_days`, `set_gp_note`.

## Development
    pip install -r requirements.txt -r requirements-dev.txt
    pytest tests && python tests/e2e.py
