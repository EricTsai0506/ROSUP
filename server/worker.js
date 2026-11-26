// 掛機小幫手 序號驗證伺服器（Cloudflare Worker + D1）
//
// 需要的設定（Cloudflare 後台 → 這個 Worker → Settings）：
//   Bindings：D1 資料庫，變數名稱 DB
//   Secrets ：SIGNING_KEY（回應簽章用的私鑰，賣家給的那串）、ADMIN_PASSWORD（管理頁密碼，自己取）
//
// 程式端：POST /v1/check  {key, machine, app_ver, nonce}
//   → {p: "<JSON 字串>", s: "<Ed25519 簽章 base64>"}，程式用公鑰驗證，回應無法偽造
// 管理頁：/admin（輸入 ADMIN_PASSWORD 後可以停用序號、改到期日、寫備註、下載買家回報的紀錄檔）
//
// 問題回報：POST /v1/report（內容是一個 zip，裡面只有文字紀錄檔；資訊放在 x- 開頭的標頭）
//   存在同一個 D1 資料庫的 reports 資料表（第一次有人回報時自動建立），最多留 300 筆／30 天
//   想要有人回報時收到 Discord 通知：Secrets 多加一個 DISCORD_WEBHOOK（頻道的 Webhook 網址），不加也能用

// 序號簽章的公鑰（跟程式裡 licensing.py 的 PUBLIC_KEY_HEX 一樣）
const SERIAL_PUBLIC_KEY_HEX = "29b2500b132fb7ccf147ac3d464981c57e28a1ce2cec313fe53ce6c9ee4ea839";
const EPOCH_MS = Date.UTC(2020, 0, 1);
const DAY_MS = 86400000;
const TZ_OFFSET_MS = 8 * 3600000;          // 用台灣時間算「今天」

const SCHEMA = `CREATE TABLE IF NOT EXISTS licenses (
  key_id TEXT PRIMARY KEY,
  machine TEXT NOT NULL,
  start_date TEXT,
  end_date TEXT,
  end_override TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  last_seen TEXT,
  last_ip TEXT,
  app_ver TEXT,
  checks INTEGER NOT NULL DEFAULT 0
)`;
const REPORT_SCHEMA = `CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id TEXT NOT NULL DEFAULT '',
  machine TEXT NOT NULL DEFAULT '',
  app_ver TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  ip TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  data BLOB
)`;
const REPORT_MAX = 1900000;        // 一份回報最大幾 bytes（D1 一格最多 2MB）
const REPORT_MAX_NOKEY = 400000;   // 沒有有效序號的人（例如還沒啟用就遇到問題）只能傳小的
const REPORT_KEEP = 300;           // 最多留幾筆
const REPORT_DAYS = 30;            // 留幾天
let schemaReady = false;

async function ensureSchema(env) {
  if (!schemaReady) {
    await env.DB.prepare(SCHEMA).run();
    await env.DB.prepare(REPORT_SCHEMA).run();
    schemaReady = true;
  }
}

// ---------- 小工具 ----------
const hexToBytes = (h) => new Uint8Array(h.match(/../g).map((x) => parseInt(x, 16)));
const bytesToHex = (b) => [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
const b64ToBytes = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const bytesToB64 = (b) => btoa(String.fromCharCode(...b));

function base32Decode(s) {
  const alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  s = s.toUpperCase().replace(/[^A-Z2-7]/g, "");
  let bits = 0, val = 0;
  const out = [];
  for (const ch of s) {
    val = (val << 5) | alpha.indexOf(ch);
    bits += 5;
    if (bits >= 8) {
      out.push((val >>> (bits - 8)) & 255);
      bits -= 8;
    }
  }
  return new Uint8Array(out);
}

const dateFromDays = (n) => new Date(EPOCH_MS + n * DAY_MS).toISOString().slice(0, 10);
const todayTW = () => new Date(Date.now() + TZ_OFFSET_MS).toISOString().slice(0, 10);
const nowTW = () => new Date(Date.now() + TZ_OFFSET_MS).toISOString().slice(0, 19).replace("T", " ");

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

// 序號 = 13 bytes 內容（版本、機器碼雜湊 8、開通日、到期日）+ 64 bytes 簽章
async function decodeSerial(key) {
  const raw = base32Decode(key || "");
  if (raw.length !== 77) return null;
  const payload = raw.slice(0, 13), sig = raw.slice(13);
  const pub = await crypto.subtle.importKey("raw", hexToBytes(SERIAL_PUBLIC_KEY_HEX),
    { name: "Ed25519" }, false, ["verify"]);
  if (!(await crypto.subtle.verify({ name: "Ed25519" }, pub, sig, payload))) return null;
  if (payload[0] !== 1) return null;
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const keyId = bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", raw))).slice(0, 16);
  return {
    keyId,
    machine: bytesToHex(payload.slice(1, 9)).toUpperCase(),
    start: dateFromDays(dv.getUint16(9)),
    end: dateFromDays(dv.getUint16(11)),
  };
}

let signingKey = null;
async function signedResponse(env, obj) {
  if (!signingKey) {
    signingKey = await crypto.subtle.importKey("pkcs8", b64ToBytes(env.SIGNING_KEY.trim()),
      { name: "Ed25519" }, false, ["sign"]);
  }
  const p = JSON.stringify(obj);
  const s = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, signingKey,
    new TextEncoder().encode(p)));
  return json({ p, s: bytesToB64(s) });
}

