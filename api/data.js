// Trader Dashboard <- Notion (Vercel serverless function)
//
// KORNYEZETI VALTOZOK (Vercel > Settings > Environment Variables):
//   NOTION_TOKEN  = Notion belso integracio tokene (titkos, sosem kerul a repoba)
//   LOG_SECRET    = ugyanaz a jelszo, mint az api/log-nal
//
// A Notion integraciot meg kell osztani az "Elet Log" oldallal (es alatta
// minden adatbazissal), kulonben a Notion 404-et ad.
//
// FONTOS: ez a vegpont JELSZOT KER. A dashboard maga publikus, de az adat nem:
// jelszo nelkul semmit nem ad vissza.

const NOTION = "https://api.notion.com/v1";
const DB = {
  alvas: "41d05e884af14fd1b07057c0a56f740b",
  lepes: "b9feae97cb744a95af7f4066e1866bad",
  edzes: "1ee1fb78dcaf4d089370d579c9043e01",
  suly:  "888a7fb60fb84e33a504356f321c77ad",
  munka: "875ff67011f84dfc918de23b4eb2b8d7"
};

const hdr = () => ({
  "Authorization": "Bearer " + process.env.NOTION_TOKEN,
  "Notion-Version": "2022-06-28",
  "Content-Type": "application/json"
});


// ---- MAI NAP a Daily Notes-bol (reggeli brief + figyelo korok) ----
const DAILY_NOTES = "36eac560704880469890e907b6f383d9";
// "Figyelo — ma" lap: MINDIG csak a mai napot tartalmazza, a futasok felulirjak.
const WATCH_PAGE = "3d0ac5607048818abc9ff81df709c775";

function dayHeader(){
  const p = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Budapest", weekday: "long", day: "numeric", month: "long", year: "numeric"
  }).formatToParts(new Date());
  const g = (t) => (p.find((x) => x.type === t) || {}).value || "";
  return g("weekday") + ", " + g("day") + " " + g("month") + " " + g("year");
}
const plain = (rt) => (Array.isArray(rt) ? rt : []).map((x) => (x && x.plain_text) || "").join("").trim();

async function children(id){
  const out = []; let cursor = null;
  for (let i = 0; i < 25; i++) {
    const url = NOTION + "/blocks/" + id + "/children?page_size=100" + (cursor ? "&start_cursor=" + cursor : "");
    const r = await fetch(url, { headers: hdr() });
    const j = await r.json();
    if (!r.ok) throw new Error((j && j.message) || ("Notion " + r.status));
    out.push.apply(out, j.results || []);
    if (!j.has_more) break;
    cursor = j.next_cursor;
  }
  return out;
}

// Egy blokk szoveges tartalma, tipustol fuggetlenul.
function blockText(b){
  const t = b && b.type; if (!t) return "";
  const o = b[t]; if (!o) return "";
  return plain(o.rich_text);
}
function isHeading(b){
  return b && (b.type === "heading_1" || b.type === "heading_2" || b.type === "heading_3");
}

// A mai napi callout tartalmat szekciokra bontja (fejlec -> sorok).
async function readToday(){
  const target = dayHeader();
  const top = await children(DAILY_NOTES);
  let day = null;
  for (let i = top.length - 1; i >= 0; i--) {
    const b = top[i];
    if (b.type !== "callout") continue;
    if (plain(b.callout && b.callout.rich_text).indexOf(target) === 0) { day = b; break; }
  }
  if (!day) return { header: target, found: false, sections: [] };

  const kids = day.has_children ? await children(day.id) : [];
  const sections = []; let cur = null;
  for (const b of kids) {
    const s = blockText(b);
    if (isHeading(b)) { cur = { title: s, lines: [] }; sections.push(cur); continue; }
    if (!s) continue;
    if (!cur) { cur = { title: "", lines: [] }; sections.push(cur); }
    if (cur.lines.length < 40) cur.lines.push(s);
  }
  return { header: target, found: true, sections };
}

