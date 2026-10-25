# VLite — lightweight Valorant tracker (Windows)

A single ~1,700-line Python file, no dependencies. Shows what the Tracker.gg app shows at its core:

- **Live match** (agent select + in game), for every teammate and opponent:
  - agent, name, level, rank + RR, peak rank, act win rate
  - recent form from their last 5 competitive games: KD, ACS, HS%, W/L. This fills in a few seconds after the lobby loads.
  - **Mains**: their most-played agents in those games, so you can plan comps in agent select.
  - **On this agent**: how they've done on the agent they're playing now, or "new pick".
  - **Played before**: whether you've met them, e.g. `With 2W 1L · Vs 0W 1L`
  - **Last met**: when and where, e.g. `3d ago · Ascent · Jett (vs)`
  - **Party**: players with the same coloured dot queued together. Your own party comes straight from Riot. For everyone else it's inferred: two players who were on the same team in 2+ of their last 3 competitive games are marked as a premade.
- Agent portraits, rank badges and the map banner are downloaded once from valorant-api.com and saved to `%LOCALAPPDATA%\VLite\img`. Rank names are built in, so they show even offline.
- **⚑ Flags**: your private notes on players ("threw", "AFK", "great duo"). Right-click anyone in the Live tab to flag them, or open **⚑ Flags** after a match to flag players from your last 3 lobbies. Flagged players show up in orange with your note when you meet them again. Only players whose names were visible can be flagged, and a flag never shows on someone who's hidden. Notes are stored only on your PC.
- **My profile**: current rank/RR/peak, your **session** (W/L, RR, streak and KD for games less than 3 hours apart), a **rank-progress graph** of your last 20 competitive games (hover a point for details), and your last 20 matches with map, agent, result, score, K/D/A, KD, HS%, ACS and RR change. Double-click a match to see its **scoreboard**. Names show only for players VLite saw with visible names in that match.
- **Stats**: your competitive win %, KD, ACS and HS% per map and per agent, from every match saved on this PC. "Load 20 older matches" pulls in more history.
- **Store**: your daily offers and Night Market with prices, discounts and reset timers, plus your VP, Radianite and Kingdom Credits. It's only loaded when you click "Load my store".

"Played before" checks every match VLite has saved. On first launch that's your last 20 games, and it grows each time you play.

## Install as an app (one time)
1. Install Python 3.9+ from python.org (tick **Add python.exe to PATH**).
2. Put `vlite.py` and `build.bat` in one folder and double-click `build.bat`.
3. You get `dist\VLite.exe`. Copy it anywhere (Desktop, Start menu), pin it to the taskbar, and you can uninstall Python if you like.

Windows SmartScreen may warn the first time because the exe isn't code-signed. Click **More info → Run anyway**.
If you'd rather skip building, double-click `VLite.bat` to run it straight from Python.

## In-game overlay
- Set VALORANT to **Windowed Fullscreen** (Settings → Video → Display Mode). Exclusive fullscreen draws over every other window.
- Press **F6** in game to show or hide the overlay.
- **Settings** (button in the main window) lets you change the hotkey, corner, size (Small/Normal/Large), opacity, and which columns the overlay shows. Changes apply instantly and are remembered.
- The overlay is click-through and never takes focus, so your aim and keys stay in the game.
- **Settings → Automation**: show the overlay automatically in agent select, hide it when the match starts, pop it up when a flagged player is in your lobby, and show session stats in the overlay title.

## Tray, notifications, startup
- Closing the window keeps VLite running in the system tray (bottom-right, near the clock; it may sit under the ^ arrow). Click the icon to reopen it, or right-click for *Show / hide overlay* and *Quit VLite*.
- Windows notifications for **Match found** and **Flagged player in your lobby**. Windows' gaming Focus Assist can hold notifications while a game is fullscreen. The overlay alert still works.
- **Start VLite with Windows** is in Settings. It adds a normal per-user startup entry and doesn't need admin rights.
- All of these can be switched off in Settings.

## Ban safety
- No memory reading, injection, or graphics hooking. The overlay isn't drawn inside the game. It's a normal, transparent, always-on-top window, like a Discord or OBS window. The hotkey uses Windows' standard `RegisterHotKey` instead of a keyboard hook.
- Read-only. It only uses the local Riot Client API (lockfile) and the endpoints the official client uses. It never locks agents, sends chat, or changes settings.
- Players in incognito/streamer mode stay **Hidden**, and their rank is never fetched.
- One cheap local check every 5s while you're in menus. Ranks are cached for 10 min, and finished matches are cached on disk at `%LOCALAPPDATA%\VLite`.

These endpoints are unofficial, so Riot can change them at any time. Tools built this way aren't known to cause bans, but Riot doesn't guarantee that.
