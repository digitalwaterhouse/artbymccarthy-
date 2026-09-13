/* The dark-mode button. See templates/_theme.html.
   The anti-flash half is inlined in each <head> -- it has to run before first
   paint and an external file cannot. This file only keeps the button in step. */
(function () {
  var KEY = "abm-theme";
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function stored() {
    try { var v = localStorage.getItem(KEY); return v === "dark" || v === "light" ? v : null; }
    catch (e) { return null; }
  }

  /* What the reader is ACTUALLY looking at: their own choice if they have made
     one, otherwise whatever the machine says. On a first visit there is no
     stored value and the button still has to show the right glyph. */
  function effective() {
    return stored() || (media && media.matches ? "dark" : "light");
  }

  function paint() {
    var dark = effective() === "dark";
    document.querySelectorAll(".tsw").forEach(function (b) {
      /* data-goes is what the button WILL DO, which is also what it draws. */
      b.setAttribute("data-goes", dark ? "light" : "dark");
      b.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    });
  }

  window.abmTheme = function () {
    var next = effective() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(KEY, next); } catch (e) {}
    paint();
  };

  /* If they have not chosen, the machine is still in charge -- so a laptop
     going dark at sunset swaps the glyph on a page that is already open. */
  if (media && media.addEventListener) {
    media.addEventListener("change", function () { if (!stored()) paint(); });
  }

  paint();
})();
