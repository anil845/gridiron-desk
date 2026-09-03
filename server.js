// Live draft-board host + state sync for gridiron-desk. Zero dependencies.
//
//   GET  /health            -> ok (no auth; Fly checks)
//   GET  /                  -> index of boards (basic auth)
//   GET  /board/<slug>      -> the board HTML baked into the image (basic auth)
//   GET  /state/<slug>      -> latest pushed state JSON (basic auth)
//   POST /state/<slug>      -> save state (basic auth; the board pushes on every pick)
//
// State persists to DATA_DIR (a Fly volume). The board still keeps
// localStorage as its source of truth — this sync is a mirror so analysis can
// read the draft live without copy/paste.
"use strict";
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 8080;
const DATA = process.env.DATA_DIR || "/data";
const USER = process.env.BOARD_USER || "";
const PASS = process.env.BOARD_PASS || "";
const AUTH = "Basic " + Buffer.from(USER + ":" + PASS).toString("base64");
const SLUG = /^[a-z0-9-]{1,40}$/;

try { fs.mkdirSync(DATA, { recursive: true }); } catch (e) {}

function deny(res) {
  res.writeHead(401, { "WWW-Authenticate": 'Basic realm="war room"' });
  res.end("auth required");
}

function boards() {
  return fs.readdirSync(__dirname).filter(f => /^board_[a-z0-9-]+\.html$/.test(f))
    .map(f => f.replace(/^board_|\.html$/g, ""));
}

const server = http.createServer((req, res) => {
  const url = (req.url || "/").split("?")[0];
  if (url === "/health") { res.writeHead(200); return res.end("ok"); }
  if (!USER || (req.headers.authorization || "") !== AUTH) return deny(res);

  let m;
  if (req.method === "GET" && (url === "/" || url === "/index.html")) {
    const links = boards().map(s => `<li><a href="/board/${s}">${s}</a> · <a href="/state/${s}">state</a></li>`).join("");
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    return res.end(`<h3>gridiron war room</h3><ul>${links}</ul>`);
  }
  if (req.method === "GET" && (m = url.match(/^\/board\/([a-z0-9-]+)$/))) {
    const f = path.join(__dirname, `board_${m[1]}.html`);
    if (!fs.existsSync(f)) { res.writeHead(404); return res.end("no such board"); }
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    return res.end(fs.readFileSync(f));
  }
  if ((m = url.match(/^\/state\/([a-z0-9-]+)$/)) && SLUG.test(m[1])) {
    const f = path.join(DATA, `state_${m[1]}.json`);
    if (req.method === "GET") {
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      return res.end(fs.existsSync(f) ? fs.readFileSync(f) : '{"picks":[]}');
    }
    if (req.method === "POST") {
      let body = "", len = 0;
      req.on("data", c => { len += c.length; if (len > 2e6) req.destroy(); else body += c; });
      req.on("end", () => {
        try {
          const j = JSON.parse(body);
          if (!Array.isArray(j.picks)) throw new Error("no picks array");
          j.receivedAt = new Date().toISOString();
          fs.writeFileSync(f + ".tmp", JSON.stringify(j));
          fs.renameSync(f + ".tmp", f);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true, picks: j.picks.length }));
        } catch (e) {
          res.writeHead(400); res.end("bad state: " + e.message);
        }
      });
      return;
    }
  }
  res.writeHead(404); res.end("not found");
});

server.listen(PORT, () => console.log("war room listening on :" + PORT, "boards:", boards().join(", ")));
