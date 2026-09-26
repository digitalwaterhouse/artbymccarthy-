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

  /* THE HOME PAGE NEEDS A WORD. Its top is dark in both modes on purpose (the
     black boxes are built to sink into it), so a press there changed nothing
     anyone could see -- the pages that did change were below the fold. On the
     home page only, say what happened, briefly. aria-live so a screen reader
     hears it too. Elsewhere the whole page changing is its own answer. */
  var noteTimer;
  function say(mode) {
    if (!document.body || !document.body.classList.contains("home")) return;
    var n = document.querySelector(".tsw-note");
    if (!n) {
      n = document.createElement("p");
      n.className = "tsw-note";
      n.setAttribute("role", "status");
      n.setAttribute("aria-live", "polite");
      document.body.appendChild(n);
    }
    n.textContent = (mode === "dark" ? "Dark mode" : "Light mode") +
                    " \u2014 the pages below are " + mode;
    n.classList.add("show");
    clearTimeout(noteTimer);
    noteTimer = setTimeout(function () { n.classList.remove("show"); }, 2600);
  }

  /* SMOOTH, NOT SNAPPED (2026-09-26). Where the browser has View Transitions
     the whole page cross-dissolves from the old palette to the new one --
     one GPU fade over a snapshot, so nothing re-lays out mid-change. The
     html.theme-vt class swaps the page-to-page animation (which slides) for a
     plain dissolve. Without View Transitions, html.theme-ease turns on colour
     transitions everywhere for just long enough to cover the change. Reduced
     motion gets the instant switch it asked for. */
  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
  function apply(next) {
    document.documentElement.setAttribute("data-theme", next);
    paint();
  }

  window.abmTheme = function () {
    var next = effective() === "dark" ? "light" : "dark";
    try { localStorage.setItem(KEY, next); } catch (e) {}
    var root = document.documentElement;
    if (calm && calm.matches) {
      apply(next);
    } else if (document.startViewTransition) {
      root.classList.add("theme-vt");
      var vt = document.startViewTransition(function () { apply(next); });
      /* A second press mid-fade skips the first transition, which rejects
         .ready -- swallow it, the newer press is already handling things. */
      var done = function () { root.classList.remove("theme-vt"); };
      vt.ready.catch(function () {});
      vt.finished.then(done, done);
    } else {
      root.classList.add("theme-ease");
      apply(next);
      setTimeout(function () { root.classList.remove("theme-ease"); }, 500);
    }
    say(next);
  };

  /* If they have not chosen, the machine is still in charge -- so a laptop
     going dark at sunset swaps the glyph on a page that is already open. */
  if (media && media.addEventListener) {
    media.addEventListener("change", function () { if (!stored()) paint(); });
  }

  paint();
})();
