// Click + feedback tracking for deal-watcher emails.
// GET /track?a=<alert id>&e=click|useful|not_useful&s=<signature>
// "click" redirects to the offer; the others show a one-line thank-you page.
// Deploy with JWT verification OFF (links are clicked from an email, not an app).

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const LINK_SECRET = Deno.env.get("LINK_SECRET") ?? "";

const rest = (path: string, init: RequestInit = {}) =>
  fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      "Content-Type": "application/json",
      Prefer: "return=minimal",
      ...(init.headers ?? {}),
    },
  });

async function sign(msg: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(LINK_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg)));
  return Array.from(sig).map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 24);
}

function page(text: string, status = 200): Response {
  const html = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deal watcher</title><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;display:flex;
align-items:center;justify-content:center;height:90vh;margin:0;font-size:20px;color:#111">${text}</body>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });
}

Deno.serve(async (req) => {
  const u = new URL(req.url);
  const a = u.searchParams.get("a") ?? "";
  const e = u.searchParams.get("e") ?? "";
  const s = u.searchParams.get("s") ?? "";
  if (!/^\d+$/.test(a) || !["click", "useful", "not_useful"].includes(e)) return page("Link not recognised", 400);
  if (!LINK_SECRET || s !== await sign(`${a}:${e}`)) return page("Link not recognised", 403);

  const res = await rest(`alerts?id=eq.${a}&select=id,url,clicked_at,feedback`, { headers: { Prefer: "" } });
  const rows = res.ok ? await res.json() : [];
  if (!rows.length) return page("That alert no longer exists", 404);
  const alert = rows[0];
  const now = new Date().toISOString();

  await rest("alert_events", { method: "POST", body: JSON.stringify({ alert_id: Number(a), event: e, at: now }) });
  if (e === "click") {
    if (!alert.clicked_at) {
      await rest(`alerts?id=eq.${a}`, { method: "PATCH", body: JSON.stringify({ clicked_at: now }) });
    }
    const target = alert.url && /^https?:\/\//.test(alert.url) ? alert.url : null;
    return target ? Response.redirect(target, 302) : page("No link for this one");
  }
  await rest(`alerts?id=eq.${a}`, { method: "PATCH", body: JSON.stringify({ feedback: e }) });
  return page(e === "useful" ? "👍 Noted — more like this." : "👎 Noted — fewer like this.");
});
