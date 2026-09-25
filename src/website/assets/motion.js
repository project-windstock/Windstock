/* Project Windstock — cinematic motion layer */
(function () {
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var body = document.body;
  if (!body) return;

  body.classList.add("motion-enabled");
  if (reduceMotion) body.classList.add("motion-reduced");

  var progress = document.createElement("div");
  progress.className = "scroll-progress";
  progress.setAttribute("aria-hidden", "true");
  body.appendChild(progress);

  function updateScrollProgress() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var value = max > 0 ? Math.min(100, Math.max(0, (window.scrollY / max) * 100)) : 0;
    progress.style.setProperty("--scroll-progress", value + "%");
  }

  var scrollFrame = 0;
  var requestFrame = window.requestAnimationFrame || function (callback) {
    return window.setTimeout(callback, 16);
  };

  function scheduleScrollProgress() {
    if (scrollFrame) return;
    scrollFrame = requestFrame(function () {
      scrollFrame = 0;
      updateScrollProgress();
    });
  }

  updateScrollProgress();
  window.addEventListener("scroll", scheduleScrollProgress, { passive: true });
  window.addEventListener("resize", scheduleScrollProgress, { passive: true });

  function updateVisibilityMotion() {
    body.classList.toggle("motion-page-hidden", document.hidden);
  }

  updateVisibilityMotion();
  document.addEventListener("visibilitychange", updateVisibilityMotion);

  var transition = document.createElement("div");
  transition.className = "page-transition";
  transition.setAttribute("aria-hidden", "true");
  body.appendChild(transition);

  var revealSelector = [
    ".world-section > .section-heading",
    ".feature-card",
    ".manifesto-inner > *",
    ".closing-section > *",
    ".status-intro",
    ".status-panel",
    ".chart-panel",
    ".status-actions",
    ".events-intro",
    ".featured-event",
    ".events-section-heading",
    ".event-row",
    ".event-rhythm-section",
    ".patcher-intro",
    ".patcher-status-card",
    ".patcher-section-heading",
    ".patcher-feature",
    ".patcher-steps",
    ".patcher-cta",
    ".invite-intro",
    ".invite-panel",
    ".invite-footnote"
  ].join(",");
  var revealItems = Array.prototype.slice.call(document.querySelectorAll(revealSelector));

  revealItems.forEach(function (item, index) {
    item.classList.add("motion-reveal");
    item.style.setProperty("--reveal-delay", (index % 6) * 85 + "ms");
  });

  function showAll() {
    revealItems.forEach(function (item) { item.classList.add("is-visible"); });
  }

  if (reduceMotion || !("IntersectionObserver" in window)) {
    showAll();
  } else {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    revealItems.forEach(function (item) { observer.observe(item); });
  }

  /* Keep buttons lively without making anything chase the pointer. */
  Array.prototype.slice.call(document.querySelectorAll(".button")).forEach(function (button) {
    button.classList.add("motion-button");
  });

  function shouldTransition(link, event) {
    if (reduceMotion || event.defaultPrevented || event.button !== 0) return false;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (link.target && link.target !== "_self") return false;
    if (link.hasAttribute("download")) return false;
    var raw = link.getAttribute("href");
    if (!raw || raw.charAt(0) === "#" || raw.indexOf("mailto:") === 0 || raw.indexOf("tel:") === 0) return false;
    var url;
    try { url = new URL(raw, window.location.href); } catch (e) { return false; }
    if (url.origin !== window.location.origin) return false;
    if (url.pathname === window.location.pathname && url.search === window.location.search) return false;
    return url;
  }

  Array.prototype.slice.call(document.querySelectorAll("a[href]")).forEach(function (link) {
    link.addEventListener("click", function (event) {
      var url = shouldTransition(link, event);
      if (!url) return;
      event.preventDefault();
      body.classList.add("page-leaving");
      window.setTimeout(function () { window.location.href = url.href; }, 520);
    });
  });
})();
