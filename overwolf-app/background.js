/*
 * The controller. Subscribes to Overwolf's Game Events Provider and POSTs
 * a snapshot of the handful of fields the Mac Mini cares about.
 *
 * Design notes worth keeping:
 *
 * A full snapshot every time, not a diff. GEP is a firehose - the
 * scoreboard alone updates on every damage tick - and almost none of it
 * matters here, so this file filters down to TRACKED fields and only sends
 * when one of THOSE changes. Sending the whole picture each time makes a
 * dropped POST cost nothing: the next one carries everything, so there is
 * no ordering problem and no resync protocol to get wrong.
 *
 * The same POST is the liveness ping. A timed snapshot with nothing
 * changed is how the Mac Mini knows this machine is still here, which is
 * one mechanism instead of the two the OCR agent needed - its captures
 * only travel during a burst, so their absence never meant anything.
 *
 * GEP hands back JSON-encoded STRINGS for anything structured. `score`
 * arrives as the literal text {"won":9,"lost":2} and `scoreboard` as an
 * encoded array, so every structured read goes through parseMaybe() rather
 * than being trusted to already be an object. Getting this wrong produces
 * an app that runs perfectly and reports nothing.
 *
 * setRequiredFeatures is retried. Called too soon after the game starts it
 * fails with the provider not being ready yet, which is expected rather
 * than exceptional - Overwolf's own sample app retries it the same way.
 */

const TRACKED = [
  "round_phase",
  "round_number",
  "score",
  "match_outcome",
  "match_id",
  "map",
  "game_mode",
  "money",
  "agent",
];

const FEATURE_RETRY_DELAYS_MS = [1000, 2000, 3000, 5000, 8000, 10000];

const state = {
  gameId: null,
  gameName: null,
  gameRunning: false,
  featuresOk: false,
  tracked: {},
  lastPostAt: 0,
  lastPostStatus: "nothing sent yet",
  lastPostAppliedFields: [],
  postCount: 0,
  errorCount: 0,
  log: [],
};

/* The status window reads this. A plain global on the background page is
   how Overwolf apps share state between windows - the status window calls
   overwolf.windows.getMainWindow() and gets this exact object. */
window.dbxGameEvents = state;

function note(line) {
  const stamped = new Date().toLocaleTimeString() + "  " + line;
  state.log.unshift(stamped);
  if (state.log.length > 120) state.log.length = 120;
  console.log(stamped);
}

/* GEP encodes structured values as JSON strings. Anything that is already
   an object is passed straight through, because that is not guaranteed to
   stay true and a version that only handles one shape would break quietly
   on a provider update rather than loudly. */
function parseMaybe(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "object") return value;
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text) return null;
  if (text[0] !== "{" && text[0] !== "[") return value;
  try {
    return JSON.parse(text);
  } catch (e) {
    return value;
  }
}

function toInt(value) {
  const n = parseInt(value, 10);
  return Number.isFinite(n) ? n : null;
}

/* Overwolf reports booleans as real booleans in some builds and as the
   strings "true"/"false" in others. Both mean the same thing and only one
   of them is truthy on its own. */
function isTrue(value) {
  return value === true || value === "true" || value === 1 || value === "1";
}

function localFromScoreboard(scoreboard) {
  const parsed = parseMaybe(scoreboard);
  if (!parsed) return null;
  const rows = Array.isArray(parsed) ? parsed : Object.values(parsed);
  for (const row of rows) {
    const entry = parseMaybe(row);
    if (entry && typeof entry === "object" && isTrue(entry.is_local)) return entry;
  }
  return null;
}

/* ------------------------------------------------------------------
   Reading one GEP info payload into the fields we keep
   ------------------------------------------------------------------ */
