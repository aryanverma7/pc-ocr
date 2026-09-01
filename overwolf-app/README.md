# DualBladeX Game Events (Overwolf app)

Reports live Valorant round state from the gaming PC to the Mac Mini backend.

This is the second thing that runs on the gaming PC, alongside `agent.py`. They do not
talk to each other and neither depends on the other running.

## What it reports

A snapshot of nine fields, POSTed to `/api/game/state` whenever any of them changes and
every 15 seconds regardless:

| field | from | why |
|---|---|---|
| `round_phase` | `match_info.round_phase` | `shopping` / `combat` / `end` / `game_end` — the buy-phase signal `agent.py` has only ever been able to guess at |
| `round_number` | `match_info.round_number` | |
| `score` | `match_info.score` | `{"won": n, "lost": n}` — the backend diffs it to get round results |
| `match_outcome` | `match_info.match_outcome` | `victory` / `defeat` / `draw` |
| `match_id`, `map`, `game_mode` | `match_info` | context for the dashboard |
| `money` | `match_info.scoreboard`, the row with `is_local` | the local player's **current** credits |
| `agent` | `me.agent` | sets the roulette's ability reserve, so nobody has to type `!agent` |

`money` is **not** the same number `credit_ocr` reads. Valorant's "MIN NEXT ROUND" is a
projection of next round's balance; this is what you hold right now. They agree during a
buy phase and nowhere else. Nothing on the Mac Mini makes a decision from this field yet —
it is shown on the dashboard next to the OCR reading so the two can be compared over a real
session before either replaces the other.

## Riot Games compliance

Overwolf's Riot compliance guidelines apply **"even if you do not intend to use the Riot API
itself"**, which this app does not — its data comes from Overwolf's Game Events Provider. Two
consequences:

- The disclaimer Overwolf mandates is carried in `manifest.json`'s description and shown in the
  status window. Its wording is theirs, verbatim, and should not be paraphrased.
- Publication requires Riot's own approval through their [third-party application
  process](https://developer.riotgames.com/docs/portal#_getting-started), in addition to
  Overwolf's whitelisting. Loading the app unpacked for personal use does not, but Overwolf asks
  for the Riot approval before whitelisting a developer account.

## Install

Overwolf is Windows-only, so this goes on the gaming PC, not the Mac Mini.

1. Install [Overwolf](https://www.overwolf.com/) and let it start.
2. Open Overwolf settings → **About** → **Development options** → enable them.
3. **Load unpacked extension**, and pick this folder (the one with `manifest.json`).
4. Edit `app_config.js` and set `agentSecret` to the same value as `agent_config.json`'s
   `agent_secret` and the Mac Mini's `ocr_agent_secret`. One secret, all three files.
5. Launch Valorant. The app auto-starts on game launch and opens its status window.

The status window is a debug view, not the app — closing it changes nothing. It reports the
four things that fail independently:

- **Game** — was Valorant detected, and under which id
- **GEP features** — did Overwolf accept the subscription
- **Backend** — is the Mac Mini answering, and with what
- the tracked fields themselves, in **teal** when populated and **amber** when empty

An amber field with everything else green is the interesting failure: it means GEP stopped
reporting something after a Valorant patch. That is invisible if everything renders the
same way, which is why it doesn't.

## If it reports nothing at all

**Check the game id first.** Overwolf does not publish their game id list anywhere in their
docs, so `21640` is a well-attested value rather than a confirmed one. The status window
prints the real id of whatever game it sees start (`Game started: ... (id NNNNN)`), and the
authoritative list is `%localappdata%\overwolf\gameList.xml` on this machine.

If the id is wrong, fix it in **two** places — `app_config.js`'s `games` map, and
`manifest.json`'s `game_targeting.game_ids` and `game_events`. The manifest is JSON and
cannot read the config file, so they cannot be shared.

Other things worth knowing:

- `setRequiredFeatures` failing right after the game starts is normal — the provider is not
  up yet. It retries with a backoff and the status window says so.
- A `401` means `agentSecret` doesn't match `ocr_agent_secret` on the Mac Mini.
- `Snapshots sent` climbing while the tracked fields stay amber means the app is talking to
  the backend fine and GEP is giving it nothing — that is an Overwolf/Valorant problem, not
  a config one.

## Adding another game

The app is structured for it (you play more than Valorant), but only Valorant is wired up:

1. Add the game id to `manifest.json`'s `game_targeting.game_ids` and `game_events`.
2. Add an entry to `APP_CONFIG.games` in `app_config.js` with its GEP feature names.
3. Teach `absorb()` in `background.js` how to read that game's info payload — the field
   names are per-game and nothing about Valorant's shape carries over.

Only the third step is real work.
