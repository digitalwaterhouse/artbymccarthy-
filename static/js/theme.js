/* The dark-mode switch. See templates/_theme.html.
   The anti-flash half is inlined in each <head> -- it has to run before first
   paint and an external file cannot. This file only keeps the switch in step. */
(function () {
  var KEY = "abm-theme";
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function stored() {
    try { var v = localStorage.getItem(KEY); return v === "dark" || v === "light" ? v : null; }
    catch (e) { return null; }
  }

  /* What the reader is ACTUALLY looking at: their own choice if they have made
     one, otherwise whatever the machine says. The switch shows this, not the
     stored value -- on a first visit there is no stored value and the knob
     still has to be in the right place. */
  function effective() {
    return stored() || (media && media.matches ? "dark" : "light");
  }

  function paint() {
    var dark = effective() === "dark";
    document.querySelectorAll(".tsw").forEach(function (b) {
      b.setAttribute("aria-checked", dark ? "true" : "false");
    });
  }

  window.abmTheme = function () {
    var next = effective() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(KEY, next); } catch (e) {}
    paint();
  };

  /* If they have not chosen, the machine is still in charge -- so a laptop
     going dark at sunset moves the knob on a page that is already open. */
  if (media && media.addEventListener) {
    media.addEventListener("change", function () { if (!stored()) paint(); });
  }

  paint();
})();
