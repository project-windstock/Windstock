const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { URL, URLSearchParams } = require("node:url");
const { DatabaseSync } = require("node:sqlite");

loadDotEnv(path.join(__dirname, ".env"));

const PORT = Number(process.env.PORT || 8787);
const ROOT = __dirname;
const DATABASE_PATH = path.resolve(ROOT, process.env.DATABASE_PATH || "data/windstock.sqlite");
const INVITE_PEPPER = process.env.INVITE_PEPPER || "local-development-only-change-me";
const STATE_TTL_MS = 10 * 60 * 1000;
const MAX_BODY_BYTES = 16 * 1024;
const RATE_LIMIT_WINDOW_MS = 60 * 1000;
const RATE_LIMIT_MAX = 20;
const rateLimits = new Map();

if (!process.env.INVITE_PEPPER && process.env.NODE_ENV === "production") {
  throw new Error("INVITE_PEPPER must be set in production.");
}
if (!process.env.INVITE_PEPPER) {
  console.warn("Warning: using the development invite pepper. Set INVITE_PEPPER before deploying.");
}

fs.mkdirSync(path.dirname(DATABASE_PATH), { recursive: true });
const db = new DatabaseSync(DATABASE_PATH);
db.exec("PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;");
db.exec(`
  CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    code_hint TEXT NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses > 0),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER
  );

  CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    invite_id INTEGER NOT NULL REFERENCES invites(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing')),
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invite_id INTEGER NOT NULL REFERENCES invites(id) ON DELETE CASCADE,
    discord_user_id TEXT NOT NULL,
    discord_username TEXT,
    joined_at INTEGER NOT NULL,
    UNIQUE (invite_id, discord_user_id)
  );

  CREATE INDEX IF NOT EXISTS invites_code_hash_idx ON invites(code_hash);
  CREATE INDEX IF NOT EXISTS oauth_states_invite_idx ON oauth_states(invite_id, status, expires_at);
`);

function loadDotEnv(filePath) {
  if (!fs.existsSync(filePath)) return;
  const contents = fs.readFileSync(filePath, "utf8");
  contents.split(/\r?\n/).forEach(function (line) {
    const match = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (!match || process.env[match[1]] !== undefined) return;
    let value = match[2];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    process.env[match[1]] = value;
  });
}

function now() {
  return Date.now();
}

function normalizeInvite(value) {
  return String(value || "").trim().toUpperCase().replace(/\s+/g, "");
}

function isInviteShape(value) {
  return value.length >= 9 && value.length <= 40 && /^[A-Z0-9]+(?:-[A-Z0-9]+){1,2}$/.test(value);
}

function hashInvite(code) {
  return crypto.createHmac("sha256", INVITE_PEPPER).update(code).digest("hex");
}

function hashState(state) {
  return crypto.createHash("sha256").update(state).digest("hex");
}

function generateInviteCode() {
  const value = crypto.randomBytes(10).toString("hex").toUpperCase();
  return "WIND-" + value.slice(0, 8) + "-" + value.slice(8);
}

function generateState() {
  return crypto.randomBytes(32).toString("base64url");
}

function withTransaction(callback) {
  db.exec("BEGIN IMMEDIATE");
  try {
    const result = callback();
    db.exec("COMMIT");
    return result;
  } catch (error) {
    try { db.exec("ROLLBACK"); } catch (rollbackError) {}
    throw error;
  }
}

function pruneExpiredStates() {
  db.prepare("DELETE FROM oauth_states WHERE expires_at <= ?").run(now());
}

function getInviteByHash(codeHash) {
  return db.prepare(`
    SELECT id, code_hint, max_uses, use_count, expires_at, revoked_at
    FROM invites
    WHERE code_hash = ?
  `).get(codeHash);
}

function inviteIsAvailable(invite) {
  return Boolean(
    invite &&
    !invite.revoked_at &&
    invite.use_count < invite.max_uses &&
    (!invite.expires_at || invite.expires_at > now())
  );
}

