/* Explicit, allowlisted marketing events only. No vendor script or autocapture. */
(() => {
  "use strict";
  if (window.FilterSightAnalytics) return;
  const endpoint = "https://gateway.umami.is/api/send";
  const publicHosts = ["filtersight.com", "www.filtersight.com"];
  const signupHost = "signup-app-v3-production.up.railway.app";
  const pages = {
    "/": "FilterSight", "/index.html": "FilterSight",
    "/founder-story.html": "Our story", "/mission.html": "Mission",
    "/privacy.html": "Privacy", "/terms.html": "Terms"
  };
  const seen = new Set();
  const pending = new Set();
  function optedOut() {
    try {
      return navigator.globalPrivacyControl === true || navigator.doNotTrack === "1" ||
        localStorage.getItem("filtersight.analytics.disabled") === "1";
    } catch (_) { return true; }
  }
  function campaign(config, input) {
    const out = {};
    for (const [key, values] of Object.entries(config.campaigns || {})) {
      const value = input.get(key);
      if (values.includes(value)) out[key] = value;
    }
    return out;
  }
  function referral() {
    try {
      const host = new URL(document.referrer).hostname;
      if (["x.com", "www.x.com", "twitter.com", "www.twitter.com", "t.co"].includes(host)) return "https://x.com/";
      if (["google.com", "www.google.com"].includes(host)) return "https://www.google.com/";
      if (["bing.com", "www.bing.com"].includes(host)) return "https://www.bing.com/";
      if (["duckduckgo.com", "www.duckduckgo.com"].includes(host)) return "https://duckduckgo.com/";
    } catch (_) { /* Direct visit, or invalid referrer. */ }
    return "";
  }
  async function send(config, path, title, tags, name, data, onceKey) {
    if (optedOut() || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(config.website_id || "")) return;
    if (onceKey) {
      if (seen.has(onceKey) || pending.has(onceKey)) return;
      try { if (sessionStorage.getItem(onceKey)) return; } catch (_) { /* In-memory dedup still works. */ }
      pending.add(onceKey);
    }
    const query = new URLSearchParams(tags).toString();
    const payload = {
      website: config.website_id, hostname: "filtersight.com",
      url: path + (query ? "?" + query : ""), title,
      referrer: referral(), language: navigator.language
    };
    if (name) { payload.name = name; payload.data = data || {}; }
    try {
      const response = await fetch(endpoint, {
        method: "POST", credentials: "omit", referrerPolicy: "no-referrer",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "event", payload }), keepalive: true
      });
      if (response.ok && onceKey) {
        seen.add(onceKey);
        try { sessionStorage.setItem(onceKey, "1"); } catch (_) { /* Optional duplicate protection. */ }
      }
    } catch (_) { /* Analytics must never prevent navigation or payment. */ }
    finally { if (onceKey) pending.delete(onceKey); }
  }
  async function publicPage() {
    if (!publicHosts.includes(location.hostname) || !pages[location.pathname]) return;
    let config;
    try {
      const response = await fetch("/analytics_config.json", { credentials: "omit" });
      if (!response.ok) return;
      config = await response.json();
    } catch (_) { return; }
    const tags = optedOut() ? {} : campaign(config, new URLSearchParams(location.search));
    document.querySelectorAll("a[href]").forEach(link => {
      const url = new URL(link.href, location.href);
      const signup = url.hostname === signupHost && url.pathname === "/" && !url.searchParams.has("view");
      const internal = url.origin === location.origin && pages[url.pathname];
      if (signup || internal) {
        for (const [key, value] of Object.entries(tags)) url.searchParams.set(key, value);
        if (signup && optedOut()) url.searchParams.set("fs_analytics", "off");
        link.href = url.href;
      }
      const placement = link.getAttribute("data-fs-start");
      if (["hero", "pricing"].includes(placement)) {
        link.addEventListener("click", () => {
          void send(config, location.pathname, pages[location.pathname], tags, "Start FilterSight", { placement });
        });
      }
    });
    void send(config, location.pathname, pages[location.pathname], tags);
  }
  function signupEvent(config, event, tags, onceKey) {
    if (location.hostname !== signupHost || new URLSearchParams(location.search).get("view") === "member") return;
    const events = {
      "Signup viewed": ["/signup", "FilterSight signup"],
      "Checkout started": ["/signup", "FilterSight signup"],
      "Paid signup": ["/signup/success", "FilterSight signup complete"]
    };
    if (!events[event]) return;
    const cleanTags = campaign(config, new URLSearchParams(tags));
    const [path, title] = events[event];
    void send(config, path, title, cleanTags, event, {}, onceKey);
  }
  window.FilterSightAnalytics = { publicPage, signupEvent };
})();
