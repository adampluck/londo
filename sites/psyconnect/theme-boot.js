/* Light/dark boot + toggle for psyconnect.

   build_site.py inlines this into the <head> of every page — the SPA shell
   at its THEME-BOOT marker, the static /e/ /t/ pages in page() — so the
   saved choice is on <html> before the first paint and there is no flash of
   the wrong theme. It is deliberately tiny and dependency-free for that
   reason; the rest of the theming is CSS (light-dark() in theme.css).

   No stored choice means "follow the system", which is the default. */
(function () {
  var KEY = "psyconnect-theme";
  var root = document.documentElement;
  var media = window.matchMedia("(prefers-color-scheme: dark)");
  // must match --dusk-0's two values in theme.css
  var LIGHT = "#f6efe4";
  var DARK = "#191310";

  var saved = null;
  try {
    saved = localStorage.getItem(KEY);
  } catch (e) {
    /* private mode / storage disabled — system preference still works */
  }
  if (saved === "dark" || saved === "light") root.dataset.theme = saved;

  function isDark() {
    return root.dataset.theme ? root.dataset.theme === "dark" : media.matches;
  }

  function sync() {
    var dark = isDark();
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? DARK : LIGHT);
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.setAttribute(
        "aria-label",
        dark ? "Switch to light theme" : "Switch to dark theme"
      );
      btn.setAttribute("aria-pressed", dark ? "true" : "false");
    }
  }

  function toggle() {
    var next = isDark() ? "light" : "dark";
    root.dataset.theme = next;
    try {
      localStorage.setItem(KEY, next);
    } catch (e) {
      /* the theme still applies for this page view */
    }
    sync();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", toggle);
    sync();
  });

  // while no explicit choice is stored we follow the system live
  media.addEventListener("change", sync);
})();