// A "Figyelo — ma" lap tartalma. Nem no: minden nap felulirodik.
async function readWatch(){
  const kids = await children(WATCH_PAGE);
  const sections = []; let cur = null;
  for (const b of kids) {
    // A lap tetejen allo magyarazat (callout VAGY idezet) nem tartalom.
    // A Notion a ">" markdownt QUOTE blokka alakitja, nem calloutta — 2026-09-03, elesben derult ki.
    if (b.type === "callout" || b.type === "quote") continue;
    const s = blockText(b);
    if (isHeading(b)) { cur = { title: s, lines: [] }; sections.push(cur); continue; }
    if (!s) continue;
    if (!cur) { cur = { title: "", lines: [] }; sections.push(cur); }
    if (cur.lines.length < 40) cur.lines.push(s);
  }
  return sections;
}

const pDate  = (p) => (p && p.date && p.date.start) ? String(p.date.start).slice(0,10) : null;
const pNum   = (p) => (p && typeof p.number === "number") ? p.number : null;
const pText  = (p) => { if(!p) return ""; const a = p.rich_text || p.title; return Array.isArray(a) ? a.map(t=>t.plain_text).join("") : ""; };
const pCheck = (p) => !!(p && p.checkbox);

async function q(dbId, sortProp) {
  const body = { page_size: 100 };
  if (sortProp) body.sorts = [{ property: sortProp, direction: "descending" }];
  const r = await fetch(NOTION + "/databases/" + dbId + "/query", {
    method: "POST", headers: hdr(), body: JSON.stringify(body)
  });
  if (!r.ok) { const e = await r.json().catch(()=>({})); throw new Error((e && e.message) || ("Notion " + r.status)); }
  const j = await r.json();
  return (j.results || []).map(x => x.properties || {});
}

export default async function handler(req, res) {
  if (!process.env.NOTION_TOKEN) return res.status(500).json({ error: "NOTION_TOKEN nincs beallitva a Vercelben" });
  if (!process.env.LOG_SECRET)   return res.status(500).json({ error: "LOG_SECRET nincs beallitva a Vercelben" });

  const given = (req.query && req.query.s) || req.headers["x-log-secret"] || "";
  if (given !== process.env.LOG_SECRET) return res.status(401).json({ error: "Rossz vagy hianyzo jelszo" });

  const out = { ok: true, ts: new Date().toISOString(), errors: {} };
  const D = "D\u00e1tum";

  const grab = async (key, dbId, mapper) => {
    try { out[key] = (await q(dbId, D)).map(mapper).filter(x => x.d); }
    catch (e) { out[key] = []; out.errors[key] = String(e.message || e); }
  };

  await grab("sleep", DB.alvas, p => ({
    d:  pDate(p[D]),
    h:  pNum(p["Alv\u00e1s (\u00f3ra)"]),
    b:  pNum(p["\u00c1gyban t\u00f6lt\u00f6tt (\u00f3ra)"]),
    q:  pNum(p["Alv\u00e1smin\u0151s\u00e9g (%)"]),
    bed: pText(p["Lefekv\u00e9s"]),
    up:  pText(p["\u00c9bred\u00e9s"]),
    we:  pCheck(p["H\u00e9tv\u00e9ge"])
  }));
  await grab("steps", DB.lepes, p => ({ d: pDate(p[D]), s: pNum(p["L\u00e9p\u00e9s"]) }));
  await grab("gym", DB.edzes, p => ({
    d: pDate(p[D]), name: pText(p["Edz\u00e9s neve"]),
    min: pNum(p["Id\u0151tartam (perc)"]), sets: pNum(p["Szettek sz\u00e1ma"]),
    vol: pNum(p["\u00d6ssz volumen (kg)"])
  }));
  await grab("weight", DB.suly, p => ({ d: pDate(p[D]), kg: pNum(p["S\u00faly (kg)"]) }));
  await grab("work", DB.munka, p => ({
    d: pDate(p[D]), h: pNum(p["Ledolgozott \u00f3ra"]), rate: pNum(p["\u00d3rab\u00e9r (Ft)"]),
    jatt: pNum(p["Jatt (Ft)"]), name: pText(p["N\u00e9v"])
  }));

  try { out.today = await readToday(); }
  catch (e) { out.today = { found: false, sections: [] }; out.errors.today = String(e.message || e); }

  try { out.watch = await readWatch(); }
  catch (e) { out.watch = []; out.errors.watch = String(e.message || e); }

  ["sleep","steps","gym","weight","work"].forEach(k => {
    if (Array.isArray(out[k])) out[k].sort((a,b) => a.d < b.d ? -1 : a.d > b.d ? 1 : 0);
  });

  res.setHeader("Cache-Control", "no-store");
  return res.status(200).json(out);
}
