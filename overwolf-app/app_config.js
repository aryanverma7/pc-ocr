/*
 * The two values that differ per install, kept in their own file for the
 * same reason agent_config.json is separate from agent.py: a `git pull`
 * must never overwrite a secret, and editing one line must never mean
 * reading the code around it.
 *
 * AGENT_SECRET is the SAME value as agent_config.json's agent_secret and
 * as the Mac Mini's ocr_agent_secret in config.json. One secret, one
 * machine, one trust boundary - a second value would just be a second
 * thing to get out of sync across three files on two machines.
 */
const APP_CONFIG = {
  // Same host as the OCR agent posts to, different path.
  backendUrl: "https://hub.dualbladex.org/api/game/state",
  agentSecret: "",

  /*
   * Overwolf's game ids. 21640 is Valorant, and it is here rather than
   * hardcoded because Overwolf does not publish this list in their docs -
   * it lives in %localappdata%\overwolf\gameList.xml on this machine, and
   * the status window prints the real id of whatever game it sees start,
   * so a wrong guess is a thirty-second fix rather than an app that
   * silently never fires.
   *
   * These must match manifest.json's game_targeting.game_ids and
   * game_events, which is JSON and cannot read this file. Change both.
   */
  games: {
    21640: {
      name: "valorant",
      // Confirmed against Overwolf's Valorant GEP docs. gep_internal is
      // not requested: it reports the provider's own version and nothing
      // here reads it.
      features: ["me", "game_info", "match_info"],
    },
  },

  // How often to send a snapshot even when nothing has changed. This IS
  // the liveness ping - the Mac Mini's game_events.SNAPSHOT_TIMEOUT_SECONDS
  // is 45, three of these, so two may be dropped before the dashboard is
  // allowed to say the gaming PC is dead. Those two numbers are one fact:
  // change this and change that.
  heartbeatSeconds: 15,

  // Nothing is sent faster than this, however fast GEP fires. The
  // scoreboard updates on every damage tick and the fields we keep change
  // a handful of times a round, but a burst of purchases can still move
  // `money` several times a second and none of those intermediate values
  // is worth a request.
  minPostIntervalMs: 400,
};