function discordConfig() {
  const config = {
    clientId: process.env.DISCORD_CLIENT_ID,
    clientSecret: process.env.DISCORD_CLIENT_SECRET,
    botToken: process.env.DISCORD_BOT_TOKEN,
    guildId: process.env.DISCORD_GUILD_ID,
    redirectUri: process.env.DISCORD_REDIRECT_URI || `http://localhost:${PORT}/auth/discord/callback`,
    roleId: process.env.DISCORD_MEMBER_ROLE_ID || "",
  };
  const missing = [];
  if (!config.clientId) missing.push("DISCORD_CLIENT_ID");
  if (!config.clientSecret) missing.push("DISCORD_CLIENT_SECRET");
  if (!config.botToken) missing.push("DISCORD_BOT_TOKEN");
  if (!config.guildId) missing.push("DISCORD_GUILD_ID");
  return { config, missing };
}

function createAuthorizationUrl(state, config) {
  const query = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    response_type: "code",
    scope: "identify guilds.join",
    state,
  });
  return "https://discord.com/oauth2/authorize?" + query.toString();
}

function clientIp(request) {
  return request.socket && request.socket.remoteAddress ? request.socket.remoteAddress : "unknown";
}

function allowedByRateLimit(request) {
  const key = clientIp(request);
  const timestamp = now();
  const existing = rateLimits.get(key);
  if (!existing || existing.resetAt <= timestamp) {
    rateLimits.set(key, { count: 1, resetAt: timestamp + RATE_LIMIT_WINDOW_MS });
    return true;
  }
  if (existing.count >= RATE_LIMIT_MAX) return false;
  existing.count += 1;
  return true;
}

function sendJson(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}

function redirect(response, location) {
  response.writeHead(302, {
    Location: location,
    "Cache-Control": "no-store",
  });
  response.end();
}

function readJson(request) {
  return new Promise(function (resolve, reject) {
    let size = 0;
    let body = "";
    request.setEncoding("utf8");
    request.on("data", function (chunk) {
      size += Buffer.byteLength(chunk);
      if (size > MAX_BODY_BYTES) {
        reject(Object.assign(new Error("Request body too large"), { statusCode: 413 }));
        request.destroy();
        return;
      }
      body += chunk;
    });
    request.on("end", function () {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (error) {
        reject(Object.assign(new Error("Request body must be valid JSON"), { statusCode: 400 }));
      }
    });
    request.on("error", reject);
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, function (character) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
  });
}

