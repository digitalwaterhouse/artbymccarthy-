/* Light, dark, or follow the device -- shared by the shop and the studio.
   See templates/_theme.html. Nothing here runs before paint; the anti-flash
   half is a three-line script inlined in each <head>, because a stylesheet
   cannot wait for a file to download and neither can the reader. */
(function () {
  var KEY = "abm-theme";
  var ORDER = ["auto", "dark", "light"];
  var SAID = {auto: "Theme follows this device", dark: "Dark", light: "Light"};

  function current() {
    var t = document.documentElement.getAttribute("data-theme");
    return t === "dark" || t === "light" ? t : "auto";
  }

  function paint() {
    var now = current();
    document.querySelectorAll(".lamp").forEach(function (b) {
      b.setAttribute("data-mode", now);
      /* The button announces the STATE it is in, not the one it would move to:
         a screen reader user pressing it hears what happened. */
      b.setAttribute("aria-label", SAID[now] + " -- press to change");
      var said = b.querySelector(".lamp-said");
      if (said) said.textContent = SAID[now];
    });
  }

  window.abmTheme = function () {
    var next = ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length];
    if (next === "auto") {
      document.documentElement.removeAttribute("data-theme");
      try { localStorage.removeItem(KEY); } catch (e) {}
    } else {
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (e) {}
    }
    paint();
  };

  paint();
})();
