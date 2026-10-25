#!/usr/bin/env python3
"""
VLite - a tiny, read-only Valorant stats companion for Windows.

What it shows (the core of the Tracker.gg app):
  * Live match: every visible player's agent, level, current rank/RR, peak rank
    and act win rate, during agent select and in game.
  * Profile: your current rank/RR/peak and your last 10 matches
    (mode, map, agent, result, score, K/D/A, KD, HS%, ACS, RR change).

Why it shouldn't get you banned:
  * It never touches the VALORANT process: no memory reads, no injection,
    no graphics hooks. The in-game overlay is just a small click-through,
    always-on-top window, and its hotkey uses the standard Windows
    RegisterHotKey API (no keyboard hook).
  * It only calls the local Riot Client API and the Riot web endpoints the
    official client itself uses, with your own logged-in session.
  * It respects Riot's privacy settings: players who hid their name
    (incognito/streamer mode) stay hidden, and their rank is never looked up.
  * It polls slowly and caches everything, so request volume stays tiny.

Requires only Python 3.9+ from python.org (standard library, nothing to pip install).
"""
import base64
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import tkinter as tk
from tkinter import simpledialog, ttk

WINDOWS = os.name == "nt"
if WINDOWS:
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetParent.argtypes, user32.GetParent.restype = [wintypes.HWND], wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    shell32 = ctypes.windll.shell32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD] + \
        [ctypes.c_int] * 4 + [wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT] + [ctypes.c_int] * 3 + \
        [wintypes.HWND, ctypes.c_void_p]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.LoadIconW.argtypes, user32.LoadIconW.restype = [wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HICON
    kernel32.GetModuleHandleW.argtypes, kernel32.GetModuleHandleW.restype = [wintypes.LPCWSTR], wintypes.HMODULE
    shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
    shell32.ExtractIconW.restype = wintypes.HICON
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

APPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
LOCKFILE = os.path.join(APPDATA, "Riot Games", "Riot Client", "Config", "lockfile")
SHOOTER_LOG = os.path.join(APPDATA, "VALORANT", "Saved", "Logs", "ShooterGame.log")
CACHE_DIR = os.path.join(APPDATA, "VLite")
POLL_SECONDS = 5
RANK_TTL = 600  # re-check a player's rank at most every 10 minutes
HISTORY_COUNT = 20
FORM_MATCHES = 5  # recent comp games checked per player in your lobby (form + mains)
FORM_TTL = 1800

CLIENT_PLATFORM = base64.b64encode(json.dumps({
    "platformType": "PC", "platformOS": "Windows",
    "platformOSVersion": "10.0.19042.1.256.64bit", "platformChipset": "Unknown",
}).encode()).decode()
REGION_SHARD = {"na": "na", "latam": "na", "br": "na", "eu": "eu", "ap": "ap", "kr": "kr", "pbe": "pbe"}
QUEUE_NAMES = {
    "competitive": "Comp", "unrated": "Unrated", "swiftplay": "Swiftplay", "spikerush": "Spike Rush",
    "deathmatch": "DM", "hurm": "TDM", "ggteam": "Escalation", "premier": "Premier",
    "onefa": "Replication", "newmap": "New Map", "": "Custom",
}
# Riot ranks are numbered "tiers": 3 = Iron 1 ... 26 = Immortal 3, 27 = Radiant, 0 = unranked.
RANKS = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ascendant", "Immortal"]
TIER_NAMES = {0: "Unranked", 27: "Radiant", **{3 + 3 * i + j: f"{r} {j + 1}" for i, r in enumerate(RANKS) for j in range(3)}}
RANK_COLORS = {"Iron": "#a3a3a3", "Bronze": "#c38a5a", "Silver": "#d6dadd", "Gold": "#eac45f", "Platinum": "#52c0d1",
               "Diamond": "#c895f0", "Ascendant": "#3fcf86", "Immortal": "#ef5a72", "Radiant": "#fff1b8"}
HOTKEYS = ["F2", "F3", "F4", "F6", "F7", "F8", "F9", "F10", "Insert", "Home", "Ctrl+Shift+O", "Alt+Q"]
CORNERS = ["Top right", "Top left", "Bottom right", "Bottom left", "Top center"]
SETTINGS_FILE = os.path.join(CACHE_DIR, "settings.json")
FLAGS_FILE = os.path.join(CACHE_DIR, "flags.json")  # your private notes: puuid -> {name, note, at}
LOBBIES_FILE = os.path.join(CACHE_DIR, "lobbies.json")  # visible players from your last few matches
FLAG_COLOR = "#ffb347"
STATE_LABEL = {"menus": "In menus", "pregame": "Agent select", "ingame": "In game"}

# The Riot Client serves its local API on 127.0.0.1 with a self-signed cert, so
# certificate checks are relaxed for that one connection only. Never use a proxy for it.
_local_ctx = ssl.create_default_context()
_local_ctx.check_hostname = False
_local_ctx.verify_mode = ssl.CERT_NONE
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPSHandler(context=_local_ctx))
_WEB = urllib.request.build_opener()


class NotFound(Exception):
    pass


