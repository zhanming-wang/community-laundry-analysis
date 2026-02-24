# Laundry API proxy (Cloudflare Worker)

Passthrough proxy so the dashboard can fetch live machine data from the browser. Secrets stay in Cloudflare.

## 1. Create a Cloudflare account

Go to [cloudflare.com](https://cloudflare.com) → sign up (free).

## 2. Install Wrangler and login

```bash
npm install -g wrangler
wrangler login
```

This opens a browser window — authorize it.

## 3. Deploy the worker

```bash
cd worker
wrangler deploy
```

It'll output your worker URL — something like `https://laundry.zhanming-wang.workers.dev`.

## 4. Set your secrets

```bash
wrangler secret put LOCATION_ID
# paste your LOCATION_ID when prompted

wrangler secret put ROOM_ID
# paste your ROOM_ID when prompted
```

## 5. Test it works

```bash
curl -s "https://laundry.YOUR_SUBDOMAIN.workers.dev/machines" | python3 -m json.tool | head -20
```

Use your actual worker URL from step 3.

## 6. Update the dashboard

In `docs/index.html`, set `WORKER_URL` to your worker machines endpoint:

```javascript
var WORKER_URL = 'https://laundry.YOUR_SUBDOMAIN.workers.dev/machines';
```

Then commit and push. The free tier gives you 100k requests/day.