function absorb(info) {
  if (!info || typeof info !== "object") return;

  const match = info.match_info;
  if (match) {
    if ("round_phase" in match) state.tracked.round_phase = match.round_phase || null;
    if ("round_number" in match) state.tracked.round_number = toInt(match.round_number);
    if ("match_outcome" in match) state.tracked.match_outcome = match.match_outcome || null;
    if ("match_id" in match) state.tracked.match_id = match.match_id || null;
    if ("map" in match) state.tracked.map = match.map || null;
    if ("game_mode" in match) state.tracked.game_mode = match.game_mode || null;

    if ("score" in match) {
      const score = parseMaybe(match.score);
      /* Normalised to plain integers here rather than passed along as
         whatever GEP said, because the Mac Mini compares this to the
         previous snapshot to decide whether a round was won - and "9"
         does not equal 9, so a string would read as a change every time
         and as a result never. */
      state.tracked.score =
        score && typeof score === "object"
          ? { won: toInt(score.won), lost: toInt(score.lost) }
          : null;
    }

    if ("scoreboard" in match) {
      const local = localFromScoreboard(match.scoreboard);
      if (local) {
        if ("money" in local) state.tracked.money = toInt(local.money);
        /* Fallback only - `me.agent` below is the direct answer. This is
           here because the scoreboard is present in matches where `me`
           has not populated yet. */
        if (!state.tracked.agent && local.character) state.tracked.agent = local.character;
      }
    }
  }

  const me = info.me;
  if (me && me.agent) state.tracked.agent = me.agent;

  /* game_info.scene tells us the client is at the menu rather than in a
     match. Not tracked as a field - the Mac Mini has no use for it - but
     leaving a stale round_phase behind when the match ends would make the
     dashboard show a buy phase that ended ten minutes ago. */
  const game = info.game_info;
  if (game && game.scene && /menu|lobby/i.test(String(game.scene))) {
    state.tracked.round_phase = null;
  }
}

function snapshotChanged(before) {
  return TRACKED.some((field) => JSON.stringify(before[field]) !== JSON.stringify(state.tracked[field]));
}

/* ------------------------------------------------------------------
   Sending
   ------------------------------------------------------------------ */
async function postSnapshot(reason) {
  if (!APP_CONFIG.agentSecret) {
    state.lastPostStatus = "agentSecret is empty in app_config.js - nothing will be sent";
    return;
  }

  const body = {
    app_version: "1.0.0",
    reason: reason,
    game_id: state.gameId,
    game_name: state.gameName,
    game_running: state.gameRunning,
    state: {},
  };
  for (const field of TRACKED) {
    body.state[field] = state.tracked[field] === undefined ? null : state.tracked[field];
  }

  state.lastPostAt = Date.now();
  try {
    const response = await fetch(APP_CONFIG.backendUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Secret": APP_CONFIG.agentSecret,
      },
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      state.errorCount++;
      state.lastPostStatus = "401 - agentSecret does not match the Mac Mini's ocr_agent_secret";
      note(state.lastPostStatus);
      return;
    }
    if (!response.ok) {
      state.errorCount++;
      state.lastPostStatus = "HTTP " + response.status;
      note("Backend answered " + response.status);
      return;
    }
    const answer = await response.json();
    state.postCount++;
    state.lastPostAppliedFields = answer.applied || [];
    state.lastPostStatus = "ok";
    /* Only a change is worth a line. The heartbeat is every fifteen
       seconds forever and would bury everything else in this log. */
    if (state.lastPostAppliedFields.length) {
      note("Sent (" + reason + ") - backend applied: " + state.lastPostAppliedFields.join(", "));
    }
  } catch (e) {
    state.errorCount++;
    state.lastPostStatus = "could not reach the backend: " + e.message;
    note(state.lastPostStatus);
  }
}

let postTimer = null;
function postSoon(reason) {
  /* Coalescing rather than dropping. A burst of purchases moves `money`
     several times a second and none of the intermediate values matters,
     but the LAST one does - so a pending send is left to fire rather than
     each new change starting its own. */
  if (postTimer) return;
  postTimer = setTimeout(() => {
    postTimer = null;
    postSnapshot(reason);
  }, APP_CONFIG.minPostIntervalMs);
}

/* ------------------------------------------------------------------
   GEP wiring
   ------------------------------------------------------------------ */