// ---------- 程式來驗證 ----------
async function handleCheck(req, env) {
  let body;
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad json" }, 400);
  }
  const nonce = String(body.nonce || "").slice(0, 64);
  const machine = String(body.machine || "").toUpperCase().replace(/[^0-9A-F]/g, "");
  const reply = (status, msg, end = null) =>
    signedResponse(env, { v: 1, nonce, status, msg, end, today: todayTW() });

  const info = await decodeSerial(String(body.key || ""));
  if (!info) return reply("bad", "序號無效");
  if (machine !== info.machine) return reply("bad", "這組序號不是這台電腦的（機器碼不符）");

  await ensureSchema(env);
  const ip = req.headers.get("cf-connecting-ip") || "";
  const now = nowTW();
  await env.DB.prepare(
    `INSERT INTO licenses (key_id, machine, start_date, end_date, created_at, last_seen, last_ip, app_ver, checks)
     VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6, ?7, 1)
     ON CONFLICT(key_id) DO UPDATE SET last_seen = ?5, last_ip = ?6, app_ver = ?7, checks = checks + 1`
  ).bind(info.keyId, info.machine, info.start, info.end, now, ip,
    String(body.app_ver || "").slice(0, 20)).run();
  const row = await env.DB.prepare("SELECT revoked, end_override FROM licenses WHERE key_id = ?1")
    .bind(info.keyId).first();

  if (row && row.revoked) return reply("revoked", "這組序號已被停用，請聯絡賣家");
  const end = (row && row.end_override) || info.end;
  if (todayTW() >= end) return reply("expired", `授權已於 ${end} 到期，請購買新的月卡`, end);
  return reply("ok", "ok", end);
}

