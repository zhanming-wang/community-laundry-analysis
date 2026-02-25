/**
 * Cloudflare Worker: passthrough proxy to CSC Go machines API.
 * LOCATION_ID and ROOM_ID are set as Worker secrets (wrangler secret put).
 * No secrets in this file.
 */
const CSC_BASE = "https://mycscgo.com/api/v3";
const ALLOWED_ORIGINS = [
  "https://zhanming-wang.github.io",
  "http://localhost:8080",
  "http://127.0.0.1:8080",
];

function corsOrigin(origin) {
  if (origin && ALLOWED_ORIGINS.includes(origin)) return origin;
  if (origin && /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)) return origin;
  return ALLOWED_ORIGINS[0];
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const allowOrigin = corsOrigin(origin);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": allowOrigin,
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Access-Control-Max-Age": "86400",
        },
      });
    }

    if (request.method !== "GET") {
      return new Response("Method not allowed", { status: 405 });
    }

    const url = new URL(request.url);
    if (!url.pathname.endsWith("/machines")) {
      return new Response("Not found", { status: 404 });
    }

    const loc = env.LOCATION_ID;
    const room = env.ROOM_ID;
    if (!loc || !room) {
      return new Response("Server configuration error", { status: 500 });
    }

    const apiUrl = `${CSC_BASE}/location/${loc}/room/${room}/machines`;
    const res = await fetch(apiUrl, { cf: { cacheTtl: 0 } });
    const body = await res.text();

    return new Response(body, {
      status: res.status,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": allowOrigin,
      },
    });
  },
};