def request(method, url, headers=None, body=None, local=False, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with (_LOCAL if local else _WEB).open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from None
        raise
    return json.loads(raw) if raw else None


def read_log():
    try:
        with open(SHOOTER_LOG, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def version_from_log():
    m = re.search(r"CI server version: (\S+)", read_log())
    return m.group(1) if m else ""


class Assets:
    """Agent/map/rank names from valorant-api.com, cached so it works offline."""

    def __init__(self):
        self.agents, self.maps, self.tiers, self.version = {}, {}, {}, ""

    def load(self):
        path = os.path.join(CACHE_DIR, "assets.json")
        try:
            data = self._fetch()
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            try:
                with open(path) as f:
                    data = json.load(f)
            except OSError:
                data = {"agents": {}, "maps": {}, "tiers": {}, "version": ""}
        self.agents, self.maps, self.icons = data["agents"], data["maps"], data.get("icons", {})
        self.tiers = {int(k): v for k, v in data["tiers"].items()}
        self.version = data["version"]

    @staticmethod
    def _fetch():
        def get(path):
            return request("GET", "https://valorant-api.com/v1" + path, {"User-Agent": "VLite"})["data"]
        agents = get("/agents?isPlayableCharacter=true")
        maps = [m for m in get("/maps") if m.get("mapUrl")]
        tiers = get("/competitivetiers")[-1]["tiers"]
        return {
            "agents": {a["uuid"].lower(): a["displayName"] for a in agents},
            "maps": {m["mapUrl"]: m["displayName"] for m in maps},
            "tiers": {t["tier"]: [t["tierName"].title(), "#" + (t.get("color") or "ffffffff")[:6]] for t in tiers},
            "icons": {
                "agent": {a["uuid"].lower(): a.get("displayIconSmall") for a in agents},
                "tier": {str(t["tier"]): t.get("smallIcon") for t in tiers},
                "map": {m["mapUrl"]: m.get("listViewIcon") for m in maps},
            },
            "version": get("/version")["riotClientVersion"],
        }

    def download_icons(self):
        """Agent portraits, rank badges and map banners: downloaded once, then read from disk."""
        self.download([(kind, key, url) for kind, urls in self.icons.items() for key, url in urls.items()])

    def skins(self, uuids):
        """Skin names and pictures for the Store tab (valorant-api.com), cached; refetched only for new skins."""
        path = os.path.join(CACHE_DIR, "skins.json")
        known = load_json(path, {})
        if any(u.lower() not in known for u in uuids):
            try:
                data = request("GET", "https://valorant-api.com/v1/weapons/skinlevels", {"User-Agent": "VLite"})["data"]
                known = {sl["uuid"].lower(): [sl["displayName"], sl.get("displayIcon")] for sl in data}
                save_json(path, known)
            except Exception:
                pass
        info = {u.lower(): known.get(u.lower(), ["Unknown item", None]) for u in uuids}
        self.download([("skin", u, i[1]) for u, i in info.items()])
        return info

    def download(self, jobs):
        jobs = [j for j in jobs if j[2] and not os.path.exists(icon_path(j[0], j[1]))]

        def fetch(job):
            path = icon_path(job[0], job[1])
            try:
                with _WEB.open(urllib.request.Request(job[2], headers={"User-Agent": "VLite"}), timeout=15) as r:
                    data = r.read()
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
            except Exception:
                pass  # no icon is fine; the text still shows

        if jobs:
            os.makedirs(os.path.join(CACHE_DIR, "img"), exist_ok=True)
            with ThreadPoolExecutor(6) as ex:
                list(ex.map(fetch, jobs))

    def agent(self, uuid):
        return self.agents.get((uuid or "").lower(), "—") if uuid else "—"

    def map(self, url):
        return self.maps.get(url, (url or "?").rsplit("/", 1)[-1])

    def rank(self, tier):
        return TIER_NAMES.get(tier or 0) or self.tiers.get(tier, [f"Rank {tier}"])[0]

    def rank_color(self, tier):
        return RANK_COLORS.get(self.rank(tier).split()[0]) if tier else None


def icon_path(kind, key):
    return os.path.join(CACHE_DIR, "img", f"{kind}_{re.sub(r'[^A-Za-z0-9-]', '_', str(key))}.png")


class Images:
    """Icons from the disk cache, shrunk to size for Tk. Main (UI) thread only."""

    def __init__(self):
        self._memo = {}

    def get(self, kind, key, px):
        if key is None or key == "":
            return None
        key = str(key).lower() if kind == "agent" else str(key)
        if (kind, key, px) not in self._memo:
            if not os.path.exists(icon_path(kind, key)):
                return None  # not downloaded (yet): try again on the next redraw
            try:
                img = tk.PhotoImage(file=icon_path(kind, key))
                factor = max(1, round(img.width() / px))
                self._memo[kind, key, px] = img.subsample(factor) if factor > 1 else img
            except tk.TclError:
                self._memo[kind, key, px] = None
        return self._memo[kind, key, px]


def col(title, key, icon=None, field=None):
    return title, key, icon, field


ALL_COLS = [
    col("Party", "party"), col("Agent", "agent", "agent", "agent_id"), col("Player", "player"), col("Flag", "flag"),
    col("Lvl", "lvl"),
    col("Rank", "rank", "tier", "tier"), col("Peak", "peak", "tier", "peak_tier"), col("Act WR", "wr"),
    col("KD", "kd"), col("ACS", "acs"), col("HS%", "hs"), col(f"Last {FORM_MATCHES}", "last"),
    col("Mains", "mains", "agent", "main_id"), col("On this agent", "on_agent"),
    col("Played before", "met"), col("Last met", "last_met"),
]
OV_SIZES = {"Small": (8, 18), "Normal": (9, 22), "Large": (11, 28)}  # font size, icon px
PARTY_COLORS = ["#f5c542", "#5ec8f2", "#c58af9", "#ff8f5a", "#7ee08a"]


class PlayerTable(tk.Frame):
    """Player rows drawn with plain labels so every row can carry agent and rank icons.
    Used by both the Live tab and the overlay; rows are dicts built in App.show_live."""

    def __init__(self, parent, cols, images, bg, icon_px, size, row_bg=None, on_menu=None):
        super().__init__(parent, bg=bg)
        self.cols, self.images, self.bg, self.icon_px, self.size, self.row_bg = cols, images, bg, icon_px, size, row_bg
        self.on_menu = on_menu  # right-click handler: on_menu(row, event)
        self._rows = None

    def render(self, rows):
        if rows == self._rows:
            return
        self._rows = rows
        for child in self.winfo_children():
            child.destroy()
        if not rows:
            return
        for c, (title, *_rest) in enumerate(self.cols):
            tk.Label(self, text=title, bg=self.bg, fg=MUTED, font=("Segoe UI", self.size - 1), anchor="w",
                     padx=6).grid(row=0, column=c, sticky="w")
        r = 1
        for i, row in enumerate(rows):
            if i and not row["ally"] and rows[i - 1]["ally"]:  # gap between your team and the enemies
                tk.Frame(self, bg=self.bg, height=6).grid(row=r, column=0)
                r += 1
            bg = self.row_bg(row) if self.row_bg else self.bg
            font = ("Segoe UI Semibold" if row["me"] else "Segoe UI", self.size)
            for c, (_title, key, icon, field) in enumerate(self.cols):
                img = self.images.get(icon, row.get(field), self.icon_px) if icon else None
                if key == "player":
                    fg = MUTED if row["hidden"] else FLAG_COLOR if row["flag"] else OV_ALLY if row["ally"] else OV_ENEMY
                else:
                    fg = row.get(key + "_color") or FG
                cell = tk.Label(self, text=(" " if img else "") + str(row[key]), image=img or "", compound="left",
                                bg=bg, fg=fg, font=font, anchor="w", padx=6, pady=1)
                cell.grid(row=r, column=c, sticky="nsew")
                if self.on_menu:
                    cell.bind("<Button-3>", lambda e, row=row: self.on_menu(row, e))
            r += 1


class Riot:
    """Read-only client for the local Riot Client API and Riot's web endpoints."""

    def __init__(self):
        self.puuid = None
        self.region = ""
        self._ranks = {}
        self._names = {}
        self._matches = {}  # match id -> compact stats (see match())
        self._form = {}
        self._recent = {}  # puuid -> their recent comp match ids (for party detection)
        self._party = (None, [])  # (match id, your party members)
        self.rr_history = []  # your recent competitive RR updates, newest first
        self._enc = None
        self._match = None  # (match id, data) of the current in-game match

    def connect(self, version):
        with open(LOCKFILE) as f:
            _name, _pid, port, password, _proto = f.read().strip().split(":")
        self.local_base = f"https://127.0.0.1:{port}"
        self.local_auth = {"Authorization": "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()}
        self.version = version or version_from_log()
        self._refresh_tokens()
        self.region, shard = self._region()
        self.pd = f"https://pd.{shard}.a.pvp.net"
        self.glz = f"https://glz-{self.region}-1.{shard}.a.pvp.net"
        self.shared = f"https://shared.{shard}.a.pvp.net"
        self.season = self._current_season()
        self.load_cached_matches()

    def _local_get(self, path):
        return request("GET", self.local_base + path, self.local_auth, local=True)

    def _refresh_tokens(self):
        ent = self._local_get("/entitlements/v1/token")
        self.puuid = ent["subject"]
        self.headers = {
            "Authorization": "Bearer " + ent["accessToken"],
            "X-Riot-Entitlements-JWT": ent["token"],
            "X-Riot-ClientPlatform": CLIENT_PLATFORM,
            "X-Riot-ClientVersion": self.version,
        }

    def _region(self):
        m = re.search(r"https://glz-(.+?)-1\.(.+?)\.a\.pvp\.net", read_log())
        if m:
            return m.group(1), m.group(2)
        for s in (self._local_get("/product-session/v1/external-sessions") or {}).values():
            for arg in s.get("launchConfiguration", {}).get("arguments", []):
                if arg.startswith("-ares-deployment="):
                    region = arg.split("=", 1)[1]
                    return region, REGION_SHARD.get(region, region)
        raise RuntimeError("couldn't detect your region; launch VALORANT once")

    def _current_season(self):
        try:
            content = self.api("GET", f"{self.shared}/content-service/v3/content")
            return next(s["ID"] for s in content["Seasons"] if s["IsActive"] and s["Type"].lower() == "act")
        except Exception:
            return None

    def api(self, method, url, body=None):
        try:
            return request(method, url, self.headers, body)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 401, 403):  # expired token: refresh once and retry
                raise
            self._refresh_tokens()
            return request(method, url, self.headers, body)

    def _loop_state(self):
        """MENUS / PREGAME / INGAME from local presence, so menus cost no web requests."""
        try:
            for p in self._local_get("/chat/v4/presences")["presences"]:
                if p.get("puuid") == self.puuid and p.get("product") == "valorant" and p.get("private"):
                    priv = json.loads(base64.b64decode(p["private"]))
                    return (priv.get("matchPresenceData") or priv).get("sessionLoopState")
        except Exception:
            pass
        return None

    def state(self):
        loop = self._loop_state()
        if loop == "MENUS":
            self._match = None
            return "menus", None
        for kind, path in (("ingame", "core-game"), ("pregame", "pregame")):
            if loop and loop.lower() != kind:
                continue
            try:
                mid = self.api("GET", f"{self.glz}/{path}/v1/players/{self.puuid}")["MatchID"]
            except NotFound:
                continue
            if kind == "ingame" and self._match and self._match[0] == mid:
                return kind, self._match[1]  # roster can't change mid-game
            match = self.api("GET", f"{self.glz}/{path}/v1/matches/{mid}")
            if kind == "ingame":
                self._match = (mid, match)
            return kind, match
        return "menus", None

    def players(self, kind, match):
        if kind == "ingame":
            raw = match.get("Players") or []
        else:
            ally = match.get("AllyTeam") or {}
            raw = [dict(p, TeamID=ally.get("TeamID")) for p in ally.get("Players") or []]
        rows = []
        for p in raw:
            ident = p.get("PlayerIdentity") or {}
            me = p["Subject"] == self.puuid
            rows.append({
                "puuid": p["Subject"], "team": p.get("TeamID"), "agent": p.get("CharacterID"),
                "locked": p.get("CharacterSelectionState") == "locked" or kind == "ingame",
                "hidden": bool(ident.get("Incognito")) and not me, "me": me,
                "level": None if ident.get("HideAccountLevel") and not me else ident.get("AccountLevel"),
            })
        return rows

    def names(self, puuids):
        missing = [p for p in puuids if p not in self._names]
        if missing:
            for n in self.api("PUT", f"{self.pd}/name-service/v2/players", missing) or []:
                self._names[n["Subject"]] = f'{n["GameName"]}#{n["TagLine"]}' if n.get("GameName") else "?"
        return {p: self._names.get(p, "?") for p in puuids}

    def rank(self, puuid, fresh=False):
        hit = self._ranks.get(puuid)
        if hit and not fresh and time.time() - hit[0] < RANK_TTL:
            return hit[1]
        try:
            d = self.api("GET", f"{self.pd}/mmr/v1/players/{puuid}")
        except Exception:
            return None
        seasons = ((d.get("QueueSkills") or {}).get("competitive") or {}).get("SeasonalInfoBySeasonID") or {}
        cur = seasons.get(self.season) or {}
        if not cur and not self.season:  # season lookup failed: fall back to latest update
            last = d.get("LatestCompetitiveUpdate") or {}
            cur = {"CompetitiveTier": last.get("TierAfterUpdate"), "RankedRating": last.get("RankedRatingAfterUpdate")}
        peak = max((int(t) for s in seasons.values() for t in (s.get("WinsByTier") or {})), default=0)
        r = {"tier": cur.get("CompetitiveTier") or 0, "rr": cur.get("RankedRating") or 0, "peak": peak,
             "wins": cur.get("NumberOfWinsWithPlacements") or 0, "games": cur.get("NumberOfGames") or 0}
        self._ranks[puuid] = (time.time(), r)
        return r

    def history(self, count=HISTORY_COUNT, start=0):
        """Your matches as (match id, match, RR change), newest first. Every match fetched is cached
        and also feeds 'played before' and the Stats tab."""
        hist = self.api("GET", f"{self.pd}/match-history/v1/history/{self.puuid}"
                               f"?startIndex={start}&endIndex={start + count}")["History"]
        if start == 0:
            try:
                self.rr_history = self.api("GET", f"{self.pd}/mmr/v1/players/{self.puuid}/competitiveupdates"
                                                  f"?startIndex=0&endIndex=20&queue=competitive")["Matches"]
            except Exception:
                self.rr_history = []
        rr = {m["MatchID"]: m.get("RankedRatingEarned") for m in self.rr_history}
        out = []
        for h in hist:
            try:
                out.append((h["MatchID"], self.match(h["MatchID"]), rr.get(h["MatchID"])))
            except Exception:
                continue
        return out

    def my_matches(self, queue_id="competitive"):
        return [(mid, m) for mid, m in list(self._matches.items())
                if self.puuid in m["players"] and m["queue"] == queue_id]

    def store(self):
        """Your daily shop / Night Market and wallet: the same read-only calls the client's store page makes."""
        try:
            front = self.api("POST", f"{self.pd}/store/v3/storefront/{self.puuid}", {})
        except urllib.error.HTTPError:
            front = self.api("GET", f"{self.pd}/store/v2/storefront/{self.puuid}")
        wallet = self.api("GET", f"{self.pd}/store/v1/wallet/{self.puuid}").get("Balances") or {}
        return front, wallet

    def load_cached_matches(self):
        folder = os.path.join(CACHE_DIR, "matches")
        for fn in os.listdir(folder) if os.path.isdir(folder) else []:
            try:
                with open(os.path.join(folder, fn)) as f:
                    self._matches[fn[:-5]] = json.load(f)
            except (OSError, ValueError):
                pass
        self._enc = None

    def match(self, mid):
        """Compact per-player stats for a finished match. They never change, so cached on disk."""
        if mid in self._matches:
            return self._matches[mid]
        d = self.api("GET", f"{self.pd}/match-details/v1/matches/{mid}")
        info = d["matchInfo"]
        shots = {}  # puuid -> [headshots, all hits]
        for rnd in d.get("roundResults") or []:
            for ps in rnd.get("playerStats") or []:
                hs = shots.setdefault(ps.get("subject"), [0, 0])
                for dmg in ps.get("damage") or []:
                    hs[0] += dmg.get("headshots", 0)
                    hs[1] += dmg.get("headshots", 0) + dmg.get("bodyshots", 0) + dmg.get("legshots", 0)
        players = {}
        for pl in d.get("players") or []:
            st = pl.get("stats") or {}
            head, hits = shots.get(pl["subject"], [0, 0])
            players[pl["subject"]] = {
                "team": pl.get("teamId"), "agent": pl.get("characterId"),
                "k": st.get("kills", 0), "d": st.get("deaths", 0), "a": st.get("assists", 0),
                "acs": round(st.get("score", 0) / max(st.get("roundsPlayed") or 1, 1)), "head": head, "hits": hits,
            }
        m = {"start": info.get("gameStartMillis", 0), "queue": info.get("queueID") or "", "map": info.get("mapId"),
             "teams": {t["teamId"]: [bool(t.get("won")), t.get("roundsWon", 0)] for t in d.get("teams") or []},
             "players": players}
        path = os.path.join(CACHE_DIR, "matches", f"{mid}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(m, f)
        self._matches[mid] = m
        self._enc = None
        return m

    def form(self, puuid):
        """How a player has done in their last few competitive games."""
        hit = self._form.get(puuid)
        if hit and time.time() - hit[0] < FORM_TTL:
            return hit[1]
        hist = self.api("GET", f"{self.pd}/match-history/v1/history/{puuid}"
                               f"?startIndex=0&endIndex={FORM_MATCHES}&queue=competitive")["History"]
        k = d = acs = head = hits = 0
        results, games = [], []
        self._recent[puuid] = [h["MatchID"] for h in hist[:FORM_MATCHES]]
        for h in hist[:FORM_MATCHES]:
            try:
                m = self.match(h["MatchID"])
            except Exception:
                continue
            p = m["players"].get(puuid)
            if not p:
                continue
            k, d, acs = k + p["k"], d + p["d"], acs + p["acs"]
            head, hits = head + p["head"], hits + p["hits"]
            results.append(outcome(m, puuid)[0])
            games.append((p["agent"], p["k"], p["d"], results[-1]))
        f = {"kd": k / max(d, 1), "acs": round(acs / len(results)), "recent": " ".join(results), "games": games,
             "hs": round(100 * head / hits) if hits else None} if results else None
        self._form[puuid] = (time.time(), f)
        return f

    def encounters(self):
        """puuid -> {"with": [W, L], "vs": [W, L], "last": (start, map, their agent, "with"/"vs")}
        from every cached match you played in."""
        if self._enc is None:
            enc = {}
            for m in list(self._matches.values()):
                me = m["players"].get(self.puuid)
                res = outcome(m, self.puuid) if me else None
                if res not in ("Win", "Loss") or len(m["teams"]) != 2:
                    continue
                for pid, p in m["players"].items():
                    if pid != self.puuid:
                        e = enc.setdefault(pid, {"with": [0, 0], "vs": [0, 0], "last": None})
                        side = "with" if p["team"] == me["team"] else "vs"
                        e[side][res == "Loss"] += 1
                        if not e["last"] or m["start"] > e["last"][0]:
                            e["last"] = (m["start"], m["map"], p["agent"], side)
            self._enc = enc
        return self._enc

    def my_party(self, match_id):
        """Who you queued with, straight from Riot's party service (asked once per match)."""
        if self._party[0] != match_id:
            try:
                party = self.api("GET", f"{self.glz}/parties/v1/players/{self.puuid}")["CurrentPartyID"]
                members = [m["Subject"] for m in self.api("GET", f"{self.glz}/parties/v1/parties/{party}")["Members"]]
            except Exception:
                members = []
            self._party = (match_id, members)
        return self._party[1]

    def parties(self, puuids, match_id):
        """Groups of lobby players who queued together. Your own party comes from Riot; for everyone
        else it's inferred: 2+ of their recent games on the same team means a premade."""
        lobby = set(puuids)
        parent = {p: p for p in puuids}

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x

        for member in self.my_party(match_id):
            if member in lobby:
                parent[find(member)] = find(self.puuid)
        together = {}
        for pid in puuids:
            if pid == self.puuid:
                continue  # your real party is already known; don't guess from your history
            for mid in self._recent.get(pid, []):
                m = self._matches.get(mid)
                if not m or pid not in m["players"]:
                    continue
                for other, op in m["players"].items():
                    if other in lobby and other not in (pid, self.puuid) and op["team"] == m["players"][pid]["team"]:
                        pair = tuple(sorted((pid, other)))
                        together.setdefault(pair, set()).add(mid)
        for (a, b), mids in together.items():
            if len(mids) >= 2:
                parent[find(a)] = find(b)
        groups = {}
        for pid in puuids:
            groups.setdefault(find(pid), []).append(pid)
        return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: self.puuid not in g)

    def cached_form(self, puuid):
        """(known, form) without any network call; known=False means it still needs fetching."""
        hit = self._form.get(puuid)
        return (bool(hit) and time.time() - hit[0] < FORM_TTL), (hit[1] if hit else None)


def outcome(m, puuid):
    team = m["teams"].get(m["players"][puuid]["team"])
    if not team:
        return "—"
    if team[0]:
        return "Win"
    others = [t[1] for k, t in m["teams"].items() if k != m["players"][puuid]["team"]]
    return "Draw" if len(m["teams"]) == 2 and others[0] == team[1] else "Loss"


def score(m, puuid):
    mine = m["players"][puuid]["team"]
    if len(m["teams"]) != 2 or mine not in m["teams"]:
        return ""
    other = next(t for k, t in m["teams"].items() if k != mine)
    return f'{m["teams"][mine][1]}-{other[1]}'


def record(wl):
    return f"{wl[0]}W {wl[1]}L"


def ago(ms):
    days = (time.time() - ms / 1000) / 86400
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(days)}d ago"
    return f"{int(days // 7)}w ago" if days < 60 else f"{int(days // 30)}mo ago"


def load_settings():
    settings = {"hotkey": "F6", "corner": "Top right", "ov_size": "Normal", "ov_opacity": 88,
                "ov_cols": ["party", "agent", "player", "rank", "kd", "acs", "last", "met"],
                "auto_show_pregame": True, "auto_hide_ingame": False, "popup_flagged": True, "notify": True,
                "tray": True, "startup": False, "ov_session": True}
    try:
        with open(SETTINGS_FILE) as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        pass
    for new_col in ("flag", "mains"):  # columns added in later versions: switch each on once
        if not settings.get(new_col + "_col"):
            settings["ov_cols"] = settings["ov_cols"] + [new_col]
            settings[new_col + "_col"] = True
    return settings


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def save_settings(settings):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)