function setFeatures(attempt) {
  const game = APP_CONFIG.games[state.gameId];
  if (!game) {
    note("No feature list configured for game id " + state.gameId + " - add it to app_config.js");
    return;
  }

  overwolf.games.events.setRequiredFeatures(game.features, (result) => {
    if (result && result.success) {
      state.featuresOk = true;
      note("Subscribed to " + game.name + " features: " + game.features.join(", "));
      /* The current values, not just future changes. Without this the app
         reports nothing at all until the next round begins - joining a
         match already in progress would look exactly like a broken app. */
      overwolf.games.events.getInfo((info) => {
        if (info && info.success && info.res) {
          absorb(info.res);
          postSnapshot("initial");
        }
      });
      return;
    }

    state.featuresOk = false;
    const delay = FEATURE_RETRY_DELAYS_MS[Math.min(attempt, FEATURE_RETRY_DELAYS_MS.length - 1)];
    /* Expected, not exceptional: called too soon after the game starts,
       this fails because the provider has not come up yet. Overwolf's own
       sample app retries it the same way. */
    note("Features not ready (" + (result && result.error) + ") - retrying in " + delay + "ms");
    setTimeout(() => setFeatures(attempt + 1), delay);
  });
}

function listen() {
  overwolf.games.events.onInfoUpdates2.removeListener(onInfoUpdate);
  overwolf.games.events.onInfoUpdates2.addListener(onInfoUpdate);
  overwolf.games.events.onNewEvents.removeListener(onNewEvent);
  overwolf.games.events.onNewEvents.addListener(onNewEvent);
}

function onInfoUpdate(update) {
  if (!update || !update.info) return;
  const before = Object.assign({}, state.tracked);
  absorb(update.info);
  if (snapshotChanged(before)) postSoon("info");
}

function onNewEvent(payload) {
  /* Events carry the same fields as info updates for everything this app
     tracks - match_start, round_start, match_end and so on all restate
     match_info - so they are only worth a nudge to re-read. The one thing
     they add is timing: an event fires at the transition, where an info
     update can arrive slightly after it. */
  if (!payload || !payload.events) return;
  for (const event of payload.events) {
    if (!event || !event.name) continue;
    note("Game event: " + event.name);
  }
  overwolf.games.events.getInfo((info) => {
    if (info && info.success && info.res) {
      const before = Object.assign({}, state.tracked);
      absorb(info.res);
      if (snapshotChanged(before)) postSoon("event");
    }
  });
}

/* ------------------------------------------------------------------
   Game detection
   ------------------------------------------------------------------ */
function gameIdOf(info) {
  /* Overwolf reports a game id with a one-digit instance suffix appended,
     so the id in the manifest is this divided by ten. classId is already
     the divided form when it is present, which it is not on every build. */
  if (!info) return null;
  if (info.classId) return info.classId;
  if (info.id) return Math.floor(info.id / 10);
  return null;
}

function onGameRunning(info) {
  const id = gameIdOf(info);
  const running = !!(info && info.isRunning);

  if (running && !state.gameRunning) {
    state.gameId = id;
    state.gameName = (APP_CONFIG.games[id] && APP_CONFIG.games[id].name) || (info && info.title) || "unknown";
    state.gameRunning = true;
    /* Printed on every launch, deliberately. Overwolf does not publish
       their game id list, so this line is how a wrong id in app_config.js
       gets found - it is the actual number, from the actual machine. */
    note("Game started: " + state.gameName + " (id " + id + ")");
    if (!APP_CONFIG.games[id]) {
      note("This id is not in app_config.js - add it there and in manifest.json to report on this game.");
      return;
    }
    setFeatures(0);
    listen();
    postSnapshot("game-start");
    return;
  }

  if (!running && state.gameRunning) {
    note("Game closed");
    state.gameRunning = false;
    state.featuresOk = false;
    state.tracked = {};
    /* One last snapshot so the dashboard stops showing a match that has
       ended, rather than waiting out the staleness timeout to find out. */
    postSnapshot("game-closed");
  }
}

function start() {
  note("DualBladeX game events app started");

  overwolf.games.onGameInfoUpdated.addListener((update) => {
    if (update && update.gameInfo) onGameRunning(update.gameInfo);
  });
  overwolf.games.getRunningGameInfo((info) => onGameRunning(info));

  setInterval(() => {
    /* The heartbeat. Sent whether or not anything changed, because its
       job is to prove this machine is alive, and a quiet match is the
       normal state for most of a round. */
    postSnapshot("heartbeat");
  }, APP_CONFIG.heartbeatSeconds * 1000);

  /* Opened rather than left for the dock, because the first thing anybody
     needs from this app is to see whether it is working. Closing it does
     not stop the app - the background page is what runs. */
  overwolf.windows.obtainDeclaredWindow("status", (result) => {
    if (result && result.success) overwolf.windows.restore(result.window.id, () => {});
  });
}

start();