// ---------- 買家回報問題（上傳文字紀錄檔的 zip）----------
async function handleReport(req, env, ctx) {
  const h = (k) => req.headers.get(k) || "";
  const declared = parseInt(h("content-length") || "0", 10);
  if (declared > REPORT_MAX) return json({ error: "檔案太大" }, 413);
  const buf = await req.arrayBuffer();
  if (buf.byteLength < 22 || buf.byteLength > REPORT_MAX) return json({ error: "檔案大小不對" }, 413);
  const head = new Uint8Array(buf, 0, 2);
  if (head[0] !== 0x50 || head[1] !== 0x4b) return json({ error: "格式不對" }, 400);    // zip 開頭是 PK

  const machine = h("x-machine").toUpperCase().replace(/[^0-9A-F]/g, "").slice(0, 16);
  const info = await decodeSerial(h("x-key"));
  const licensed = !!(info && info.machine === machine);
  let note = "";
  try { note = decodeURIComponent(h("x-note")); } catch { note = h("x-note"); }
  note = note.slice(0, 500);
  const kind = h("x-kind").replace(/[^a-z_]/g, "").slice(0, 20) || "manual";
  const ver = h("x-ver").replace(/[^0-9A-Za-z._-]/g, "").slice(0, 20);
  const ip = h("cf-connecting-ip");
  const now = nowTW();
  const hourAgo = new Date(Date.now() + TZ_OFFSET_MS - 3600000).toISOString().slice(0, 19).replace("T", " ");
  const dayAgo = new Date(Date.now() + TZ_OFFSET_MS - DAY_MS).toISOString().slice(0, 19).replace("T", " ");

  await ensureSchema(env);
  // 限制次數：避免有人灌爆資料庫
  if (licensed) {
    const r = await env.DB.prepare(
      `SELECT SUM(created_at > ?2) AS h, COUNT(*) AS d FROM reports WHERE key_id = ?1 AND created_at > ?3`
    ).bind(info.keyId, hourAgo, dayAgo).first();
    if ((r.h || 0) >= 6 || (r.d || 0) >= 20) return json({ error: "回報太頻繁了，請晚一點再試" }, 429);
  } else {
    if (buf.byteLength > REPORT_MAX_NOKEY) return json({ error: "還沒啟用序號時，回報的檔案不能太大" }, 413);
    const r = await env.DB.prepare(
      `SELECT SUM(ip = ?1) AS mine, COUNT(*) AS allc FROM reports WHERE key_id = '' AND created_at > ?2`
    ).bind(ip, dayAgo).first();
    if ((r.mine || 0) >= 3 || (r.allc || 0) >= 40) return json({ error: "回報太頻繁了，請晚一點再試" }, 429);
  }
  const ins = await env.DB.prepare(
    `INSERT INTO reports (key_id, machine, app_ver, created_at, ip, kind, note, size, data)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)`
  ).bind(licensed ? info.keyId : "", machine, ver, now, ip, kind, note, buf.byteLength, buf).run();
  const id = ins.meta && ins.meta.last_row_id;
  // 只留最近 REPORT_KEEP 筆、REPORT_DAYS 天
  const old = new Date(Date.now() + TZ_OFFSET_MS - REPORT_DAYS * DAY_MS).toISOString().slice(0, 19).replace("T", " ");
  await env.DB.prepare(
    `DELETE FROM reports WHERE created_at < ?1 OR id <= (SELECT MAX(id) FROM reports) - ?2`
  ).bind(old, REPORT_KEEP).run();

  if (env.DISCORD_WEBHOOK && ctx) ctx.waitUntil(notifyDiscord(env, { id, machine, ver, kind, note, now, licensed }, buf));
  return json({ ok: true, id });
}

async function notifyDiscord(env, r, buf) {
  try {
    let who = "";
    const row = await env.DB.prepare("SELECT note FROM licenses WHERE machine = ?1 ORDER BY last_seen DESC LIMIT 1")
      .bind(r.machine).first();
    if (row && row.note) who = `（${row.note}）`;
    const mc = (r.machine.match(/..../g) || [r.machine]).join("-");
    const kindName = { manual: "手動回報", update_fail: "更新失敗" }[r.kind] || r.kind;
    const form = new FormData();
    form.append("payload_json", JSON.stringify({
      content: `📩 回報 #${r.id}｜${kindName}｜v${r.ver || "?"}｜${mc}${who}${r.licensed ? "" : "｜沒有有效序號"}\n` +
        (r.note ? "> " + r.note.replace(/\n/g, "\n> ").slice(0, 900) : "（沒有寫說明）"),
      allowed_mentions: { parse: [] },
    }));
    form.append("files[0]", new Blob([buf], { type: "application/zip" }), `report_${r.id}_${mc}.zip`);
    await fetch(env.DISCORD_WEBHOOK.trim(), { method: "POST", body: form });
  } catch (e) {
    // 通知失敗不影響回報本身（管理頁還是看得到）
  }
}