def parse_hotkey(combo):
    mods, vk = 0, 0
    for part in combo.split("+"):
        part = part.strip().lower()
        if part in ("ctrl", "shift", "alt"):
            mods |= {"alt": 1, "ctrl": 2, "shift": 4}[part]
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])
        else:
            vk = {"insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22}.get(part, ord(part.upper()[0]))
    return mods, vk


class Hotkey:
    """System-wide hotkey via RegisterHotKey: Windows tells us when it's pressed; no keyboard hook."""

    def __init__(self, callback):
        self.callback = callback
        self.tid = None
        self.thread = None

    def set(self, combo):
        self.stop()
        if not WINDOWS:
            return True
        ready, result = threading.Event(), {}
        self.thread = threading.Thread(target=self._run, args=(*parse_hotkey(combo), ready, result), daemon=True)
        self.thread.start()
        ready.wait(2)
        return result.get("ok", False)

    def _run(self, mods, vk, ready, result):
        self.tid = kernel32.GetCurrentThreadId()
        result["ok"] = bool(user32.RegisterHotKey(None, 1, mods | 0x4000, vk))  # 0x4000: MOD_NOREPEAT
        ready.set()
        if not result["ok"]:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.callback()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT ends that thread's loop
            self.thread.join(1)  # let it unregister before the next key is registered
            self.tid = None


class Tray:
    """Notification-area icon with a small menu. Also how VLite shows Windows notifications.
    Runs its own hidden window and message loop on a background thread."""

    WM_TRAY = 0x8001  # WM_APP + 1

    def __init__(self, on_open, on_toggle, on_quit):
        self.cmds = {1: on_open, 2: on_toggle, 3: on_quit}
        self.hwnd = None
        if WINDOWS:
            threading.Thread(target=self._run, daemon=True).start()

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        return nid

    def _run(self):
        def proc(hwnd, msg, wp, lp):
            if msg == self.WM_TRAY:
                event = lp & 0xFFFF
                if event in (0x0202, 0x0203):  # left click / double-click
                    self.cmds[1]()
                elif event == 0x0205:  # right click
                    self._menu(hwnd)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        self._proc = WNDPROC(proc)  # keep a reference: Windows calls it for as long as the window lives
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "VLiteTray"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "VLiteTray", "VLite", 0, 0, 0, 0, 0, None, None, hinst, None)
        nid = self._nid(0x1 | 0x2 | 0x4)  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = shell32.ExtractIconW(hinst, sys.executable, 0) or user32.LoadIconW(None, 32512)
        nid.szTip = "VLite"
        shell32.Shell_NotifyIconW(0, ctypes.byref(nid))  # NIM_ADD
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _menu(self, hwnd):
        menu = user32.CreatePopupMenu()
        for cid, text in ((1, "Open VLite"), (2, "Show / hide overlay"), (3, "Quit VLite")):
            user32.AppendMenuW(menu, 0, cid, text)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)  # so the menu closes when you click elsewhere
        cid = user32.TrackPopupMenu(menu, 0x0100 | 0x0080, pt.x, pt.y, 0, hwnd, None)  # RETURNCMD | NONOTIFY
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        if cid in self.cmds:
            self.cmds[cid]()

    def notify(self, title, text):
        if not self.hwnd:
            return
        nid = self._nid(0x10)  # NIF_INFO
        nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = title[:63], text[:255], 0x1  # NIIF_INFO
        shell32.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY

    def remove(self):
        if self.hwnd:
            shell32.Shell_NotifyIconW(2, ctypes.byref(self._nid(0)))  # NIM_DELETE


