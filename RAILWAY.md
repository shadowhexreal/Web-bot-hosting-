# Deploy SHADOW HOSTING on Railway

## 1. Upload the code
Push this folder to a GitHub repository (or use the Railway CLI: `railway up`).
Railway finds the `Dockerfile` by itself. It installs Python, **Node.js** and **cloudflared** for you.

## 2. Create the service
Railway dashboard -> New Project -> Deploy from GitHub repo -> pick the repository.

## 3. Add a Volume (important)
Service -> Settings -> Volumes -> Add Volume -> mount path: `/data`
Without a volume every deploy erases users, servers and uploaded files.

## 4. Add variables (Service -> Variables)
| Variable | What for |
|---|---|
| `ADMIN_PASSWORD` | **Required.** At least 8 characters. Becomes the admin password on first start. |
| `SECRET_KEY` | Optional. A long random text. If you skip it, one is created and kept on the volume. |
| `NODE_MAJOR` | Optional build argument (default 22). |

## 5. Open the site
Service -> Settings -> Networking -> Generate Domain.
Admin login: `https://YOUR-DOMAIN/login` (user `admin`, password = `ADMIN_PASSWORD`).
In the admin panel open **Runtimes**: Node.js and Cloudflare tunnel should both say *Ready*.

## Good to know
- Keep **1 replica** only. Running bots, live links and rate limits live in memory.
- After every deploy/restart, servers that were running are started again automatically.
  Live Cloudflare website links are not restored; users press *Generate link* again.
- All variables you add to this service are visible to code running inside it. Do not add other secrets here.
- Railway has its own Acceptable Use Policy (it restricts tunneling/proxy services and reselling compute).
  Read it before opening the platform to the public: https://railway.com/legal/acceptable-use
