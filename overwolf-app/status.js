/*
 * A read-only view of the background page's state.
 *
 * This exists because the first question anybody asks of this app is "is
 * it working", and the honest answer has four parts that fail
 * independently: is the game detected, did GEP accept the feature
 * subscription, is the backend answering, and is any of the data actually
 * populated. An app that shows one green light for all four would hide
 * exactly the failures that matter - a wrong game id looks identical to a
 * wrong secret from the outside.
 *
 * It polls rather than subscribing: this is a debug window that is usually
 * closed, and half a second of staleness costs nothing.
 */
function paint(el, text, cls) {
  el.textContent = text;
  el.className = "v" + (cls ? " " + cls : "");
}

function render(state) {
  paint(
    document.getElementById("game"),
    state.gameRunning ? state.gameName + " (id " + state.gameId + ")" : "not running",
    state.gameRunning ? "ok" : ""
  );
  paint(
    document.getElementById("features"),
    state.featuresOk ? "subscribed" : state.gameRunning ? "not subscribed" : "-",
    state.featuresOk ? "ok" : state.gameRunning ? "bad" : ""
  );

  const status = state.lastPostStatus || "-";
  paint(document.getElementById("backend"), status, status === "ok" ? "ok" : "bad");
  document.getElementById("posts").textContent =
    state.postCount + (state.errorCount ? "  (" + state.errorCount + " failed)" : "");

  const t = state.tracked || {};
  /* A populated field and an absent one are shown differently on purpose.
     GEP going quiet on one field while the rest keep updating is a real
     failure mode after a Valorant patch, and it is invisible if every
     empty value renders as a dash in the same colour as a real one. */
  const show = (id, value, format) => {
    const el = document.getElementById(id);
    if (value === null || value === undefined || value === "") return paint(el, "-", "warn");
    paint(el, format ? format(value) : String(value), "ok");
  };

  show("phase", t.round_phase);
  show("round", t.round_number);
  show("score", t.score, (s) => s.won + " : " + s.lost);
  show("money", t.money, (m) => "¤" + m);
  show("agent", t.agent);
  show("map", t.map ? t.map + (t.game_mode ? " / " + t.game_mode : "") : null);
  show("outcome", t.match_outcome);

  document.getElementById("log").textContent = (state.log || []).join("\n");
}

function tick() {
  overwolf.windows.getMainWindow((main) => {
    if (main && main.dbxGameEvents) render(main.dbxGameEvents);
  });
}

tick();
setInterval(tick, 500);