def set_startup(on):
    """Add/remove VLite from 'start with Windows' (current user only, no admin needed)."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                        winreg.KEY_SET_VALUE) as key:
        if not on:
            try:
                winreg.DeleteValue(key, "VLite")
            except FileNotFoundError:
                pass
        elif getattr(sys, "frozen", False):  # the built VLite.exe
            winreg.SetValueEx(key, "VLite", 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            winreg.SetValueEx(key, "VLite", 0, winreg.REG_SZ, f'"{pyw}" "{os.path.abspath(__file__)}"')


OV_BG, OV_ALLY, OV_ENEMY = "#0b0f14", "#7dd3a8", "#f4978e"


class Overlay:
    """Compact player panel above the game. A plain window that is click-through, always on top,
    and never takes focus, so the game keeps your mouse and keyboard. Needs Windowed Fullscreen."""

    def __init__(self, root, settings, images):
        self.settings, self.images = settings, images
        self.table = None
        self.visible = False
        self._last = None
        w = self.win = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=OV_BG)
        self.head = tk.Label(w, bg=OV_BG, fg=FG, anchor="w", padx=10)
        self.head.pack(fill="x", pady=(8, 2))
        self.apply()
        w.update_idletasks()
        if WINDOWS:
            self.hwnd = user32.GetParent(w.winfo_id())
            ex = user32.GetWindowLongW(self.hwnd, -20)  # GWL_EXSTYLE
            # layered | click-through | no taskbar button | never activated
            user32.SetWindowLongW(self.hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
        self.show(False)

    def apply(self, rebuild=True):
        """Pick up size / opacity / column changes from settings."""
        # keep below 1.0: Tk drops the layered (click-through) style at exactly 1.0
        self.win.attributes("-alpha", min(self.settings["ov_opacity"], 97) / 100)
        if not rebuild:
            return
        font, icon = OV_SIZES.get(self.settings["ov_size"], OV_SIZES["Normal"])
        self.head.config(font=("Segoe UI Semibold", font + 1))
        if self.table:
            self.table.destroy()
        cols = [c for c in ALL_COLS if c[1] in self.settings["ov_cols"]]
        self.table = PlayerTable(self.win, cols, self.images, OV_BG, icon_px=icon, size=font)
        self.table.pack(padx=6, pady=(0, 8))
        if self._last:
            self.table.render(self._last[1])
        if self.visible:
            self.place()

    def toggle(self):
        self.show(not self.visible)

    def show(self, on):
        self.visible = on
        if WINDOWS:
            user32.ShowWindow(self.hwnd, 4 if on else 0)  # SW_SHOWNOACTIVATE / SW_HIDE
        else:
            (self.win.deiconify if on else self.win.withdraw)()
        if on:
            self.place()

    def place(self):
        w = self.win
        w.update_idletasks()
        ww, wh, sw, sh = w.winfo_reqwidth(), w.winfo_reqheight(), w.winfo_screenwidth(), w.winfo_screenheight()
        corner = self.settings["corner"]
        x = {"left": 20, "right": sw - ww - 20, "center": (sw - ww) // 2}[corner.split()[-1].lower()]
        y = 20 if corner.startswith("Top") else sh - wh - 60
        if WINDOWS:  # HWND_TOPMOST, SWP_NOSIZE | SWP_NOACTIVATE
            user32.SetWindowPos(self.hwnd, wintypes.HWND(-1), x, y, 0, 0, 0x0001 | 0x0010)
        else:
            w.geometry(f"+{x}+{y}")
            w.lift()

    def render(self, header, rows):
        if (header, rows) == self._last:
            return
        self._last = (header, rows)
        self.head.config(text=header)
        self.table.render(rows)
        if self.visible:
            self.place()


BG, PANEL, FG, MUTED = "#0f1419", "#171e26", "#e6e9ed", "#8b96a3"
GRID, CHART = "#243040", "#5ec8f2"  # chart gridlines, rank-graph line
ALLY_BG, ENEMY_BG, WIN, LOSS = "#14251e", "#2b1719", "#4ade80", "#f87171"


def make_tree(parent, cols, icon_col=None):
    tree = ttk.Treeview(parent, columns=[c for c, _ in cols], selectmode="none",
                        show="tree headings" if icon_col else "headings")
    if icon_col:  # Treeview can only put an image in its first (#0) column
        tree.heading("#0", text=icon_col[0], anchor="w")
        tree.column("#0", width=icon_col[1], stretch=False)
    for c, w in cols:
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="w" if w >= 120 else "center", stretch=w >= 120)
    tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    return tree


class App:
    def __init__(self, root):
        self.root = root
        self.assets = Assets()
        self.riot = Riot()
        self.q = queue.Queue()
        self.wake = threading.Event()
        self.reload_profile = True
        self.kind = "menus"
        self.me_name = ""
        self.hotkey_note = ""
        self.form_pool = ThreadPoolExecutor(2)  # recent-form lookups run behind the scenes
        self.form_pending = set()
        self.settings = load_settings()
        self.images = Images()
        self._build()
        self.overlay = Overlay(root, self.settings, self.images)
        self.dlg = self.flag_dlg = None
        self.flags = load_json(FLAGS_FILE, {})
        self.lobbies = load_json(LOBBIES_FILE, [])
        self._live_args = None
        self._flag_seen = (None, set())
        self.session_short = ""
        self.hist_loaded = 0  # how many of your matches have been paged in (Stats tab "load more")
        self.want_more = self.want_store = False
        self.tray = Tray(lambda: self.post(self.open_window), lambda: self.post(self.overlay.toggle),
                         lambda: self.post(self.quit))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.hotkey = Hotkey(lambda: self.post(self.overlay.toggle))
        self.set_hotkey()
        self.show_live(None, None, [], {}, {}, {}, {})
        threading.Thread(target=self.worker, daemon=True).start()
        root.after(200, self.drain)

    # ---------- UI ----------
    def _build(self):
        r = self.root
        r.title("VLite")
        r.geometry("1400x720")
        r.minsize(700, 360)
        r.configure(bg=BG)
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=30, borderwidth=0)
        s.configure("Treeview.Heading", background=BG, foreground=MUTED, relief="flat")
        s.map("Treeview.Heading", background=[("active", PANEL)])
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(16, 6))
        s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", FG)])
        s.configure("TButton", background=PANEL, foreground=FG, borderwidth=0)
        s.map("TButton", background=[("active", "#243040")])
        s.configure("TCheckbutton", background=BG, foreground=MUTED)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("Head.TLabel", font=("Segoe UI Semibold", 12))
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        r.option_add("*TCombobox*Listbox.background", PANEL)
        r.option_add("*TCombobox*Listbox.foreground", FG)
        s.configure("Muted.TLabel", foreground=MUTED)

        top = ttk.Frame(r)
        top.pack(fill="x", padx=10, pady=8)
        self.status = ttk.Label(top, text="Starting…", style="Muted.TLabel")
        self.status.pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Button(top, text="Show / hide overlay", command=lambda: self.overlay.toggle()).pack(side="right", padx=6)
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="⚑ Flags", command=self.open_flags).pack(side="right", padx=6)

        self.tabs = ttk.Notebook(r)
        self.tabs.pack(fill="both", expand=True)
        live, prof, stats, store = (ttk.Frame(self.tabs) for _ in range(4))
        self.tabs.add(live, text="Live match")
        self.tabs.add(prof, text="My profile")
        self.tabs.add(stats, text="Stats")
        self.tabs.add(store, text="Store")

        self.live_head = ttk.Label(live, style="Head.TLabel", compound="left")
        self.live_head.pack(anchor="w", padx=10, pady=8)
        self.live_table = PlayerTable(live, ALL_COLS, self.images, BG, icon_px=28, size=10,
            row_bg=lambda row: ALLY_BG if row["ally"] else ENEMY_BG, on_menu=self.row_menu)
        self.live_table.pack(anchor="nw", padx=10, pady=(0, 4))
        ttk.Label(live, text="Right-click a player to flag them with a note. Past lobbies are under ⚑ Flags.",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(0, 8))

        self.prof_head = ttk.Label(prof, style="Head.TLabel", compound="left")
        self.prof_head.pack(anchor="w", padx=10, pady=(8, 2))
        self.session_lbl = ttk.Label(prof, style="Muted.TLabel")
        self.session_lbl.pack(anchor="w", padx=12, pady=(0, 6))
        ttk.Label(prof, text="Rank progress (last 20 competitive games)", style="Muted.TLabel").pack(anchor="w", padx=12)
        self.rr_canvas = tk.Canvas(prof, height=150, bg=BG, highlightthickness=0)
        self.rr_canvas.pack(fill="x", padx=10, pady=(0, 8))
        self.rr_canvas.bind("<Configure>", lambda _e: self.draw_rr())
        self.rr_canvas.bind("<Motion>", self.rr_hover)
        self.rr_canvas.bind("<Leave>", lambda _e: self.rr_canvas.delete("tip"))
        self.hist = make_tree(prof, [("Date", 110), ("Mode", 80), ("Map", 80), ("Result", 60),
                                     ("Score", 60), ("K / D / A", 90), ("KD", 50), ("HS%", 50), ("ACS", 50),
                                     ("RR", 50)], icon_col=("Agent", 130))
        self.hist.tag_configure("Win", foreground=WIN)
        self.hist.tag_configure("Loss", foreground=LOSS)
        self.hist.bind("<Double-1>", lambda e: self.open_scoreboard(self.hist.identify_row(e.y)))
        ttk.Label(prof, text="Double-click a match to see its scoreboard.", style="Muted.TLabel") \
            .pack(anchor="w", padx=12, pady=(0, 8))

        bar = ttk.Frame(stats)
        bar.pack(fill="x", padx=10, pady=8)
        self.stats_head = ttk.Label(bar, style="Head.TLabel")
        self.stats_head.pack(side="left")
        ttk.Button(bar, text="Load 20 older matches", command=self.load_more).pack(side="right")
        panes = ttk.Frame(stats)
        panes.pack(fill="both", expand=True)
        stat_cols = [("Games", 70), ("Win %", 70), ("KD", 60), ("ACS", 60), ("HS%", 60)]
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        left.pack(side="left", fill="both", expand=True)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="By map", style="Muted.TLabel").pack(anchor="w", padx=12)
        self.map_stats = make_tree(left, stat_cols, icon_col=("Map", 150))
        ttk.Label(right, text="By agent", style="Muted.TLabel").pack(anchor="w", padx=12)
        self.agent_stats = make_tree(right, stat_cols, icon_col=("Agent", 150))

        bar = ttk.Frame(store)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="Load my store", command=self.load_store).pack(side="left")
        self.wallet_lbl = ttk.Label(bar, style="Muted.TLabel")
        self.wallet_lbl.pack(side="left", padx=14)
        self.store_body = ttk.Frame(store)
        self.store_body.pack(fill="both", expand=True, padx=10)

    def set_hotkey(self, combo=None):
        combo = combo or self.settings["hotkey"]
        if self.hotkey.set(combo):
            self.settings["hotkey"] = combo
            save_settings(self.settings)
            self.hotkey_note = f"Overlay: {combo}"
        else:
            self.hotkey_note = f"⚠ {combo} is used by another app, pick another overlay hotkey"
        self.set_status(self.hotkey_note)

    def update_overlay(self, **changes):
        self.settings.update(changes)
        save_settings(self.settings)
        self.overlay.apply(rebuild=set(changes) != {"ov_opacity"})

    def open_settings(self):
        if self.dlg and self.dlg.winfo_exists():
            self.dlg.lift()
            return
        d = self.dlg = tk.Toplevel(self.root)
        d.title("Overlay settings")
        d.configure(bg=BG)
        d.resizable(False, False)
        d.transient(self.root)
        f = ttk.Frame(d, padding=16)
        f.pack(fill="both")

        def row(r, label, widget):
            ttk.Label(f, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="nw", pady=5, padx=(0, 14))
            widget.grid(row=r, column=1, sticky="w", pady=5)

        def combo(values, current, on_pick):
            box = ttk.Combobox(f, values=values, width=16, state="readonly")
            box.set(current)
            box.bind("<<ComboboxSelected>>", lambda _e: on_pick(box.get()))
            return box

        row(0, "Show / hide hotkey", combo(HOTKEYS, self.settings["hotkey"], self.set_hotkey))
        row(1, "Position", combo(CORNERS, self.settings["corner"], lambda v: self.update_overlay(corner=v)))
        row(2, "Size", combo(list(OV_SIZES), self.settings["ov_size"], lambda v: self.update_overlay(ov_size=v)))
        opacity = tk.Scale(f, from_=40, to=97, orient="horizontal", length=200, bg=BG, fg=FG, troughcolor=PANEL,
                           highlightthickness=0, bd=0, activebackground=MUTED)
        opacity.set(self.settings["ov_opacity"])
        opacity.config(command=lambda v: self.update_overlay(ov_opacity=int(float(v))))
        row(3, "Opacity %", opacity)
        box = ttk.Frame(f)
        col_vars = {}

        def pick_cols():
            self.update_overlay(ov_cols=[k for k, v in col_vars.items() if v.get()] or ["player"])

        for i, (title, key, *_rest) in enumerate(ALL_COLS):
            col_vars[key] = tk.BooleanVar(value=key in self.settings["ov_cols"])
            ttk.Checkbutton(box, text=title, variable=col_vars[key], command=pick_cols) \
                .grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 14), pady=1)
        row(4, "Columns", box)
        auto = ttk.Frame(f)
        for i, (key, text) in enumerate([
                ("ov_session", "Show session stats in the overlay title"),
                ("auto_show_pregame", "Show the overlay automatically in agent select"),
                ("auto_hide_ingame", "Hide it automatically when the match starts"),
                ("popup_flagged", "Show the overlay when a flagged player is in your lobby"),
                ("notify", "Windows notifications (match found, flagged player in lobby)"),
                ("tray", "Closing the window keeps VLite running in the tray"),
                ("startup", "Start VLite with Windows")]):
            var = tk.BooleanVar(value=self.settings[key])
            ttk.Checkbutton(auto, text=text, variable=var,
                            command=lambda k=key, v=var: self.set_option(k, v.get())).grid(row=i, column=0, sticky="w", pady=1)
        row(5, "Automation", auto)
        ttk.Label(f, text="Changes apply instantly. The overlay stays shown while you adjust it.",
                  style="Muted.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.overlay.show(True)

    def set_option(self, key, value):
        if key == "startup" and WINDOWS:
            try:
                set_startup(value)
            except OSError as e:
                self.set_status(f"Couldn't change 'start with Windows': {e}")
                return
        self.settings[key] = value
        save_settings(self.settings)
        if key == "ov_session" and self._live_args:
            self.show_live(*self._live_args)

    # ---------- window, tray, notifications ----------
    def notify(self, title, text):
        if self.settings["notify"]:
            self.tray.notify(title, text)

    def open_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.settings["tray"] and self.tray.hwnd:
            self.root.withdraw()
            if not self.settings.get("tray_hint_shown"):
                self.tray.notify("VLite is still running", "It's in the tray. Right-click the icon to quit.")
                self.set_option("tray_hint_shown", True)
        else:
            self.quit()

    def quit(self):
        self.tray.remove()
        self.hotkey.stop()
        self.root.destroy()

    def on_match_found(self, kind, map_name):
        self.notify("Match found", f"{map_name} · {STATE_LABEL[kind]}")
        if kind == "pregame" and self.settings["auto_show_pregame"]:
            self.overlay.show(True)

    def refresh(self):
        self.reload_profile = True
        self.wake.set()

    def post(self, fn, *args):
        self.q.put((fn, args))

    def drain(self):
        while not self.q.empty():
            fn, args = self.q.get()
            fn(*args)
        self.root.after(200, self.drain)

    def set_status(self, text):
        self.status.config(text=text)

    def show_live(self, kind, match, rows, names, ranks, forms, enc, parties=()):
        self._live_args = (kind, match, rows, names, ranks, forms, enc, parties)
        hide = f'{self.settings["hotkey"]} to hide'
        if not kind:
            self.live_head.config(text="Not in a match — players show up here in agent select and in game.", image="")
            self.live_table.render([])
            self.overlay.render(f"Not in a match · {hide}", [])
            return
        a = self.assets
        queue_id = (match.get("MatchmakingData") or {}).get("QueueID") or match.get("QueueID") or ""
        head = f'{STATE_LABEL[kind]} · {a.map(match.get("MapID"))} · {QUEUE_NAMES.get(queue_id, queue_id.title())}'
        banner = self.images.get("map", match.get("MapID"), 180)
        self.live_head.config(text=("   " if banner else "") + head, image=banner or "")
        my_team = next((p["team"] for p in rows if p["me"]), None)
        party_of = {pid: PARTY_COLORS[i % len(PARTY_COLORS)] for i, group in enumerate(parties) for pid in group}
        table = []
        for p in sorted(rows, key=lambda p: (p["team"] != my_team, not p["me"])):
            rk = ranks.get(p["puuid"]) or {}
            known, f = forms.get(p["puuid"], (True, None))
            form = ((f'{f["kd"]:.2f}', f["acs"], "—" if f["hs"] is None else f'{f["hs"]}%', f["recent"]) if f
                    else ("—",) * 4 if known else ("…",) * 4)
            games = f["games"] if f else []
            mains = Counter(g[0] for g in games).most_common(2)
            on = [g for g in games if g[0] == p["agent"]]
            e = None if p["hidden"] else enc.get(p["puuid"])  # incognito players stay fully anonymous
            flag = None if p["hidden"] or p["me"] else self.flags.get(p["puuid"])
            tier, peak = rk.get("tier"), rk.get("peak")
            table.append({
                "puuid": p["puuid"], "ally": p["team"] == my_team, "me": p["me"], "hidden": p["hidden"],
                "flag": f'⚑ {flag["note"]}' if flag else "", "flag_color": FLAG_COLOR,
                "agent_id": p["agent"], "agent": a.agent(p["agent"]) + ("" if p["locked"] else " (hovering)"),
                "player": "Hidden (incognito)" if p["hidden"] else names.get(p["puuid"], "?"),
                "lvl": p["level"] if p["level"] is not None else "—",
                "tier": tier, "rank": (a.rank(tier) + (f' · {rk["rr"]}' if tier else "")) if rk else "—",
                "rank_color": a.rank_color(tier),
                "peak_tier": peak, "peak": a.rank(peak) if rk else "—", "peak_color": a.rank_color(peak),
                "wr": f'{round(100 * rk["wins"] / rk["games"])}% ({rk["games"]})' if rk.get("games") else "—",
                "kd": form[0], "acs": form[1], "hs": form[2], "last": form[3],
                "met": " · ".join(f"{k.title()} {record(e[k])}" for k in ("with", "vs") if e and sum(e[k])) or "—",
                "last_met": (f'{ago(e["last"][0])} · {a.map(e["last"][1])} · {a.agent(e["last"][2])} ({e["last"][3]})'
                             if e and e.get("last") else "—"),
                "party": "●" if p["puuid"] in party_of else "", "party_color": party_of.get(p["puuid"]),
                "main_id": mains[0][0] if mains else None,
                "mains": " · ".join(f"{a.agent(ag)} {n}" for ag, n in mains) or form[0],
                "on_agent": (f'{len(on)}/{len(games)} games · {sum(g[1] for g in on) / max(sum(g[2] for g in on), 1):.2f} KD'
                             f' · {sum(g[3] == "W" for g in on)}W' if on else "new pick" if games else form[0]),
            })
        self.live_table.render(table)
        session = f" · {self.session_short}" if self.settings["ov_session"] and self.session_short else ""
        self.overlay.render(f"{head}{session} · {hide}", table)
        self.alert_flagged(match.get("ID"), table)
        self.remember_lobby(match.get("ID"), a.map(match.get("MapID")), [
            {"puuid": r["puuid"], "name": r["player"], "agent_id": r["agent_id"], "ally": r["ally"]}
            for r in table if not r["hidden"] and not r["me"]])

    # ---------- flags: private notes on players whose names you could see ----------
    def remember_lobby(self, match_id, map_name, players):
        """Keep the visible players of your last 3 matches so you can flag them after the game."""
        if not match_id:
            return
        if self.lobbies and self.lobbies[0]["match"] == match_id:
            lobby = self.lobbies[0]
        else:
            lobby = {"match": match_id, "map": map_name, "at": int(time.time()), "players": []}
            self.lobbies = [lobby] + self.lobbies[:2]
        known = {p["puuid"]: p for p in lobby["players"]}
        merged = dict(known, **{p["puuid"]: p for p in players})
        if merged != known or not os.path.exists(LOBBIES_FILE):
            lobby["players"] = list(merged.values())
            save_json(LOBBIES_FILE, self.lobbies)
            self.fill_flags()

    def alert_flagged(self, match_id, table):
        """Tell you once per match when someone you flagged is in the lobby."""
        if self._flag_seen[0] != match_id:
            self._flag_seen = (match_id, set())
        new = [r for r in table if r["flag"] and r["puuid"] not in self._flag_seen[1]]
        if not new:
            return
        self._flag_seen[1].update(r["puuid"] for r in new)
        self.notify("Flagged player in your lobby", "\n".join(f'{r["player"]}: {r["flag"][2:]}' for r in new))
        if self.settings["popup_flagged"]:
            self.overlay.show(True)

    def row_menu(self, row, event):
        if row["hidden"] or row["me"]:
            return  # hidden players can't be flagged: that would identify them later
        menu = tk.Menu(self.root, tearoff=0)
        flagged = row["puuid"] in self.flags
        menu.add_command(label="Edit flag note…" if flagged else "⚑ Flag player…",
                         command=lambda: self.edit_flag(row["puuid"], row["player"]))
        if flagged:
            menu.add_command(label="Remove flag", command=lambda: self.unflag(row["puuid"]))
        menu.tk_popup(event.x_root, event.y_root)

    def edit_flag(self, puuid, name):
        note = simpledialog.askstring("Flag player", f"Note for {name} (e.g. threw, AFK, toxic, great duo):",
                                      initialvalue=self.flags.get(puuid, {}).get("note", ""),
                                      parent=self.flag_dlg if self.flag_dlg and self.flag_dlg.winfo_exists() else self.root)
        if note is None:
            return
        self.flags[puuid] = {"name": name, "note": note.strip() or "flagged", "at": int(time.time())}
        self.flags_changed()

    def unflag(self, puuid):
        self.flags.pop(puuid, None)
        self.flags_changed()

    def flags_changed(self):
        save_json(FLAGS_FILE, self.flags)
        self._flag_seen[1].update(self.flags)  # flagging someone in this lobby isn't news to you
        if self._live_args:
            self.show_live(*self._live_args)
        self.fill_flags()

    def open_flags(self):
        if self.flag_dlg and self.flag_dlg.winfo_exists():
            self.flag_dlg.lift()
            return
        d = self.flag_dlg = tk.Toplevel(self.root)
        d.title("Flagged players")
        d.configure(bg=BG)
        d.transient(self.root)
        self.flag_body = ttk.Frame(d, padding=16)
        self.flag_body.pack(fill="both", expand=True)
        self.fill_flags()

    def fill_flags(self):
        if not (self.flag_dlg and self.flag_dlg.winfo_exists()):
            return
        f = self.flag_body
        for child in f.winfo_children():
            child.destroy()

        def player_row(r, puuid, name, agent_id=None, ally=None):
            flag = self.flags.get(puuid)
            img = self.images.get("agent", agent_id, 22)
            tk.Label(f, image=img or "", text=(" " if img else "") + name, compound="left", bg=BG, anchor="w",
                     fg=FLAG_COLOR if flag else OV_ALLY if ally else OV_ENEMY if ally is False else FG,
                     font=("Segoe UI", 10)).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Label(f, text="" if ally is None else "Teammate" if ally else "Enemy",
                      style="Muted.TLabel").grid(row=r, column=1, sticky="w", padx=10)
            ttk.Label(f, text=f'⚑ {flag["note"]}' if flag else "", foreground=FLAG_COLOR).grid(row=r, column=2, sticky="w", padx=10)
            ttk.Button(f, text="Edit" if flag else "Flag", width=7,
                       command=lambda: self.edit_flag(puuid, name)).grid(row=r, column=3, padx=2)
            if flag:
                ttk.Button(f, text="Remove", width=7, command=lambda: self.unflag(puuid)).grid(row=r, column=4, padx=2)

        r = 0
        shown = set()
        for lobby in self.lobbies:
            ttk.Label(f, text=f'{lobby["map"]} · {ago(lobby["at"] * 1000)}', style="Head.TLabel") \
                .grid(row=r, column=0, columnspan=5, sticky="w", pady=(10 if r else 0, 4))
            r += 1
            for p in sorted(lobby["players"], key=lambda p: not p["ally"]):
                player_row(r, p["puuid"], p["name"], p["agent_id"], p["ally"])
                shown.add(p["puuid"])
                r += 1
        others = [(pid, fl) for pid, fl in self.flags.items() if pid not in shown]
        if others:
            ttk.Label(f, text="Other flagged players", style="Head.TLabel") \
                .grid(row=r, column=0, columnspan=5, sticky="w", pady=(10 if r else 0, 4))
            r += 1
            for pid, fl in sorted(others, key=lambda x: -x[1]["at"]):
                player_row(r, pid, fl["name"])
                r += 1
        if not r:
            ttk.Label(f, text="Nobody here yet. After a match, the players whose names you could see\n"
                             "show up here so you can flag them.", style="Muted.TLabel").grid(row=0, column=0)
        ttk.Label(f, text="Hidden (incognito) players are never listed or flagged. Notes stay on this PC.",
                  style="Muted.TLabel").grid(row=r + 1, column=0, columnspan=5, sticky="w", pady=(14, 0))

    def show_profile(self, rk, hist):
        a = self.assets
        if rk:
            wr = f' · Act: {rk["wins"]}W / {rk["games"]}G' if rk["games"] else ""
            badge = self.images.get("tier", rk["tier"], 44)
            self.prof_head.config(image=badge or "", text=f'{"  " if badge else ""}{self.me_name} · {a.rank(rk["tier"])}'
                                                          f'{" " + str(rk["rr"]) + " RR" if rk["tier"] else ""}'
                                                          f' · Peak {a.rank(rk["peak"])}{wr}')
        self.show_session(hist)
        self.draw_rr()
        self.hist.delete(*self.hist.get_children())
        me = self.riot.puuid
        for mid, m, rr in hist:
            p = m["players"].get(me)
            if not p:
                continue
            result = outcome(m, me)
            icon = self.images.get("agent", p["agent"], 24)
            self.hist.insert("", "end", iid=mid, tags=(result,), text=" " + a.agent(p["agent"]),
                             **({"image": icon} if icon else {}), values=(
                datetime.fromtimestamp(m["start"] / 1000).strftime("%b %d %H:%M"),
                QUEUE_NAMES.get(m["queue"], m["queue"].title()), a.map(m["map"]),
                result, score(m, me), f'{p["k"]} / {p["d"]} / {p["a"]}', f'{p["k"] / max(p["d"], 1):.2f}',
                f'{round(100 * p["head"] / p["hits"])}%' if p["hits"] else "—", p["acs"],
                "" if rr is None else f"{rr:+d}"))
        self.show_stats()

    def show_session(self, hist):
        """Session = your recent games with less than 3 hours between them."""
        me, games, last = self.riot.puuid, [], time.time() * 1000
        for _mid, m, rr in hist:
            if last - m["start"] > 3 * 3600 * 1000:
                break
            if me in m["players"]:
                games.append((outcome(m, me), rr, m["players"][me]))
            last = m["start"]
        if not games:
            self.session_short = ""
            self.session_lbl.config(text="Session: no games in the last 3 hours")
            return
        wins, losses = sum(g[0] == "Win" for g in games), sum(g[0] == "Loss" for g in games)
        rr = sum(g[1] for g in games if g[1] is not None)
        streak = next((i for i, g in enumerate(games) if g[0] != games[0][0]), len(games))
        kd = sum(g[2]["k"] for g in games) / max(sum(g[2]["d"] for g in games), 1)
        self.session_short = f"Session {wins}W {losses}L {rr:+d} RR"
        self.session_lbl.config(text=f"Session: {wins}W {losses}L · {rr:+d} RR · "
                                     f"{games[0][0][0]}{streak} streak · KD {kd:.2f} · {len(games)} games")

    def draw_rr(self):
        """Line chart of your RR over the last 20 competitive games (oldest left)."""
        c = self.rr_canvas
        c.delete("all")
        pts = [u for u in reversed(self.riot.rr_history) if u.get("TierAfterUpdate")]  # placements have no tier
        w, h = c.winfo_width(), int(c["height"])
        if len(pts) < 2 or w < 100:
            c.create_text(12, h // 2, anchor="w", fill=MUTED, font=("Segoe UI", 9),
                          text="Play a couple of competitive games to see your rank graph.")
            self._rr_pts = []
            return
        vals = [u["TierAfterUpdate"] * 100 + u.get("RankedRatingAfterUpdate", 0) for u in pts]
        lo, hi = min(vals) // 100 * 100, (max(vals) // 100 + 1) * 100
        left, right, top, bottom = 110, w - 20, 10, h - 14
        x = lambda i: left + (right - left) * i / (len(vals) - 1)
        y = lambda v: bottom - (bottom - top) * (v - lo) / (hi - lo)
        for tier_v in range(lo, hi + 1, 100):  # rank boundaries: recessive grid + rank names
            c.create_line(left, y(tier_v), right, y(tier_v), fill=GRID)
            if tier_v < hi:
                c.create_text(left - 8, y(tier_v + 50), anchor="e", fill=MUTED, font=("Segoe UI", 9),
                              text=self.assets.rank(tier_v // 100))
        c.create_line(*[v for i, val in enumerate(vals) for v in (x(i), y(val))], fill=CHART, width=2)
        self._rr_pts = []
        for i, (u, val) in enumerate(zip(pts, vals)):
            px, py = x(i), y(val)
            c.create_oval(px - 4, py - 4, px + 4, py + 4, fill=CHART, outline=BG, width=2)
            self._rr_pts.append((px, py, u))

    def rr_hover(self, event):
        c = self.rr_canvas
        c.delete("tip")
        if not getattr(self, "_rr_pts", None):
            return
        px, py, u = min(self._rr_pts, key=lambda p: abs(p[0] - event.x))
        if abs(px - event.x) > 30:
            return
        when = datetime.fromtimestamp(u.get("MatchStartTime", 0) / 1000).strftime("%b %d")
        text = (f'{when} · {self.assets.map(u.get("MapID"))} · {u.get("RankedRatingEarned", 0):+d} RR → '
                f'{self.assets.rank(u["TierAfterUpdate"])} {u.get("RankedRatingAfterUpdate", 0)}')
        c.create_oval(px - 6, py - 6, px + 6, py + 6, outline=FG, width=2, tags="tip")
        tx = min(max(px, 160), c.winfo_width() - 160)
        t = c.create_text(tx, max(py - 20, 10), text=text, fill=FG, font=("Segoe UI", 9), tags="tip")
        bx = c.bbox(t)
        c.tag_lower(c.create_rectangle(bx[0] - 6, bx[1] - 3, bx[2] + 6, bx[3] + 3, fill=PANEL, outline=GRID, tags="tip"), t)

    def show_stats(self):
        """Your competitive win rate / KD / ACS / HS% per map and per agent from every saved match."""
        me, a = self.riot.puuid, self.assets
        matches = self.riot.my_matches()
        self.stats_head.config(text=f"Competitive · {len(matches)} of your matches saved on this PC")
        for tree, key, name, icon_kind in ((self.map_stats, "map", a.map, None),
                                           (self.agent_stats, "agent", a.agent, "agent")):
            groups = {}
            for _mid, m in matches:
                p = m["players"][me]
                g = groups.setdefault(m[key] if key == "map" else p["agent"], [0, 0, 0, 0, 0, 0, 0])
                res = outcome(m, me)
                g[0] += 1
                g[1] += res == "Win"
                g[2], g[3], g[4] = g[2] + p["k"], g[3] + p["d"], g[4] + p["acs"]
                g[5], g[6] = g[5] + p["head"], g[6] + p["hits"]
            tree.delete(*tree.get_children())
            for k, g in sorted(groups.items(), key=lambda kv: -kv[1][0]):
                icon = self.images.get(icon_kind, k, 24) if icon_kind else None
                tree.insert("", "end", text=" " + name(k), **({"image": icon} if icon else {}), values=(
                    g[0], f"{round(100 * g[1] / g[0])}%", f"{g[2] / max(g[3], 1):.2f}", round(g[4] / g[0]),
                    f"{round(100 * g[5] / g[6])}%" if g[6] else "—"))

    def load_more(self):
        self.want_more = True
        self.set_status("Loading older matches…")
        self.wake.set()

    def open_scoreboard(self, mid):
        """Scoreboard of a past match. Names only for players VLite saw with visible names in that
        match (or you); everyone else is shown by agent, so hidden players stay hidden."""
        m = self.riot._matches.get(mid)
        me = self.riot.puuid
        if not m or me not in m["players"]:
            return
        lobby = next((l for l in self.lobbies if l["match"] == mid), {"players": []})
        known = {p["puuid"]: p["name"] for p in lobby["players"]}
        known[me] = self.me_name
        a, my_team = self.assets, m["players"][me]["team"]
        rows = []
        for pid, p in sorted(m["players"].items(), key=lambda kv: (kv[1]["team"] != my_team, -kv[1]["acs"])):
            flag = self.flags.get(pid) if pid in known and pid != me else None
            rows.append({
                "puuid": pid, "ally": p["team"] == my_team, "me": pid == me, "hidden": pid not in known,
                "agent_id": p["agent"], "agent": a.agent(p["agent"]), "player": known.get(pid, "Name not shown"),
                "flag": f'⚑ {flag["note"]}' if flag else "", "flag_color": FLAG_COLOR,
                "kda": f'{p["k"]} / {p["d"]} / {p["a"]}', "kd": f'{p["k"] / max(p["d"], 1):.2f}', "acs": p["acs"],
                "hs": f'{round(100 * p["head"] / p["hits"])}%' if p["hits"] else "—",
            })
        d = tk.Toplevel(self.root)
        d.title("Scoreboard")
        d.configure(bg=BG)
        d.transient(self.root)
        when = datetime.fromtimestamp(m["start"] / 1000).strftime("%b %d %H:%M")
        ttk.Label(d, style="Head.TLabel", text=f'{a.map(m["map"])} · {QUEUE_NAMES.get(m["queue"], m["queue"].title())} · '
                                               f'{outcome(m, me)} {score(m, me)} · {when}').pack(anchor="w", padx=12, pady=10)
        table = PlayerTable(d, [col("Agent", "agent", "agent", "agent_id"), col("Player", "player"), col("Flag", "flag"),
                                col("K / D / A", "kda"), col("KD", "kd"), col("ACS", "acs"), col("HS%", "hs")],
                            self.images, BG, icon_px=26, size=10, row_bg=lambda row: ALLY_BG if row["ally"] else ENEMY_BG)
        table.pack(padx=10, pady=(0, 6))
        table.render(rows)
        ttk.Label(d, text="Names show only for players VLite saw with visible names in this match.",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(0, 10))

    def load_store(self):
        self.want_store = True
        self.set_status("Loading your store…")
        self.wake.set()

    def show_store(self, data):
        for child in self.store_body.winfo_children():
            child.destroy()
        if isinstance(data, str):
            ttk.Label(self.store_body, text=data, style="Muted.TLabel").pack(anchor="w")
            return
        wallet, daily, daily_left, night, night_left, info = data
        self.wallet_lbl.config(text=f'{wallet.get(VP_ID, 0):,} VP · {wallet.get(RAD_ID, 0):,} Radianite · '
                                    f'{wallet.get(KC_ID, 0):,} Kingdom Credits')

        def section(title, offers, left, row):
            ttk.Label(self.store_body, style="Head.TLabel", text=f"{title} · resets in {duration(left)}") \
                .grid(row=row, column=0, columnspan=6, sticky="w", pady=(10, 6))
            for i, (uuid, price, sale, pct) in enumerate(offers):
                card = tk.Frame(self.store_body, bg=PANEL, padx=10, pady=8)
                card.grid(row=row + 1, column=i, sticky="nsew", padx=(0, 8))
                img = self.images.get("skin", uuid.lower(), 200)
                tk.Label(card, image=img or "", bg=PANEL, height=70 if img else 3).pack()
                tk.Label(card, text=info[uuid.lower()][0], bg=PANEL, fg=FG, font=("Segoe UI Semibold", 9),
                         wraplength=200).pack(anchor="w", pady=(6, 0))
                cost = f"{sale:,} VP  (−{pct}%, was {price:,})" if sale else f"{price:,} VP" if price else ""
                tk.Label(card, text=cost, bg=PANEL, fg=FLAG_COLOR if sale else MUTED, font=("Segoe UI", 9)).pack(anchor="w")

        section("Daily offers", daily, daily_left, 0)
        if night:
            section("Night Market", night, night_left, 2)

    # ---------- background worker (all network calls happen here) ----------    # ---------- background worker (all network calls happen here) ----------
    def worker(self):
        self.post(self.set_status, "Loading game data and icons (first start takes a few seconds)…")
        self.assets.load()
        self.assets.download_icons()
        while True:
            delay = POLL_SECONDS
            try:
                if not self.riot.puuid:
                    self.riot.connect(self.assets.version)
                    self.me_name = self.riot.names([self.riot.puuid])[self.riot.puuid]
                if self.reload_profile:
                    self.reload_profile = False
                    self.post(self.set_status, "Loading your matches…")
                    hist = self.riot.history()
                    self.hist_loaded = max(self.hist_loaded, HISTORY_COUNT)
                    self.post(self.show_profile, self.riot.rank(self.riot.puuid, fresh=True), hist)
                if self.want_more:
                    self.want_more = False
                    self.riot.history(20, start=self.hist_loaded)
                    self.hist_loaded += 20
                    self.post(self.show_stats)
                if self.want_store:
                    self.want_store = False
                    self.post(self.show_store, self.fetch_store())
                self.poll_match()
                self.post(self.set_status, f"● {self.me_name} · {self.riot.region.upper()} · "
                                           f"{STATE_LABEL[self.kind]}  |  {self.hotkey_note}")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.post(self.set_status, "Riot asked us to slow down — pausing 60s")
                    delay = 60
                else:
                    self.post(self.set_status, f"Riot servers returned {e.code}, retrying…")
                    delay = 15
            except (FileNotFoundError, ConnectionError, urllib.error.URLError):
                self.riot.puuid = None
                self.post(self.set_status, "Waiting for the Riot Client / VALORANT to start…")
                delay = 10
            except Exception as e:
                self.riot.puuid = None
                self.post(self.set_status, f"Reconnecting… ({e})")
                delay = 10
            self.wake.wait(delay)
            self.wake.clear()

    def fetch_store(self):
        try:
            front, wallet = self.riot.store()
        except Exception as e:
            return f"Couldn't load your store ({e}). Try again in a minute."
        panel = front.get("SkinsPanelLayout") or {}
        daily = [(o["OfferID"], o.get("Cost", {}).get(VP_ID), None, None) for o in panel.get("SingleItemStoreOffers") or []] \
            or [(u, None, None, None) for u in panel.get("SingleItemOffers") or []]
        bonus = front.get("BonusStore") or {}
        night = []
        for b in bonus.get("BonusStoreOffers") or []:
            offer = b.get("Offer") or {}
            item = (offer.get("Rewards") or [{}])[0].get("ItemID") or offer.get("OfferID")
            night.append((item, offer.get("Cost", {}).get(VP_ID), (b.get("DiscountCosts") or {}).get(VP_ID),
                          b.get("DiscountPercent")))
        info = self.assets.skins([o[0] for o in daily + night])
        return (wallet, daily, panel.get("SingleItemOffersRemainingDurationInSeconds", 0), night,
                bonus.get("BonusStoreRemainingDurationInSeconds", 0), info)

    def poll_match(self):
        kind, match = self.riot.state()
        if kind != "menus" and self.kind == "menus":
            self.post(self.on_match_found, kind, self.assets.map(match.get("MapID")))
        elif kind == "ingame" and self.kind == "pregame" and self.settings["auto_hide_ingame"]:
            self.post(self.overlay.show, False)
        if kind == "menus":
            if self.kind != "menus":  # a match just ended: pull it into history shortly
                self.post(self.show_live, None, None, [], {}, {}, {}, {})
                threading.Timer(45, self.refresh).start()
            self.kind = kind
            return
        if self.kind == "menus":
            self.post(self.tabs.select, 0)
        self.kind = kind
        rows = self.riot.players(kind, match)
        visible = [p["puuid"] for p in rows if not p["hidden"]]
        names = self.riot.names(visible)
        with ThreadPoolExecutor(3) as ex:  # only new players cost a request; the rest are cached
            ranks = dict(zip(visible, ex.map(self.riot.rank, visible)))
        forms = {}
        for pid in visible:
            forms[pid] = self.riot.cached_form(pid)
            if not forms[pid][0] and pid not in self.form_pending:
                self.form_pending.add(pid)
                self.form_pool.submit(self._fetch_form, pid)
        parties = self.riot.parties(visible, match.get("ID"))
        self.post(self.show_live, kind, match, rows, names, ranks, forms, self.riot.encounters(), parties)

    def _fetch_form(self, puuid):
        try:
            self.riot.form(puuid)
        except Exception:
            self.riot._form[puuid] = (time.time(), None)  # don't retry until FORM_TTL passes
        finally:
            self.form_pending.discard(puuid)
            self.wake.set()  # redraw as soon as it lands


VP_ID, RAD_ID, KC_ID = ("85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741", "e59aa87c-4cbf-517a-5983-6e81511be9b7",
                        "85ca954a-41f2-ce94-9b45-8ca3dd39a00d")


def duration(seconds):
    hours, minutes = divmod(int(seconds or 0) // 60, 60)
    return f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h {minutes}m"


def main():
    if WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
