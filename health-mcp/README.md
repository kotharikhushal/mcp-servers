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

### Claude connector for two people

Every data tool takes an optional `person` (a `WEB_USERS` name such as `shivani`). Leave it empty and you get the default person's data. This is how two people can share one Claude account: whoever is asking has Claude pass their name. It is a convenience, not a security boundary, because anyone with the connector can name anyone.

If people have separate Claude accounts, you can also link each person's Authelia sign-in (the `sub`, username or email in their token; separate several ids with `|`) so the right database is chosen automatically:

    MCP_USERS=khushal:khushal@example.com,shivani:shivani@example.com

A sign-in that is not listed gets an error showing the id to add. Phone sync (`/ingest`) still feeds the default person only; other people add data through the app or the Apple Health import.

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

**One Mealie account per person.** `MEALIE_API_TOKEN` belongs to the default person. Give anyone else their own with `MEALIE_TOKEN_<NAME>` (for example `MEALIE_TOKEN_BUNNY`), created while signed in to Mealie as them. Meal plans and shopping lists belong to the household of the token's owner, so people in different households each see their own plan, while recipes stay shared across the group. The connector tools and the web app both use the token of whoever is asking; a person with no token gets a clear message instead of someone else's plan. Replace the token in the environment file and recreate the container to change it.
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

Health settings tools (always on; `set_goals` also sets the optional daily calorie, protein and salt targets the app shows as "left today"): `get_settings`, `set_goals` (including goal weight), `set_fasting_days`, `set_fasting_weekdays`, `set_gp_note`, `set_ai_analysis` (saves the analysis card shown at the top of the Today page; written by Claude on request, per person).

When `MEALIE_URL` and `MEALIE_API_TOKEN` are set, the wellness app's home screen shows today's Mealie meal plan with one-tap "I ate this" (logs the recipe's own nutrition) and Skip.

## Development
    pip install -r requirements.txt -r requirements-dev.txt
    pytest tests && python tests/e2e.py


## AI analysis from a local model (optional)

Set `OLLAMA_URL` (for example `http://192.168.0.202:9068`) and, if you like, `OLLAMA_MODEL` (default `gemma4:12b`)
and the Today page shows a **Refresh analysis** button. The app works out every figure and the blood-pressure
comparison itself, asks the model only to write the wording, and checks the answer before saving it: valid
shape, no number that is not in the facts, no diagnosis or medication wording, no wrong or alarming
blood-pressure wording, and thin data admitted as thin. A failed answer is retried once, then dropped and the
earlier analysis is kept. One refresh per person per hour. Ollama is called from the server, so it only needs to be
reachable from the app container (do not expose Ollama to the internet: it has no login). Without `OLLAMA_URL`
the analysis is written by Claude when asked ("refresh wellness") through `set_ai_analysis`.

## Chat (optional, same local model)

With `OLLAMA_URL` set, a **Chat** tab appears. You can ask about your numbers, today's food and today's Mealie plan,
and ask it to log things: weight, blood pressure, a meal with kcal, a Mealie recipe, or items for your Mealie shopping
list. The model never writes data. It proposes one action, the app checks it (known kind, sane ranges, every number
must be one you typed), and it waits as a **Confirm / Cancel** card for 30 minutes. Nothing is saved until you press
Confirm. There is no delete or meal-plan editing in chat. Replies get the same checks as the analysis (no invented
numbers, no diagnosis or medication wording, blood pressure only with the GP). The last 20 messages are kept per person
and can be cleared; they are only sent back to the model as context, and the model is never trained by them.
If you give no calories for a meal, the model proposes an estimate: the confirm card shows the kcal in an editable box, the name is saved with "(estimate)", and a meal you logged before is filled in from your own earlier entry instead of a guess.
Meal planning stays with Claude through the connector.
