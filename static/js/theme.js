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

  window.abmTheme = function () {
    var next = effective() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(KEY, next); } catch (e) {}
    paint();
    say(next);
  };

  /* If they have not chosen, the machine is still in charge -- so a laptop
     going dark at sunset swaps the glyph on a page that is already open. */
  if (media && media.addEventListener) {
    media.addEventListener("change", function () { if (!stored()) paint(); });
  }

  paint();
})();
