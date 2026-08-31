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
    // --- Gyorsjegyzet -> Daily Notes vegere ---
    if (body.kind === "note") {
      const stamp = new Date().toLocaleString("hu-HU", { timeZone: "Europe/Budapest" });
      const r = await fetch(NOTION + "/blocks/" + DAILY_NOTES + "/children", {
        method: "PATCH",
        headers: hdr(),
        body: JSON.stringify({
          children: [{ object: "block", type: "paragraph", paragraph: { rich_text: txt("[" + stamp + "] " + (d.szoveg || "")) } }]
        })
      });
      const j = await r.json();
      if (!r.ok) return res.status(502).json({ error: j.message || "Notion hiba" });
      return res.status(200).json({ ok: true, hova: "Daily Notes" });
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
