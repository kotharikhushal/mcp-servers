# mcp-servers

Personal MCP servers, one folder each, each published as a Docker image to GitHub's container registry.

| Folder | What it does | Image |
|---|---|---|
| [`health-mcp`](health-mcp) | Pixel Watch / Health Connect data (steps, heart rate, sleep, weight, blood pressure, food, workouts) for Claude | `ghcr.io/kotharikhushal/health-mcp` |

## How changes reach the server
1. Claude opens a pull request. CI runs the unit tests and an end-to-end test (real server, mock login provider, real MCP client).
2. You review and **merge** it (the GitHub mobile app is enough).
3. The merge to `main` builds and publishes the image (`:latest` and `:<commit sha>`).
4. Unraid pulls the new image within minutes (below) and restarts the container.
5. If tools changed, disconnect and reconnect the connector in Claude so it refreshes its tool list.

Nothing reaches your server without your merge. Recommended: in the repo settings, add a branch
protection rule for `main` that requires a pull request, so even a direct push can't deploy.

## Automatic updates on Unraid
Install the **User Scripts** plugin, add a script scheduled with the custom cron `*/5 * * * *`:

```bash
#!/bin/bash
# Pull new images and recreate only the containers whose image changed.
for d in /mnt/user/appdata/health-mcp; do
  cd "$d" && docker compose pull -q && docker compose up -d
done
```

`docker compose up -d` leaves a container alone when its image has not changed. Add each new server's
folder to the list. This avoids giving a third-party updater access to the Docker socket.
To roll back, pin the compose file to an earlier `:<commit sha>` tag and run `docker compose up -d`.

## First publish
The image is built by the first merge to `main`. The `org.opencontainers.image.source` label links the
package to this repo, so it takes the repo's visibility. Check GitHub, Packages, `health-mcp`,
Package settings: for an anonymous pull on Unraid it must be **Public**. The image contains only code,
never secrets (those live in the server's `.env`). If you prefer it private, run `docker login ghcr.io`
on Unraid with a token that has only `read:packages`.

## Adding another server
Copy a folder's layout (`Dockerfile`, `requirements.txt`, `tests/`, compose file), add a small workflow
like `.github/workflows/health-mcp.yml` calling `_server.yml` with the folder name.
