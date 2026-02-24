/**
 * Cloudflare Worker: passthrough proxy to CSC Go machines API.
 * LOCATION_ID and ROOM_ID are set as Worker secrets (wrangler secret put).
 * No secrets in this file.
 */
const CSC_BASE = "https://mycscgo.com/api/v3";
const CORS_ORIGIN = "https://zhanming-wang.github.io";

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": CORS_ORIGIN,
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
        "Access-Control-Allow-Origin": CORS_ORIGIN,
      },
    });
  },
};
