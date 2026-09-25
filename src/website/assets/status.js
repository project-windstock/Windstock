/* Project Windstock — static status page logic */
(function () {
  var REPO_URL = "https://api.github.com/repos/project-windstock/windstock";
  var REPO_OWNER = "project-windstock";
  var REPO_NAME = "windstock";
  var LAST_UPDATED = "2026-09-25T12:00:00.000Z";
  var STORAGE_KEY = "windstock:repo-history";
  var WINDOW_MS = 10 * 60 * 60 * 1000;
  var MAX_HISTORY = 360;
  var MAX_CHART_POINTS = 180;
  var CHART_MIN_INTERVAL = 30000;
  var POLL_INTERVAL = 60000;

  var $ = function (id) { return document.getElementById(id); };

  function fmt(iso) {
    try {
      return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch (e) { return iso; }
  }

  function loadHistory() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(parsed)) return [];
      var cutoff = Date.now() - WINDOW_MS;
      return parsed.filter(function (s) { return s && typeof s.t === "number" && s.t >= cutoff; }).slice(-MAX_HISTORY);
    } catch (e) { return []; }
  }

  var history = loadHistory();
  var chartRenderAt = 0;
  var checkInFlight = false;

  function pushSample(s) {
    var last = history[history.length - 1];
    if (last && Math.abs(last.t - s.t) < 500) return;
    history.push(s);
    var cutoff = Date.now() - WINDOW_MS;
    history = history.filter(function (x) { return x.t >= cutoff; }).slice(-MAX_HISTORY);
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(history)); } catch (e) {}
  }

  function renderChart(force) {
    var host = $("chart");
    if (!host) return;
    var now = Date.now();
    if (!force && history.length > 1 && now - chartRenderAt < CHART_MIN_INTERVAL) return;
    if (history.length < 2) {
      host.innerHTML = '<p class="text-sm text-muted-foreground">Collecting data… the chart appears after a couple of checks.</p>';
      chartRenderAt = now;
      return;
    }

    chartRenderAt = now;
    var chartHistory = history;
    if (chartHistory.length > MAX_CHART_POINTS) {
      var step = Math.ceil(chartHistory.length / MAX_CHART_POINTS);
      chartHistory = chartHistory.filter(function (sample, index) {
        return index === 0 || index === chartHistory.length - 1 || index % step === 0;
      });
    }

    var width = 640, height = 140, padY = 12;
    var start = Math.min(chartHistory[0].t, now - WINDOW_MS);
    var span = Math.max(now - start, 1);
    var maxMs = 100;
    chartHistory.forEach(function (s) { if (s.ms && s.ms > maxMs) maxMs = s.ms; });
    var x = function (t) { return ((t - start) / span) * width; };
    var y = function (ms) { return height - padY - (ms / maxMs) * (height - padY * 2); };

    var parts = [];
    parts.push('<line x1="0" x2="' + width + '" y1="' + (height - padY) + '" y2="' + (height - padY) + '" class="stroke-border" stroke-width="1"/>');
    chartHistory.forEach(function (s) {
      if (s.online) return;
      parts.push('<rect x="' + Math.max(0, x(s.t) - 2) + '" y="0" width="4" height="' + height + '" class="fill-destructive/25"/>');
    });
    var pts = chartHistory.filter(function (s) { return s.ms !== null && s.ms !== undefined; })
      .map(function (s) { return x(s.t).toFixed(1) + "," + y(s.ms).toFixed(1); });
    if (pts.length > 1) {
      parts.push('<polyline points="' + pts.join(" ") + '" fill="none" class="stroke-primary" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>');
    }
    chartHistory.forEach(function (s) {
      if (s.ms === null || s.ms === undefined) return;
      parts.push('<circle cx="' + x(s.t) + '" cy="' + y(s.ms) + '" r="2.5" class="' + (s.online ? "fill-primary" : "fill-destructive") + '"/>');
    });

    host.innerHTML =
      '<svg viewBox="0 0 ' + width + " " + height + '" class="h-36 w-full" role="img" aria-label="Response time over the last 10 hours" preserveAspectRatio="none">' +
      parts.join("") + "</svg>" +
      '<div class="mt-2 flex justify-between text-xs text-muted-foreground"><span>10h ago</span><span>peak ' +
      Math.round(maxMs) + " ms</span><span>now</span></div>";
  }

  function renderAlert(data) {
    var host = $("alert");
    if (!host) return;
    if (data.online) { host.innerHTML = ""; return; }
    host.innerHTML =
        '<div role="alert" aria-live="assertive" class="wind-alert">' +
        '<div class="wind-alert-inner rounded-xl border border-destructive/50 bg-destructive/10 p-5">' +
        '<p class="text-sm font-semibold tracking-tight text-destructive">Repo check failed</p>' +
        '<p class="mt-1 text-sm text-muted-foreground">' + esc(data.error || "GitHub did not respond as expected. Try again in a minute.") + "</p>" +
        '<dl class="mt-3 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">' +
        '<div><dt class="inline uppercase tracking-[0.2em]">HTTP</dt> <dd class="inline font-medium">' +
        (data.httpStatus ? data.httpStatus : "no response") + "</dd></div>" +
        '<div><dt class="inline uppercase tracking-[0.2em]">Checked</dt> <dd class="inline font-medium">' +
        fmt(data.checkedAt) + "</dd></div></dl>" +
        (data.responseBody
          ? '<pre class="mt-3 max-h-40 overflow-auto rounded-lg border border-border/60 bg-card p-3 text-xs whitespace-pre-wrap">' + esc(data.responseBody) + "</pre>"
          : "") +
        "</div></div>";
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render(data) {
    $("dot").className = "status-pulse" + (data.online ? "" : " is-offline");
    $("state").textContent = data.online ? "Online" : "Offline";
    $("blurb").textContent = data.online
      ? "The repository exists — github.com/project-windstock/windstock is public and alive, and so is the world. Log in from the game client with any trainer name."
      : "The repository didn't answer. Either GitHub is having a moment, or the world went quiet. Check back in a minute.";
    $("updated").textContent = fmt(data.pushedAt || LAST_UPDATED);
    $("checked").textContent = fmt(data.checkedAt);
    $("rt").textContent = data.responseTimeMs !== null ? data.responseTimeMs + " ms" : "—";
    renderAlert(data);
    renderChart(false);
  }

  async function check() {
    if (checkInFlight || document.hidden) return;
    checkInFlight = true;
    try {
      var online = false, responseTimeMs = null, error = null, httpStatus = null, responseBody = null, pushedAt = null;
      var started = performance.now();
      try {
        var controller = new AbortController();
        var timer = setTimeout(function () { controller.abort(); }, 6000);
        var res;
        try {
          res = await fetch(REPO_URL, { signal: controller.signal, cache: "no-store", headers: { "Accept": "application/vnd.github+json" } });
        } finally {
          clearTimeout(timer);
        }
        responseTimeMs = Math.round(performance.now() - started);
        httpStatus = res.status;
        var text = await res.text().catch(function () { return ""; });
        responseBody = text ? text.slice(0, 500) : null;
        if (res.ok) {
          var body = null;
          try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
          online = !!(body && String(body.full_name || "").toLowerCase() === (REPO_OWNER + "/" + REPO_NAME).toLowerCase() && body.archived !== true);
          pushedAt = body && body.pushed_at ? body.pushed_at : null;
          if (!online) error = "GitHub answered, but the repository is not there it should be.";
        } else {
          if (res.status === 404) {
            error = "GitHub returned HTTP 404 — the repository does not exist.";
          } else if (res.status === 403) {
            error = "GitHub rate-limited the check (HTTP 403). Give it a few minutes.";
          } else {
            error = "GitHub returned HTTP " + res.status + " " + res.statusText + ".";
          }
        }
      } catch (e) {
        online = false;
        error = (e && e.name ? e.name + ": " : "") + (e && e.message ? e.message : "Unknown network error while contacting GitHub's API.");
      }
      var data = {
        online: online,
        checkedAt: new Date().toISOString(),
        responseTimeMs: responseTimeMs,
        error: error,
        httpStatus: httpStatus,
        responseBody: responseBody,
        pushedAt: pushedAt,
      };
      pushSample({ t: Date.now(), ms: responseTimeMs, online: online });
      render(data);
    } finally {
      checkInFlight = false;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = $("checkNow");
    if (btn) {
      btn.addEventListener("click", async function () {
        btn.disabled = true;
        btn.textContent = "Checking…";
        try { await check(); } finally {
          btn.disabled = false;
          btn.innerHTML = "<span>Check now</span><span aria-hidden=\"true\">↗</span>";
        }
      });
    }
    check();
    setInterval(function () {
      if (!document.hidden) check();
    }, POLL_INTERVAL);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) check();
    });
  });
})();
