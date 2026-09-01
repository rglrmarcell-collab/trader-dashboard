// Trader Dashboard -> Notion logger (Vercel serverless function)
//
// SZUKSEGES KORNYEZETI VALTOZOK (Vercel > Project > Settings > Environment Variables):
//   NOTION_TOKEN  = a Notion belso integracio tokene (titkos, sosem kerul a repoba)
//   LOG_SECRET    = sajat jelszo, amit egyszer beirsz a telefonon
//
// A Notion integraciot meg kell osztani az "Elet Log" oldallal ES a "Daily Notes" oldallal,
// kulonben a Notion 404-et ad vissza.

const NOTION = "https://api.notion.com/v1";
const DAILY_NOTES = "36eac560704880469890e907b6f383d9";
const DB = {
  munka:   "875ff67011f84dfc918de23b4eb2b8d7",
  suly:    "888a7fb60fb84e33a504356f321c77ad",
  checkin: "6b3bfc9c1ae1458192603bbc72b19aa3"
};

const hdr = () => ({
  "Authorization": "Bearer " + process.env.NOTION_TOKEN,
  "Notion-Version": "2022-06-28",
  "Content-Type": "application/json"
});

const txt = (v) => (v === undefined || v === null || v === "") ? [] : [{ type: "text", text: { content: String(v).slice(0, 1900) } }];
const num = (v) => (v === undefined || v === null || v === "" || isNaN(Number(v))) ? null : Number(v);
const sel = (v) => v ? { select: { name: String(v) } } : undefined;
const today = () => new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Budapest" });

// "2026-09-01" (vagy ma) -> "Tuesday, 1 September 2026" — pontosan ez all a Daily Notes napi calloutjaban.
function dayHeader(iso) {
  const dt = iso ? new Date(String(iso) + "T12:00:00Z") : new Date();
  if (isNaN(dt.getTime())) throw new Error("Ervenytelen datum: " + iso);
  const p = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Budapest", weekday: "long", day: "numeric", month: "long", year: "numeric"
  }).formatToParts(dt);
  const g = (t) => (p.find((x) => x.type === t) || {}).value || "";
  return g("weekday") + ", " + g("day") + " " + g("month") + " " + g("year");
}

const clock = () => new Intl.DateTimeFormat("hu-HU", {
  timeZone: "Europe/Budapest", hour: "2-digit", minute: "2-digit", hour12: false
}).format(new Date());

const para = (s) => ({ object: "block", type: "paragraph", paragraph: { rich_text: txt(s) } });

const plain = (rt) => (Array.isArray(rt) ? rt : []).map((x) => (x && x.plain_text) || "").join("").trim();

// Vegigjarja a Daily Notes lap blokkjait, es megkeresi azt a calloutot,
// aminek az elso sora a keresett nappal kezdodik. Hatulrol elore keres:
// a mai nap a lap vegen van, igy altalaban az elso talalat.
async function findDayCallout(target) {
  const blocks = [];
  let cursor = null;
  for (let i = 0; i < 25; i++) {
    const url = NOTION + "/blocks/" + DAILY_NOTES + "/children?page_size=100" + (cursor ? "&start_cursor=" + cursor : "");
    const r = await fetch(url, { headers: hdr() });
    const j = await r.json();
    if (!r.ok) return { error: j.message || "Notion hiba a lap olvasasakor" };
    blocks.push.apply(blocks, j.results || []);
    if (!j.has_more) break;
    cursor = j.next_cursor;
  }
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i];
    if (b.type !== "callout") continue;
    if (plain(b.callout && b.callout.rich_text).indexOf(target) === 0) return { id: b.id };
  }
  return { id: null };
}