// ---------- 管理頁 ----------
function adminOk(req, env) {
  const a = req.headers.get("x-admin-password") || "";
  const b = env.ADMIN_PASSWORD || "";
  if (!b || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

async function handleAdminApi(req, env, path) {
  if (!adminOk(req, env)) return json({ error: "密碼錯誤" }, 401);
  await ensureSchema(env);
  if (path === "/admin/api/list") {
    const { results } = await env.DB.prepare(
      "SELECT * FROM licenses ORDER BY last_seen DESC LIMIT 2000").all();
    return json({ rows: results, today: todayTW() });
  }
  if (path === "/admin/api/set") {
    const b = await req.json();
    const endOv = b.end_override ? String(b.end_override).slice(0, 10) : null;
    if (endOv && !/^\d{4}-\d{2}-\d{2}$/.test(endOv)) return json({ error: "日期格式要像 2026-12-31" }, 400);
    await env.DB.prepare(
      "UPDATE licenses SET revoked = ?2, end_override = ?3, note = ?4 WHERE key_id = ?1"
    ).bind(String(b.key_id), b.revoked ? 1 : 0, endOv, String(b.note || "").slice(0, 200)).run();
    return json({ ok: true });
  }
  if (path === "/admin/api/reports") {
    const { results } = await env.DB.prepare(
      `SELECT r.id, r.key_id, r.machine, r.app_ver, r.created_at, r.ip, r.kind, r.note, r.size,
              (SELECT l.note FROM licenses l WHERE l.machine = r.machine ORDER BY l.last_seen DESC LIMIT 1) AS who
       FROM reports r ORDER BY r.id DESC LIMIT 300`).all();
    return json({ rows: results });
  }
  if (path === "/admin/api/report_get") {
    const b = await req.json();
    const row = await env.DB.prepare("SELECT data, machine FROM reports WHERE id = ?1").bind(Number(b.id)).first();
    if (!row || !row.data) return json({ error: "找不到這筆回報" }, 404);
    return new Response(new Uint8Array(row.data), {
      headers: { "content-type": "application/zip", "cache-control": "no-store" },
    });
  }
  if (path === "/admin/api/report_del") {
    const b = await req.json();
    await env.DB.prepare("DELETE FROM reports WHERE id = ?1").bind(Number(b.id)).run();
    return json({ ok: true });
  }
  return json({ error: "not found" }, 404);
}

const ADMIN_HTML = `<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>序號管理</title>
<style>
body{font-family:system-ui,"Microsoft JhengHei",sans-serif;margin:20px;color:#222}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #ddd;padding:6px;text-align:left}
th{background:#f3f3f3}tr.rev td{background:#fbebeb}tr.exp td{color:#999}
input[type=text],input[type=date]{padding:3px;font-size:14px}button{padding:4px 10px;cursor:pointer}
#msg{margin:8px 0;color:#c0392b}.mono{font-family:Consolas,monospace}
</style></head><body>
<h2>掛機小幫手 序號管理</h2>
<div id="login">密碼：<input type="password" id="pw"> <button onclick="login()">登入</button></div>
<div id="main" style="display:none">
  <p>今天（台灣時間）：<b id="today"></b>　共 <b id="cnt"></b> 組　搜尋：<input type="text" id="q" oninput="draw()" placeholder="機器碼／備註"></p>
  <p style="color:#666">買家第一次開程式就會出現在這裡。勾「停用」→ 該序號下次驗證就不能用（程式每 6 小時、和每次開啟時驗證）。
  「改到期日」可以直接延長或縮短，不用重發序號；清空就回到序號原本的到期日。</p>
  <div id="msg"></div>
  <table><thead><tr><th>機器碼</th><th>序號到期</th><th>改到期日</th><th>停用</th><th>備註</th><th>最後上線</th><th>IP</th><th>版本</th><th>次數</th><th></th></tr></thead>
  <tbody id="tb"></tbody></table>
  <h3 style="margin-top:28px">問題回報 <button onclick="loadReports()">重新整理</button></h3>
  <p style="color:#666">買家在程式按「回報問題」（或更新失敗時選擇回報）送來的文字紀錄檔。按「下載」會拿到一個 zip，
  裡面有執行紀錄、更新紀錄、設定檔和「電腦資訊.txt」。最多留 300 筆、30 天。</p>
  <table><thead><tr><th>#</th><th>時間</th><th>買家（序號備註）</th><th>機器碼</th><th>版本</th><th>類型</th><th>買家寫的說明</th><th>大小</th><th></th></tr></thead>
  <tbody id="rb"></tbody></table>
</div>
<script>
let PW = sessionStorage.getItem("pw") || "", ROWS = [], TODAY = "";
async function api(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"content-type": "application/json", "x-admin-password": PW},
    body: JSON.stringify(body || {})});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
async function login() {
  PW = document.getElementById("pw").value || PW;
  try { const j = await api("/admin/api/list"); sessionStorage.setItem("pw", PW); show(j); }
  catch (e) { document.getElementById("msg").textContent = e.message; alert(e.message); }
}
function show(j) {
  ROWS = j.rows; TODAY = j.today;
  document.getElementById("login").style.display = "none";
  document.getElementById("main").style.display = "";
  document.getElementById("today").textContent = TODAY;
  draw();
  loadReports();
}
const KIND = {manual: "手動回報", update_fail: "更新失敗"};
async function loadReports() {
  try {
    const j = await api("/admin/api/reports");
    document.getElementById("rb").innerHTML = j.rows.map(r => {
      const mc = (r.machine.match(/..../g) || [r.machine]).join("-");
      return '<tr data-id="' + r.id + '" data-mc="' + esc(mc) + '"><td>' + r.id + '</td><td>' + esc(r.created_at) + '</td>' +
        '<td>' + esc(r.who || (r.key_id ? "" : "（沒有有效序號）")) + '</td><td class="mono">' + esc(mc) + '</td>' +
        '<td>' + esc(r.app_ver) + '</td><td>' + esc(KIND[r.kind] || r.kind) + '</td>' +
        '<td style="white-space:pre-wrap;max-width:420px">' + esc(r.note) + '</td><td>' + Math.round(r.size / 1024) + ' KB</td>' +
        '<td><button onclick="getReport(this)">下載</button> <button onclick="delReport(this)">刪除</button></td></tr>';
    }).join("") || '<tr><td colspan="9" style="color:#999">還沒有人回報</td></tr>';
  } catch (e) { document.getElementById("msg").textContent = "讀取回報失敗：" + e.message; }
}
async function getReport(btn) {
  const tr = btn.closest("tr");
  const r = await fetch("/admin/api/report_get", {method: "POST",
    headers: {"content-type": "application/json", "x-admin-password": PW}, body: JSON.stringify({id: Number(tr.dataset.id)})});
  if (!r.ok) { alert("下載失敗"); return; }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await r.blob());
  a.download = "report_" + tr.dataset.id + "_" + tr.dataset.mc + ".zip";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
async function delReport(btn) {
  const tr = btn.closest("tr");
  if (!confirm("刪除回報 #" + tr.dataset.id + "？")) return;
  try { await api("/admin/api/report_del", {id: Number(tr.dataset.id)}); tr.remove(); }
  catch (e) { alert(e.message); }
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function draw() {
  const q = document.getElementById("q").value.trim().toUpperCase();
  const rows = ROWS.filter(r => !q || (r.machine + " " + r.note).toUpperCase().includes(q));
  document.getElementById("cnt").textContent = ROWS.length;
  document.getElementById("tb").innerHTML = rows.map((r, i) => {
    const end = r.end_override || r.end_date;
    const cls = r.revoked ? "rev" : (end <= TODAY ? "exp" : "");
    const mc = r.machine.match(/..../g).join("-");
    return '<tr class="' + cls + '" data-id="' + esc(r.key_id) + '">' +
      '<td class="mono">' + esc(mc) + '</td><td>' + esc(r.end_date) + '</td>' +
      '<td><input type="date" value="' + esc(r.end_override || "") + '"></td>' +
      '<td><input type="checkbox"' + (r.revoked ? " checked" : "") + '></td>' +
      '<td><input type="text" size="18" value="' + esc(r.note) + '"></td>' +
      '<td>' + esc(r.last_seen) + '</td><td>' + esc(r.last_ip) + '</td><td>' + esc(r.app_ver) + '</td><td>' + r.checks + '</td>' +
      '<td><button onclick="save(this)">儲存</button></td></tr>';
  }).join("");
}
async function save(btn) {
  const tr = btn.closest("tr"), ins = tr.querySelectorAll("input");
  const body = {key_id: tr.dataset.id, end_override: ins[0].value, revoked: ins[1].checked, note: ins[2].value};
  try { await api("/admin/api/set", body); const r = ROWS.find(x => x.key_id === body.key_id);
    Object.assign(r, {end_override: body.end_override || null, revoked: body.revoked ? 1 : 0, note: body.note});
    draw(); document.getElementById("msg").textContent = "已儲存"; }
  catch (e) { alert(e.message); }
}
if (PW) login();
</script></body></html>`;

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    try {
      if (url.pathname === "/v1/check" && req.method === "POST") return await handleCheck(req, env);
      if (url.pathname === "/v1/report" && req.method === "POST") return await handleReport(req, env, ctx);
      if (url.pathname === "/admin") {
        return new Response(ADMIN_HTML, { headers: { "content-type": "text/html; charset=utf-8" } });
      }
      if (url.pathname.startsWith("/admin/api/") && req.method === "POST") {
        return await handleAdminApi(req, env, url.pathname);
      }
      if (url.pathname === "/") return new Response("ok");
      return new Response("not found", { status: 404 });
    } catch (e) {
      return json({ error: String(e && e.message || e) }, 500);
    }
  },
};
