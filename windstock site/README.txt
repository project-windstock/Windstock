Project Windstock — static site export
======================================

Files
  index.html        Landing page
  events.html       Community events page
  patcher.html      WIP client patcher page
  invite.html        Private invite validation and Discord join flow
  server.js          Node server, SQLite invite database, and Discord OAuth2 bridge
  .env.example       Discord/server configuration template
  assets/styles.css Full compiled stylesheet (light + dark themes)
  assets/motion-safe.css Crash-safe motion budget loaded after the main stylesheet
  assets/motion.js   Shared cinematic transitions and interaction layer
  assets/status.js  Health-check, auto-refresh, alert banner and 10h chart logic
  assets/hero.jpg, windstock-mark.png, favicon.png

Usage
  Static pages can still be opened directly or uploaded to a static host.
  The invite flow requires the Node server because invite codes, OAuth state,
  and Discord credentials must stay server-side.

  1. Copy .env.example to .env and fill in the Discord application values.
  2. Register DISCORD_REDIRECT_URI exactly in the Discord Developer Portal.
  3. Enable the OAuth2 `guilds.join` scope and add the bot to the target guild.
  4. Create a one-time invite with:
       node --experimental-sqlite server.js create-invite
     Optional flags: `--uses=5` and `--expires=2026-12-31T23:59:59Z`.
  5. Start the site with:
       node --experimental-sqlite server.js
     Then open `http://localhost:8787/invite.html`.

  The bot needs permission to add members through the OAuth2 join flow. If
  DISCORD_MEMBER_ROLE_ID is set, the bot also needs Manage Roles and that role
  must be below the bot's highest role.

  The server requires Node.js 22.5+ with the experimental built-in SQLite API.
  Use HTTPS and a strong INVITE_PEPPER in production.

Note about the invite page
  Invite codes are stored as HMAC hashes, never plaintext. A valid code creates
  a short-lived OAuth state, sends the trainer to Discord, and is consumed only
  after Discord confirms the user and the server adds them to the guild.

Note about the status page
  status.html checks https://bracky.playit.plus/health from the browser.
  Browsers block cross-origin requests unless that endpoint sends the header:
      Access-Control-Allow-Origin: *
  Without it the page will always show "Offline" (opening from file:// is
  always blocked). Either add that header to the health endpoint, or host
  these files on the same domain.