function sendHtml(response, statusCode, title, message) {
  const body = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${escapeHtml(title)} — Project Windstock</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box;background:#f7f2e8;color:#172b29;font:16px/1.6 system-ui,sans-serif}main{max-width:560px;padding:42px;border:1px solid #d8d4c7;border-radius:24px;background:#fffdf8;box-shadow:0 18px 40px #24302b14}h1{margin:0 0 14px;font:600 clamp(2.5rem,8vw,5rem)/.9 Georgia,serif;letter-spacing:-.07em}p{color:#64716c}a{display:inline-block;margin-top:14px;padding:12px 18px;border-radius:999px;color:#fff9ee;background:#e45e42;text-decoration:none;font-weight:700}</style></head>
<body><main><p>PROJECT WINDSTOCK</p><h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p><a href="/invite.html">Back to invites</a></main></body></html>`;
  response.writeHead(statusCode, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}

async function fetchJson(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(function () { controller.abort(); }, timeoutMs || 10000);
  try {
    const response = await fetch(url, Object.assign({}, options, { signal: controller.signal }));
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch (error) {}
    if (!response.ok) {
      const error = new Error("Discord API request failed");
      error.statusCode = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

async function exchangeDiscordCode(code, config) {
  const body = new URLSearchParams({
    client_id: config.clientId,
    client_secret: config.clientSecret,
    grant_type: "authorization_code",
    code,
    redirect_uri: config.redirectUri,
  });
  return fetchJson("https://discord.com/api/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
}

async function getDiscordUser(accessToken) {
  return fetchJson("https://discord.com/api/users/@me", {
    headers: { Authorization: "Bearer " + accessToken },
  });
}

async function addDiscordMember(userId, accessToken, config) {
  const memberUrl = "https://discord.com/api/guilds/" + encodeURIComponent(config.guildId) + "/members/" + encodeURIComponent(userId);
  await fetchJson(memberUrl, {
    method: "PUT",
    headers: {
      Authorization: "Bot " + config.botToken,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ access_token: accessToken }),
  });
  if (config.roleId) {
    const roleUrl = memberUrl + "/roles/" + encodeURIComponent(config.roleId);
    await fetchJson(roleUrl, {
      method: "PUT",
      headers: { Authorization: "Bot " + config.botToken },
    });
  }
}

function reserveInvite(code) {
  const normalized = normalizeInvite(code);
  if (!isInviteShape(normalized)) return null;
  return withTransaction(function () {
    pruneExpiredStates();
    const invite = getInviteByHash(hashInvite(normalized));
    if (!inviteIsAvailable(invite)) return null;
    const reservations = db.prepare(`
      SELECT COUNT(*) AS count
      FROM oauth_states
      WHERE invite_id = ? AND expires_at > ?
    `).get(invite.id, now());
    if (invite.use_count + Number(reservations.count) >= invite.max_uses) return null;

    const state = generateState();
    const expiresAt = now() + STATE_TTL_MS;
    db.prepare(`
      INSERT INTO oauth_states (state_hash, invite_id, status, expires_at, created_at)
      VALUES (?, ?, 'pending', ?, ?)
    `).run(hashState(state), invite.id, expiresAt, now());
    return { state, expiresAt };
  });
}

function beginOAuthState(state) {
  if (!state || state.length < 32 || state.length > 128) return null;
  return withTransaction(function () {
    pruneExpiredStates();
    const stateHash = hashState(state);
    const row = db.prepare(`
      SELECT state_hash, invite_id, expires_at
      FROM oauth_states
      WHERE state_hash = ? AND status = 'pending' AND expires_at > ?
    `).get(stateHash, now());
    if (!row) return null;
    db.prepare("UPDATE oauth_states SET status = 'processing' WHERE state_hash = ?").run(stateHash);
    return row;
  });
}

function releaseOAuthState(state) {
  if (!state) return;
  try { db.prepare("DELETE FROM oauth_states WHERE state_hash = ?").run(hashState(state)); } catch (error) { console.error("Could not release OAuth state:", error); }
}

function finalizeRedemption(state, inviteId, user) {
  return withTransaction(function () {
    const invite = db.prepare(`
      SELECT id, max_uses, use_count, expires_at, revoked_at
      FROM invites WHERE id = ?
    `).get(inviteId);
    if (!invite || invite.revoked_at || invite.use_count >= invite.max_uses || (invite.expires_at && invite.expires_at <= now())) {
      throw new Error("Invite is no longer available");
    }
    const existing = db.prepare(`
      SELECT id FROM redemptions WHERE invite_id = ? AND discord_user_id = ?
    `).get(inviteId, String(user.id));
    if (!existing) {
      db.prepare("UPDATE invites SET use_count = use_count + 1 WHERE id = ?").run(inviteId);
      db.prepare(`
        INSERT INTO redemptions (invite_id, discord_user_id, discord_username, joined_at)
        VALUES (?, ?, ?, ?)
      `).run(inviteId, String(user.id), user.username ? String(user.username).slice(0, 120) : null, now());
    }
    db.prepare("DELETE FROM oauth_states WHERE state_hash = ?").run(hashState(state));
    return !existing;
  });
}

async function handleInviteValidation(request, response) {
  if (!allowedByRateLimit(request)) {
    sendJson(response, 429, { ok: false, error: "Too many attempts. Try again in a minute." });
    return;
  }
  const configured = discordConfig();
  if (configured.missing.length) {
    sendJson(response, 503, { ok: false, error: "Discord access is not configured on the server yet." });
    return;
  }
  let input;
  try { input = await readJson(request); } catch (error) {
    sendJson(response, error.statusCode || 400, { ok: false, error: error.message });
    return;
  }
  const reservation = reserveInvite(input && input.code);
  if (!reservation) {
    sendJson(response, 400, { ok: false, error: "That invite is invalid, expired, revoked, or already used." });
    return;
  }
  sendJson(response, 200, {
    ok: true,
    authorizationUrl: createAuthorizationUrl(reservation.state, configured.config),
    expiresInSeconds: Math.floor(STATE_TTL_MS / 1000),
  });
}

async function handleDiscordCallback(request, response, url) {
  const state = url.searchParams.get("state") || "";
  if (url.searchParams.get("error")) {
    releaseOAuthState(state);
    redirect(response, "/invite.html?error=discord_denied");
    return;
  }
  const code = url.searchParams.get("code") || "";
  if (!code || !state) {
    redirect(response, "/invite.html?error=missing_callback");
    return;
  }
  const stateRow = beginOAuthState(state);
  if (!stateRow) {
    redirect(response, "/invite.html?error=expired_invite");
    return;
  }

  const configured = discordConfig();
  if (configured.missing.length) {
    releaseOAuthState(state);
    redirect(response, "/invite.html?error=server_setup");
    return;
  }

  try {
    const token = await exchangeDiscordCode(code, configured.config);
    if (!token || !token.access_token) throw new Error("Discord did not return an access token");
    const user = await getDiscordUser(token.access_token);
    if (!user || !user.id) throw new Error("Discord did not return a user");
    await addDiscordMember(user.id, token.access_token, configured.config);
    finalizeRedemption(state, stateRow.invite_id, user);
    redirect(response, "/invite.html?joined=1");
  } catch (error) {
    releaseOAuthState(state);
    console.error("Discord invite flow failed:", error.statusCode || error.message);
    redirect(response, "/invite.html?error=discord_join_failed");
  }
}

const MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".txt": "text/plain; charset=utf-8",
};

function serveStatic(request, response, url) {
  let pathname;
  try { pathname = decodeURIComponent(url.pathname); } catch (error) {
    response.writeHead(400); response.end("Bad request"); return;
  }
  if (pathname === "/") pathname = "/index.html";
  const filePath = path.resolve(ROOT, "." + pathname);
  if (filePath !== ROOT && !filePath.startsWith(ROOT + path.sep)) {
    response.writeHead(403); response.end("Forbidden"); return;
  }
  const relativePath = path.relative(ROOT, filePath);
  const publicFile = relativePath === "" ||
    (/^\.?[^\\/]+\.html$/i.test(relativePath)) ||
    relativePath.startsWith("assets" + path.sep);
  if (!publicFile) {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Not found");
    return;
  }
  fs.stat(filePath, function (error, stats) {
    if (error || !stats.isFile()) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Not found");
      return;
    }
    const extension = path.extname(filePath).toLowerCase();
    response.writeHead(200, {
      "Content-Type": MIME_TYPES[extension] || "application/octet-stream",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "same-origin",
    });
    fs.createReadStream(filePath).pipe(response);
  });
}

async function handleRequest(request, response) {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  if (request.method === "POST" && url.pathname === "/api/invites/validate") {
    await handleInviteValidation(request, response);
    return;
  }
  if (request.method === "GET" && url.pathname === "/auth/discord/callback") {
    await handleDiscordCallback(request, response, url);
    return;
  }
  if (request.method !== "GET" && request.method !== "HEAD") {
    response.writeHead(405, { Allow: "GET, HEAD, POST" });
    response.end("Method not allowed");
    return;
  }
  serveStatic(request, response, url);
}

function createInviteFromCli(args) {
  let maxUses = 1;
  let expiresAt = null;
  args.forEach(function (argument) {
    const match = /^--(?:uses|max-uses)=(\d+)$/.exec(argument);
    if (match) maxUses = Number(match[1]);
    const expiry = /^--expires=(.+)$/.exec(argument);
    if (expiry) {
      const parsed = Date.parse(expiry[1]);
      if (!Number.isNaN(parsed)) expiresAt = parsed;
    }
  });
  if (!Number.isInteger(maxUses) || maxUses < 1 || maxUses > 100000) throw new Error("--uses must be an integer between 1 and 100000");
  const code = generateInviteCode();
  db.prepare(`
    INSERT INTO invites (code_hash, code_hint, max_uses, expires_at, created_at)
    VALUES (?, ?, ?, ?, ?)
  `).run(hashInvite(code), code.slice(0, 9) + "…", maxUses, expiresAt, now());
  console.log("Created invite:", code);
  console.log("Uses:", maxUses + (expiresAt ? " · expires " + new Date(expiresAt).toISOString() : ""));
}

if (process.argv[2] === "create-invite") {
  try {
    createInviteFromCli(process.argv.slice(3));
    db.close();
  } catch (error) {
    console.error(error.message);
    db.close();
    process.exitCode = 1;
  }
} else {
  const server = http.createServer(function (request, response) {
    handleRequest(request, response).catch(function (error) {
      console.error("Request failed:", error);
      if (!response.headersSent) sendJson(response, 500, { ok: false, error: "Internal server error" });
      else response.end();
    });
  });

  server.listen(PORT, function () {
    console.log(`Project Windstock server listening at http://localhost:${PORT}`);
  });
}