export default async function handler(req, res) {
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST") return res.status(405).json({ error: "Csak POST" });

  if (!process.env.NOTION_TOKEN) return res.status(500).json({ error: "NOTION_TOKEN nincs beallitva a Vercelben" });
  if (!process.env.LOG_SECRET)   return res.status(500).json({ error: "LOG_SECRET nincs beallitva a Vercelben" });

  let body = req.body;
  if (typeof body === "string") { try { body = JSON.parse(body || "{}"); } catch (e) { body = {}; } }
  body = body || {};

  if (body.secret !== process.env.LOG_SECRET) return res.status(401).json({ error: "Rossz jelszo" });

  const d = body.data || {};
  const date = d.datum || today();

  try {
    // --- Gyorsjegyzet -> a Daily Notes ADOTT NAPI blokkjaba ---
    // A nap fejlece angolul all a callout elso soraban: "Tuesday, 1 September 2026".
    // Napkozben barmennyi jegyzet mehet: mind ugyanabba a napi calloutba kerul, idobelyeggel.
    if (body.kind === "note") {
      const szoveg = String(d.szoveg || "").trim();
      if (!szoveg) return res.status(400).json({ error: "Ures jegyzet" });

      const target = dayHeader(d.datum);              // pl. "Tuesday, 1 September 2026"
      const ido = clock();                            // pl. "14:32"
      const sor = "📥 [" + ido + "] " + szoveg;

      const hit = await findDayCallout(target);
      if (hit.error) return res.status(502).json({ error: hit.error });

      if (hit.id) {
        const r = await fetch(NOTION + "/blocks/" + hit.id + "/children", {
          method: "PATCH",
          headers: hdr(),
          body: JSON.stringify({ children: [para(sor)] })
        });
        const j = await r.json();
        if (!r.ok) return res.status(502).json({ error: j.message || "Notion hiba" });
        return res.status(200).json({ ok: true, hova: target, mod: "napi-blokk" });
      }

      // Nem talaltam a napi blokkot. NEM hozunk letre uj napot (duplikatum-veszely),
      // hanem a lap vegere irjuk, es ezt meg is mondjuk - sosem tunhet el csendben.
      const r2 = await fetch(NOTION + "/blocks/" + DAILY_NOTES + "/children", {
        method: "PATCH",
        headers: hdr(),
        body: JSON.stringify({ children: [para(sor + "  — (nem talaltam a napi blokkot: " + target + ")")] })
      });
      const j2 = await r2.json();
      if (!r2.ok) return res.status(502).json({ error: j2.message || "Notion hiba" });
      return res.status(200).json({ ok: true, hova: target, mod: "lap-vege", figyelmeztetes: "A " + target + " napi blokkot nem talaltam, a lap vegere irtam." });
    }

    // --- Adatbazis sorok ---
    let props;
    if (body.kind === "munka") {
      props = {};
      props["N\u00e9v"] = { title: txt(date + " \u2013 " + (d.ora || "?") + " \u00f3ra") };
      props["D\u00e1tum"] = { date: { start: date } };
      props["Ledolgozott \u00f3ra"] = { number: num(d.ora) };
      props["\u00d3rab\u00e9r (Ft)"] = { number: num(d.orabar) };
      props["Megjegyz\u00e9s"] = { rich_text: txt(d.megj) };
      if (d.hely) props["Hely"] = sel(d.hely);
    } else if (body.kind === "suly") {
      props = {};
      props["N\u00e9v"] = { title: txt(date + " \u2013 " + (d.suly || "?") + " kg") };
      props["D\u00e1tum"] = { date: { start: date } };
      props["S\u00faly (kg)"] = { number: num(d.suly) };
      props["Megjegyz\u00e9s"] = { rich_text: txt(d.megj) };
    } else if (body.kind === "checkin") {
      props = {};
      props["N\u00e9v"] = { title: txt(date + " \u2013 check-in") };
      props["D\u00e1tum"] = { date: { start: date } };
      props["Alv\u00e1s (\u00f3ra)"] = { number: num(d.alvas) };
      props["Jegyzet"] = { rich_text: txt(d.jegyzet) };
      if (d.hangulat) props["Hangulat"] = sel(d.hangulat);
      if (d.energia)  props["Energia 1-5"] = sel(d.energia);
      if (Array.isArray(d.terulet) && d.terulet.length)
        props["Mir\u0151l sz\u00f3l"] = { multi_select: d.terulet.map(function (t) { return { name: String(t) }; }) };
    } else {
      return res.status(400).json({ error: "Ismeretlen kind: " + body.kind });
    }

    const r = await fetch(NOTION + "/pages", {
      method: "POST",
      headers: hdr(),
      body: JSON.stringify({ parent: { database_id: DB[body.kind] }, properties: props })
    });
    const j = await r.json();
    if (!r.ok) return res.status(502).json({ error: j.message || "Notion hiba" });
    return res.status(200).json({ ok: true, hova: body.kind, url: j.url });
  } catch (e) {
    return res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
}
