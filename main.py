#!/usr/bin/env python3
"""
Discord Quest Auto-Completer – live dashboard edition
Reads configuration from config.json
"""

import requests
import time
import json
import random
import sys
import re
import base64
import traceback
import threading
import shutil
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List

# ── Configuration ──────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

# ── Constants ──────────────────────────────────────────────────────────────────
BUILD_NUMBER_FALLBACK = 504649
ENROLL_LOCATION       = 11
MAX_RATE_LIMIT_WAITS  = 5
MAX_FETCH_RETRIES     = 3
VIDEO_TICK_INTERVAL   = 1.0
VIDEO_SPEED           = 7.0
VIDEO_MAX_FUTURE      = 10.0

# ─────────────────────────────────────────────────────────────────────────────
#  ANSI palette  (auto-stripped when not a TTY)
# ─────────────────────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()

def _a(code: str) -> str:
    return f"\033[{code}m" if _TTY else ""

class A:   # ANSI shortcuts
    RST   = _a("0")
    BOLD  = _a("1")
    DIM   = _a("2")
    # Foreground
    BLK   = _a("30")
    RED   = _a("91")
    GRN   = _a("92")
    YLW   = _a("93")
    BLU   = _a("94")
    MAG   = _a("95")
    CYN   = _a("96")
    WHT   = _a("97")
    # Background
    BBLK  = _a("40")
    # Cursor / screen
    HOME  = "\033[H"        if _TTY else ""
    CLR   = "\033[2J"       if _TTY else ""
    ELINE = "\033[K"        if _TTY else ""
    HIDE  = "\033[?25l"     if _TTY else ""
    SHOW  = "\033[?25h"     if _TTY else ""
    ALT   = "\033[?1049h"   if _TTY else ""   # enter alternate screen
    NORM  = "\033[?1049l"   if _TTY else ""   # leave alternate screen

# ── ASCII banner (matches the screenshot's outline/wireframe style) ─────────
BANNER_LINES = [
    r"  _   _______ _     ___   ____ ___ _______   __   _  _  ",
    r" | | / / ____| |   / _ \ / ___|_ _|_   _\ \ / /  \ \/ / ",
    r" | |/ /|  _| | |  | | | | |    | |  | |  \ V /    >  <  ",
    r" |___/ |_____|_____\___/ \____|___| |_|    \_/    /_/\_\ ",
]

# ─────────────────────────────────────────────────────────────────────────────
#  QuestState  – shared across threads
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class QuestState:
    quest:          dict
    task_type:      str
    seconds_needed: int
    seconds_done:   float
    enrolled_ts:    float
    name:           str
    reward:         str   = "—"
    completed:      bool  = False
    status:         str   = "queued"   # queued | running | done | error | skipped
    last_update:    float = field(default_factory=time.time)
    lock:           threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def advance(self, value: float):
        with self.lock:
            self.seconds_done = value
            self.status = "running"
            if self.seconds_done >= self.seconds_needed:
                self.completed = True
                self.status    = "done"

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds_needed - self.seconds_done)

    @property
    def pct(self) -> float:
        if self.seconds_needed == 0:
            return 100.0
        return min(100.0, self.seconds_done / self.seconds_needed * 100)


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard – pure ANSI, zero external dependencies
# ─────────────────────────────────────────────────────────────────────────────
LOG_LINES   = 8    # visible log rows at the bottom
RENDER_HZ   = 0.5  # seconds between redraws

@dataclass
class _LogEntry:
    ts:    str
    level: str
    msg:   str

class Dashboard:
    """
    Live terminal dashboard.
    Call .start() to begin rendering, .stop() to clean up.
    All public methods are thread-safe.
    """

    def __init__(self):
        self._lock        = threading.Lock()
        self._rows: List[QuestState] = []
        self._logs        = deque(maxlen=200)
        self._username    = "—"
        self._user_id     = "—"
        self._status_msg  = "INITIALIZING..."
        self._status_ok   = False
        self._next_scan   = 0.0
        self._cycle       = 0
        self._running     = False
        self._thread: Optional[threading.Thread] = None
        self._start_ts    = time.time()
        self._spin_frame  = 0   # animation tick for running quests

    # ── Public API ─────────────────────────────────────────────────────────
    def set_user(self, username: str, user_id: str):
        with self._lock:
            self._username = username
            self._user_id  = user_id

    def set_status(self, msg: str, ok: bool = True):
        with self._lock:
            self._status_msg = msg
            self._status_ok  = ok

    def set_next_scan(self, ts: float):
        with self._lock:
            self._next_scan = ts

    def set_cycle(self, n: int):
        with self._lock:
            self._cycle = n

    def set_rows(self, states: List[QuestState]):
        with self._lock:
            self._rows = list(states)

    def add_log(self, ts: str, level: str, msg: str):
        with self._lock:
            self._logs.append(_LogEntry(ts, level, msg))

    # ── Render thread ──────────────────────────────────────────────────────
    def start(self):
        if not _TTY:
            return
        self._running = True
        sys.stdout.write(A.ALT + A.HIDE)
        sys.stdout.flush()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="Dashboard")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if _TTY:
            sys.stdout.write(A.NORM + A.SHOW)
            sys.stdout.flush()

    def _loop(self):
        while self._running:
            try:
                self._spin_frame += 1
                self._render()
            except Exception:
                pass
            time.sleep(RENDER_HZ)

    # ── Drawing helpers ────────────────────────────────────────────────────
    @staticmethod
    def _tw() -> int:
        return shutil.get_terminal_size((100, 30)).columns

    @staticmethod
    def _th() -> int:
        return shutil.get_terminal_size((100, 30)).lines

    def _line(self, txt: str = "", fill: str = " ") -> str:
        """Pad/truncate txt to terminal width then clear to EOL."""
        tw = self._tw()
        # strip ANSI for length calc
        plain = re.sub(r'\033\[[^m]*m', '', txt)
        pad   = max(0, tw - len(plain))
        return txt + fill * pad + A.ELINE

    def _hline(self, char: str = "─", left: str = "├", right: str = "┤",
               color: str = "") -> str:
        tw  = self._tw()
        mid = char * (tw - 2)
        return self._line(f"{color}{left}{mid}{right}{A.RST}")

    def _render(self):
        with self._lock:
            rows   = list(self._rows)
            logs   = list(self._logs)
            uname  = self._username
            uid    = self._user_id
            smsg   = self._status_msg
            sok    = self._status_ok
            nscan  = self._next_scan
            cycle  = self._cycle
        spin   = self._spin_frame

        tw = self._tw()
        out: List[str] = []
        W = lambda t="", f=" ": self._line(t, f)
        H = lambda **kw: self._hline(**kw)

        SPIN_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

        # ── Banner ────────────────────────────────────────────────────────
        out.append(W())
        for bl in BANNER_LINES:
            pad = max(0, (tw - len(bl)) // 2)
            out.append(W(f"{A.GRN}{' '*pad}{bl}{A.RST}"))
        out.append(W())

        # ── Status bar ────────────────────────────────────────────────────
        scol  = A.GRN if sok else A.YLW
        sicon = "☑" if sok else "☐"
        eta   = ""
        if nscan > time.time():
            secs = int(nscan - time.time())
            eta  = f"   {A.DIM}next scan in {secs}s  scan #{cycle}{A.RST}"
        elapsed = int(time.time() - self._start_ts)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        uptime  = f"{h:02d}:{m:02d}:{s:02d}"
        out.append(W(
            f"  {scol}{A.BOLD}{sicon} {smsg}{A.RST}"
            f"{eta}"
            f"   {A.DIM}uptime {uptime}{A.RST}"
        ))

        # ── User info table ───────────────────────────────────────────────
        out.append(H(color=A.DIM))
        col1w = max(20, tw // 3)
        col2w = tw - col1w - 3
        out.append(W(
            f"{A.DIM}│{A.RST} {A.DIM}{'User Account':<{col1w-1}}{A.RST}"
            f"{A.DIM}│{A.RST} {A.DIM}{'User ID':<{col2w}}{A.RST}{A.DIM}│{A.RST}"
        ))
        out.append(H(color=A.DIM))
        uname_d = (uname[:col1w-2] + "…") if len(uname) > col1w-1 else uname
        uid_d   = (uid[:col2w-1]   + "…") if len(uid)   > col2w    else uid
        out.append(W(
            f"{A.DIM}│{A.RST} {A.CYN}{uname_d:<{col1w-1}}{A.RST}"
            f"{A.DIM}│{A.RST} {A.CYN}{uid_d:<{col2w}}{A.RST}{A.DIM}│{A.RST}"
        ))
        out.append(H(color=A.DIM))

        # ── Quest table ───────────────────────────────────────────────────
        out.append(W())
        out.append(W(f"  {A.WHT}{A.BOLD}■ LIVE PROGRESS{A.RST}"))
        out.append(W())

        NO_W   = 4
        STAT_W = 14
        REW_W  = 20
        TIME_W = 12
        NAME_W = max(16, tw - NO_W - REW_W - TIME_W - STAT_W - 7)

        top = (f"{A.DIM}┌{'─'*(NO_W+1)}┬{'─'*(NAME_W+1)}┬"
               f"{'─'*(REW_W+1)}┬{'─'*(TIME_W+1)}┬{'─'*(STAT_W+1)}┐{A.RST}")
        sep = (f"{A.DIM}├{'─'*(NO_W+1)}┼{'─'*(NAME_W+1)}┼"
               f"{'─'*(REW_W+1)}┼{'─'*(TIME_W+1)}┼{'─'*(STAT_W+1)}┤{A.RST}")
        bot = (f"{A.DIM}└{'─'*(NO_W+1)}┴{'─'*(NAME_W+1)}┴"
               f"{'─'*(REW_W+1)}┴{'─'*(TIME_W+1)}┴{'─'*(STAT_W+1)}┘{A.RST}")

        def _trow():
            return W(
                f"{A.DIM}│{A.RST}"
                f"{A.BOLD}{A.WHT} {'No':<{NO_W}}{A.RST}{A.DIM}│{A.RST}"
                f"{A.BOLD}{A.WHT} {'Quest Name':<{NAME_W}}{A.RST}{A.DIM}│{A.RST}"
                f"{A.BOLD}{A.WHT} {'Reward':<{REW_W}}{A.RST}{A.DIM}│{A.RST}"
                f"{A.BOLD}{A.WHT} {'Time Left':<{TIME_W}}{A.RST}{A.DIM}│{A.RST}"
                f"{A.BOLD}{A.WHT} {'Status':<{STAT_W}}{A.RST}{A.DIM}│{A.RST}"
            )

        out.append(W(top))
        out.append(_trow())

        if not rows:
            out.append(W(sep))
            out.append(W(
                f"{A.DIM}│{A.RST} {'':<{NO_W}}{A.DIM}│{A.RST}"
                f" {A.DIM}{'No active quests':<{NAME_W}}{A.RST}{A.DIM}│{A.RST}"
                f" {'':<{REW_W}}{A.DIM}│{A.RST}"
                f" {'':<{TIME_W}}{A.DIM}│{A.RST}"
                f" {'':<{STAT_W}}{A.DIM}│{A.RST}"
            ))
        else:
            for i, st in enumerate(rows, 1):
                out.append(W(sep))
                name_d = (st.name[:NAME_W-1]+"…") if len(st.name) > NAME_W else st.name
                rew_d  = (st.reward[:REW_W-1]+"…") if len(st.reward) > REW_W else st.reward

                # ── Time Left cell ─────────────────────────────────────
                if st.status == "done":
                    tl_col = A.GRN
                    tl_str = "✓ DONE"
                elif st.status == "running":
                    secs_left = int(st.remaining)
                    mm, ss    = divmod(secs_left, 60)
                    spinner   = SPIN_CHARS[spin % len(SPIN_CHARS)]
                    tl_col    = A.YLW
                    tl_str    = f"{spinner} {mm:02d}:{ss:02d}"
                elif st.status == "error":
                    tl_col = A.RED
                    tl_str = "✗ ERROR"
                elif st.status == "skipped":
                    tl_col = A.DIM
                    tl_str = "— SKIP"
                else:
                    tl_col = A.DIM
                    tl_str = "⏳ queued"
                tl_cell = f"{tl_col}{tl_str:<{TIME_W}}{A.RST}"

                # ── Status cell ────────────────────────────────────────
                if st.status == "done":
                    st_col = A.GRN;  st_str = "✓ Done"
                elif st.status == "running":
                    st_col = A.YLW;  st_str = f"▶ {st.task_type[:STAT_W-3]}"
                elif st.status == "error":
                    st_col = A.RED;  st_str = "✗ Error"
                elif st.status == "skipped":
                    st_col = A.DIM;  st_str = "— Skipped"
                else:
                    st_col = A.DIM;  st_str = "○ Queued"

                out.append(W(
                    f"{A.DIM}│{A.RST}"
                    f" {A.DIM}{i:<{NO_W}}{A.RST}{A.DIM}│{A.RST}"
                    f" {A.CYN}{name_d:<{NAME_W}}{A.RST}{A.DIM}│{A.RST}"
                    f" {A.MAG}{rew_d:<{REW_W}}{A.RST}{A.DIM}│{A.RST}"
                    f" {tl_cell}{A.DIM}│{A.RST}"
                    f" {st_col}{st_str:<{STAT_W}}{A.RST}{A.DIM}│{A.RST}"
                ))

        out.append(W(bot))

        # ── Log panel ─────────────────────────────────────────────────────
        out.append(W())
        out.append(W(f"  {A.WHT}{A.BOLD}■ RECENT LOG{A.RST}"))
        out.append(H(left="┌", right="┐", color=A.DIM))
        level_fmt = {
            "ok":       (A.GRN,  "[  OK]"),
            "warn":     (A.YLW,  "[WARN]"),
            "error":    (A.RED,  "[ ERR]"),
            "progress": (A.DIM,  "[PROG]"),
            "debug":    (A.DIM,  "[ DBG]"),
            "info":     (A.CYN,  "[INFO]"),
        }
        visible_logs = list(logs)[-LOG_LINES:]
        for entry in visible_logs:
            col, lbl = level_fmt.get(entry.level, (A.WHT, f"[{entry.level[:4].upper()}]"))
            msg_trunc = entry.msg[:tw-22]
            out.append(W(
                f"{A.DIM}│{A.RST} {A.DIM}{entry.ts}{A.RST} "
                f"{col}{lbl}{A.RST} {msg_trunc}"
            ))
        for _ in range(LOG_LINES - len(visible_logs)):
            out.append(W(f"{A.DIM}│{A.RST}"))
        out.append(H(left="└", right="┘", color=A.DIM))
        out.append(W(f"  {A.DIM}Ctrl+C to stop{A.RST}"))

        # ── Flush ─────────────────────────────────────────────────────────
        sys.stdout.write(A.HOME + "\n".join(out))
        sys.stdout.flush()


# ── Global dashboard instance (created in main()) ──────────────────────────────
_dash: Optional[Dashboard] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────
def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ {CONFIG_FILE} not found.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"❌ {CONFIG_FILE} contains invalid JSON")
        sys.exit(1)

config = load_config()

TOKEN = config.get("TOKEN_DISCORD", "")
if not TOKEN:
    print("❌ TOKEN_DISCORD not set in config.json")
    sys.exit(1)

POLL_INTERVAL      = config.get("POLL_INTERVAL", 60)
HEARTBEAT_INTERVAL = config.get("HEARTBEAT_INTERVAL", 20)
AUTO_ACCEPT        = config.get("AUTO_ACCEPT", True)
LOG_PROGRESS       = config.get("LOG_PROGRESS", True)
DEBUG              = config.get("DEBUG", False)

SUPPORTED_TASKS = [
    "WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY", "WATCH_VIDEO_ON_MOBILE", "PLAY_ON_MOBILE",
    "WATCH_VIDEO_ON_DESKTOP", "ACHIEVEMENT_IN_ACTIVITY",
]
HEARTBEAT_TASKS = {"PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY", "PLAY_ON_MOBILE"}
ACHIEVEMENT_TASKS = {"ACHIEVEMENT_IN_ACTIVITY"}
VIDEO_TASKS = {"WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE", "WATCH_VIDEO_ON_DESKTOP"}


# ─────────────────────────────────────────────────────────────────────────────
#  Logging  –  routes through dashboard when live, else plain print
# ─────────────────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "info"):
    if level == "debug" and not DEBUG:
        return
    if level == "progress" and not LOG_PROGRESS:
        return
    # strip ANSI for clean log storage
    clean = re.sub(r'\033\[[^m]*m', '', msg)
    ts = datetime.now().strftime("%H:%M:%S")
    if _dash:
        _dash.add_log(ts, level, clean)
    else:
        # fallback: plain print
        pfx = {
            "info": f"{A.CYN}[INFO]{A.RST}", "ok": f"{A.GRN}[  OK]{A.RST}",
            "warn": f"{A.YLW}[WARN]{A.RST}", "error": f"{A.RED}[ ERR]{A.RST}",
            "progress": f"{A.DIM}[PROG]{A.RST}", "debug": f"{A.DIM}[ DBG]{A.RST}",
        }.get(level, f"[{level.upper()}]")
        print(f"{A.DIM}{ts}{A.RST} {pfx} {msg}")


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def jitter(base: float, pct: float = 0.20) -> float:
    return base + random.uniform(-base*pct, base*pct)

def human_sleep(base: float, pct: float = 0.25):
    time.sleep(max(0.5, jitter(base, pct)))

def random_sleep(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))

def _wait_for_rate_limit(response: requests.Response, context: str = "") -> float:
    try:
        retry_after = response.json().get("retry_after", 10)
    except Exception:
        retry_after = 10
    wait = retry_after + random.uniform(0.5, 2)
    log(f"Rate limited{f' ({context})' if context else ''} – waiting {wait:.1f}s", "warn")
    time.sleep(wait)
    return wait


# ─────────────────────────────────────────────────────────────────────────────
#  Build number
# ─────────────────────────────────────────────────────────────────────────────
def fetch_latest_build_number() -> int:
    try:
        log("Fetching Discord build number...", "info")
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
        r = requests.get("https://discord.com/app", headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200:
            log(f"Discord page returned {r.status_code}, using fallback", "warn")
            return BUILD_NUMBER_FALLBACK
        scripts = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
        if not scripts:
            alts = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
            scripts = [s.split('/')[-1].replace('.js', '') for s in alts]
        for asset_hash in scripts[-5:]:
            try:
                ar = requests.get(f"https://discord.com/assets/{asset_hash}.js",
                                  headers={"User-Agent": ua}, timeout=15)
                m = re.search(r'buildNumber["\s:]+["\s]*(\d{5,7})', ar.text)
                if m:
                    bn = int(m.group(1))
                    log(f"Build number: {bn}", "ok")
                    return bn
            except Exception:
                continue
        log(f"Build number not found, using fallback {BUILD_NUMBER_FALLBACK}", "warn")
        return BUILD_NUMBER_FALLBACK
    except Exception as e:
        log(f"Error fetching build number: {e}, using fallback", "warn")
        return BUILD_NUMBER_FALLBACK


# ─────────────────────────────────────────────────────────────────────────────
#  Super-properties
# ─────────────────────────────────────────────────────────────────────────────
def make_super_properties(build_number: int) -> str:
    obj = {
        "os": "Windows", "browser": "Discord Client",
        "release_channel": "stable", "client_version": "1.0.9175",
        "os_version": "10.0.26100", "os_arch": "x64", "app_arch": "x64",
        "system_locale": "en-US",
        "browser_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 "
            "Electron/32.2.7 Safari/537.36"),
        "browser_version": "32.2.7",
        "client_build_number": build_number,
        "native_build_number": 59498,
        "client_event_source": None,
    }
    return base64.b64encode(json.dumps(obj, separators=(',', ':')).encode()).decode()


# ─────────────────────────────────────────────────────────────────────────────
#  Discord API
# ─────────────────────────────────────────────────────────────────────────────
class DiscordAPI:
    def __init__(self, token: str, build_number: int):
        self.token   = token
        self.session = requests.Session()
        self._lock   = threading.Lock()
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 "
              "Electron/32.2.7 Safari/537.36")
        self.session.headers.update({
            "Authorization": token, "Content-Type": "application/json",
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br", "User-Agent": ua,
            "X-Super-Properties": make_super_properties(build_number),
            "X-Discord-Locale": "en-US", "X-Discord-Timezone": "America/New_York",
            "X-Debug-Options": "bugReporterEnabled",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
            "DNT": "1", "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        })

    def get(self, path: str, **kw) -> requests.Response:
        log(f"GET {path}", "debug")
        with self._lock:
            time.sleep(random.uniform(0.1, 0.4))
            r = self.session.get(f"https://discord.com/api/v9{path}", **kw)
        log(f"  -> {r.status_code}", "debug")
        return r

    def post(self, path: str, payload: Optional[dict] = None, **kw) -> requests.Response:
        log(f"POST {path}", "debug")
        with self._lock:
            time.sleep(random.uniform(0.1, 0.4))
            r = self.session.post(f"https://discord.com/api/v9{path}", json=payload, **kw)
        log(f"  -> {r.status_code}", "debug")
        return r

    def validate_token(self) -> bool:
        try:
            r = self.get("/users/@me")
            if r.status_code == 200:
                user = r.json()
                uname = user.get("username", "?")
                uid   = str(user.get("id", "?"))
                log(f"Logged in as: {uname} (ID: {uid})", "ok")
                if _dash:
                    _dash.set_user(uname, uid)
                    _dash.set_status("SYSTEM RUNNING...", ok=True)
                return True
            log(f"Invalid token (status {r.status_code})", "error")
            if _dash:
                _dash.set_status("AUTH FAILED", ok=False)
            return False
        except Exception as e:
            log(f"Cannot connect to Discord: {e}", "error")
            return False


# ─────────────────────────────────────────────────────────────────────────────
#  Quest helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get(d: Optional[dict], *keys):
    if d is None: return None
    for k in keys:
        if k in d: return d[k]
    return None

def get_task_config(quest: dict) -> Optional[dict]:
    cfg = quest.get("config", {})
    return _get(cfg, "taskConfig", "task_config", "taskConfigV2", "task_config_v2")

def get_quest_name(quest: dict) -> str:
    cfg  = quest.get("config", {})
    msgs = cfg.get("messages", {})
    for key in ("questName", "quest_name", "gameTitle", "game_title"):
        v = msgs.get(key)
        if v: return v.strip()
    app_name = cfg.get("application", {}).get("name")
    if app_name: return app_name
    return f"Quest#{quest.get('id', '?')}"

def get_quest_reward(quest: dict) -> str:
    """Best-effort extraction of the reward label from a quest config."""
    cfg = quest.get("config", {})
    # try structured reward fields
    for key in ("rewardItems", "reward_items", "rewards"):
        items = cfg.get(key)
        if items and isinstance(items, list) and items:
            item = items[0]
            if isinstance(item, dict):
                name = item.get("name") or item.get("label") or item.get("item_name")
                if name: return name
    # try messages
    msgs = cfg.get("messages", {})
    for key in ("rewardDescription", "reward_description", "rewardTitle", "reward_title"):
        v = msgs.get(key)
        if v: return v.strip()
    return "—"

def get_expires_at(quest: dict) -> Optional[str]:
    cfg = quest.get("config", {})
    return _get(cfg, "expiresAt", "expires_at")

def get_user_status(quest: dict) -> dict:
    us = _get(quest, "userStatus", "user_status")
    return us if isinstance(us, dict) else {}

def is_completable(quest: dict) -> bool:
    expires = get_expires_at(quest)
    if expires:
        try:
            if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                return False
        except Exception: pass
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc: return False
    return any(tc["tasks"].get(t) is not None for t in SUPPORTED_TASKS)

def is_enrolled(quest: dict) -> bool:
    return bool(_get(get_user_status(quest), "enrolledAt", "enrolled_at"))

def is_completed(quest: dict) -> bool:
    return bool(_get(get_user_status(quest), "completedAt", "completed_at"))

def get_task_type(quest: dict) -> Optional[str]:
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc: return None
    for t in SUPPORTED_TASKS:
        if tc["tasks"].get(t) is not None: return t
    return None

def get_raw_task_keys(quest: dict) -> list:
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc: return []
    return list(tc["tasks"].keys())

def get_activity_quest_info(quest: dict) -> dict:
    tc = get_task_config(quest)
    if not tc: return {}
    task_data = tc.get("tasks", {}).get("ACHIEVEMENT_IN_ACTIVITY", {})
    app_id = None
    apps = task_data.get("applications") or []
    if apps and isinstance(apps, list): app_id = apps[0].get("id")
    if not app_id: app_id = quest.get("config", {}).get("application", {}).get("id")
    return {"app_id": app_id,
            "event_name": task_data.get("event_name", "progress"),
            "target": task_data.get("target", 1)}

def get_seconds_needed(quest: dict) -> int:
    tc = get_task_config(quest)
    tt = get_task_type(quest)
    if not tc or not tt: return 0
    return tc["tasks"][tt].get("target", 0)

def get_seconds_done(quest: dict) -> float:
    tt = get_task_type(quest)
    if not tt: return 0
    us = get_user_status(quest)
    return (us.get("progress") or {}).get(tt, {}).get("value", 0)

def get_enrolled_at(quest: dict) -> Optional[str]:
    return _get(get_user_status(quest), "enrolledAt", "enrolled_at")


# ─────────────────────────────────────────────────────────────────────────────
#  Core logic
# ─────────────────────────────────────────────────────────────────────────────
class QuestAutocompleter:
    def __init__(self, api: DiscordAPI):
        self.api            = api
        self._completed_ids: set = set()
        self._ids_lock      = threading.Lock()

    def mark_completed(self, qid: str):
        with self._ids_lock: self._completed_ids.add(qid)

    def is_already_done(self, qid: str) -> bool:
        with self._ids_lock: return qid in self._completed_ids

    # ── Fetch ──────────────────────────────────────────────────────────────
    def fetch_quests(self) -> list:
        for attempt in range(1, MAX_FETCH_RETRIES + 1):
            try:
                r = self.api.get("/quests/@me")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        blocked = _get(data, "quest_enrollment_blocked_until")
                        if blocked: log(f"Enrollment blocked until: {blocked}", "warn")
                        return data.get("quests", [])
                    return data if isinstance(data, list) else []
                elif r.status_code == 429:
                    if attempt >= MAX_FETCH_RETRIES:
                        log("Max fetch retries reached.", "error"); return []
                    _wait_for_rate_limit(r, f"fetch {attempt}/{MAX_FETCH_RETRIES}")
                else:
                    log(f"Quest fetch error ({r.status_code}): {r.text[:200]}", "warn"); return []
            except Exception as e:
                log(f"Error fetching quests: {e}", "error")
                if DEBUG: traceback.print_exc()
                return []
        return []

    # ── Enroll ────────────────────────────────────────────────────────────
    def enroll_quest(self, quest: dict) -> bool:
        name = get_quest_name(quest)
        qid  = quest["id"]
        for attempt in range(1, 4):
            try:
                random_sleep(1.5, 4.0)
                r = self.api.post(f"/quests/{qid}/enroll", {
                    "location": ENROLL_LOCATION, "is_targeted": False,
                    "metadata_raw": None, "metadata_sealed": None,
                    "traffic_metadata_raw":    quest.get("traffic_metadata_raw"),
                    "traffic_metadata_sealed": quest.get("traffic_metadata_sealed"),
                })
                if r.status_code == 429:
                    if attempt >= 3:
                        log(f'Skipping "{name}" after 3 rate limits', "warn"); return False
                    _wait_for_rate_limit(r, f'enrolling "{name}" {attempt}/3'); continue
                if r.status_code in (200, 201, 204):
                    log(f"Enrolled: {name}", "ok"); return True
                log(f'Enroll "{name}" failed ({r.status_code}): {r.text[:200]}', "warn")
                return False
            except Exception as e:
                log(f'Error enrolling "{name}" ({attempt}/3): {e}', "error")
                if attempt >= 3: return False
                time.sleep(random.uniform(1, 3))
        return False

    def auto_accept(self, quests: list) -> list:
        if not AUTO_ACCEPT: return quests
        unaccepted = [q for q in quests
                      if not is_enrolled(q) and not is_completed(q) and is_completable(q)]
        if not unaccepted: return quests
        log(f"Auto-accepting {len(unaccepted)} quest(s)...", "info")
        for q in unaccepted:
            self.enroll_quest(q)
            random_sleep(2, 5)
        human_sleep(2)
        return self.fetch_quests()

    # ── State factory ──────────────────────────────────────────────────────
    def _make_state(self, quest: dict) -> Optional[QuestState]:
        tt = get_task_type(quest)
        if not tt: return None
        ea = get_enrolled_at(quest)
        enrolled_ts = (
            datetime.fromisoformat(ea.replace("Z", "+00:00")).timestamp()
            if ea else time.time()
        )
        sd = get_seconds_done(quest)
        sn = get_seconds_needed(quest)
        return QuestState(
            quest=quest, task_type=tt, seconds_needed=sn, seconds_done=sd,
            enrolled_ts=enrolled_ts, name=get_quest_name(quest),
            reward=get_quest_reward(quest),
            completed=sd >= sn, status="done" if sd >= sn else "queued",
        )

    # ── Video group ────────────────────────────────────────────────────────
    def _run_video_group(self, states: List[QuestState]):
        log(f"Video group starting ({len(states)} quest(s))", "info")
        for s in states:
            log(f"  • {s.name}  {s.seconds_done:.0f}/{s.seconds_needed}s", "info")
            s.status = "running"

        while True:
            all_done = True
            for state in states:
                if state.completed: continue
                all_done = False
                qid = state.quest["id"]
                max_allowed = (time.time() - state.enrolled_ts) + VIDEO_MAX_FUTURE
                if max_allowed - state.seconds_done < VIDEO_SPEED: continue
                timestamp = min(float(state.seconds_needed),
                                state.seconds_done + VIDEO_SPEED + random.uniform(0, 0.5))
                try:
                    r = self.api.post(f"/quests/{qid}/video-progress", {"timestamp": timestamp})
                    if r.status_code == 200:
                        body = r.json()
                        state.advance(timestamp)
                        log(f"[{state.name}] {state.seconds_done:.0f}/{state.seconds_needed}s ({state.pct:.0f}%)", "progress")
                        if body.get("completed_at") or state.completed:
                            try: self.api.post(f"/quests/{qid}/video-progress", {"timestamp": state.seconds_needed})
                            except Exception: pass
                            log(f"Video done: {state.name}", "ok")
                            state.status = "done"; state.completed = True
                            self.mark_completed(qid)
                    elif r.status_code == 429:
                        _wait_for_rate_limit(r, state.name)
                    else:
                        log(f"Video error ({r.status_code}) [{state.name}]: {r.text[:200]}", "warn")
                except Exception as e:
                    log(f"Video error [{state.name}]: {e}", "error")
            if all_done: break
            time.sleep(VIDEO_TICK_INTERVAL)

    # ── Heartbeat group ────────────────────────────────────────────────────
    def _run_heartbeat_group(self, states: List[QuestState]):
        log(f"Heartbeat group starting ({len(states)} quest(s), one thread each)", "info")

        def _worker(state: QuestState):
            qid        = state.quest["id"]
            pid        = random.randint(1000, 30000)
            channel_id = random.randint(10**17, 10**18 - 1)
            stream_key = f"call:{channel_id}:{pid}"
            state.status = "running"
            log(f"{state.name}  ~{state.remaining // 60:.0f}m remaining [{state.task_type}]", "info")
            while not state.completed:
                try:
                    r = self.api.post(f"/quests/{qid}/heartbeat",
                                      {"stream_key": stream_key, "terminal": False})
                    if r.status_code == 200:
                        body = r.json()
                        pd   = body.get("progress", {})
                        if pd and state.task_type in pd:
                            state.advance(pd[state.task_type].get("value", state.seconds_done))
                        log(f"[{state.name}] {state.seconds_done:.0f}/{state.seconds_needed}s ({state.pct:.0f}%)", "progress")
                        if body.get("completed_at") or state.completed:
                            try: self.api.post(f"/quests/{qid}/heartbeat",
                                               {"stream_key": stream_key, "terminal": True})
                            except Exception: pass
                            log(f"Heartbeat done: {state.name}", "ok")
                            state.status = "done"; state.completed = True
                            self.mark_completed(qid); return
                    elif r.status_code == 429:
                        _wait_for_rate_limit(r, state.name); continue
                    else:
                        log(f"Heartbeat error ({r.status_code}) [{state.name}]: {r.text[:200]}", "warn")
                except Exception as e:
                    log(f"Heartbeat error [{state.name}]: {e}", "error")
                human_sleep(HEARTBEAT_INTERVAL, pct=0.15)

        workers = [threading.Thread(target=_worker, args=(s,),
                                    name=f"HB-{s.name[:20]}", daemon=True)
                   for s in states]
        for w in workers: w.start()
        for w in workers: w.join()

    # ── Achievement (manual only) ──────────────────────────────────────────
    def _handle_achievement(self, quest: dict):
        name  = get_quest_name(quest)
        info  = get_activity_quest_info(quest)
        us    = get_user_status(quest)
        already = int((us.get("progress") or {})
                      .get("ACHIEVEMENT_IN_ACTIVITY", {}).get("value", 0))
        target  = info.get("target", 1)
        ename   = info.get("event_name", "progress")
        log(f'Skipping "{name}" [ACHIEVEMENT — manual only] {already}/{target}', "warn")
        log(f"  ↳ Play Discord Activity until '{ename}' fires {target-already}x", "info")

    # ── Run all ────────────────────────────────────────────────────────────
    def run_all_quests(self, quests: list):
        video_states, hb_states = [], []
        all_states: List[QuestState] = []

        for quest in quests:
            qid  = quest.get("id")
            tt   = get_task_type(quest)
            name = get_quest_name(quest)
            if self.is_already_done(qid): continue
            if not tt:
                raw = get_raw_task_keys(quest)
                log(f'"{name}" — unknown task {raw}, skipping', "warn"); continue
            if tt in ACHIEVEMENT_TASKS:
                self._handle_achievement(quest); continue
            state = self._make_state(quest)
            if state is None or state.completed: continue
            all_states.append(state)
            if tt in VIDEO_TASKS: video_states.append(state)
            elif tt in HEARTBEAT_TASKS: hb_states.append(state)
            else: log(f"No handler for {tt} [{name}], skipping", "warn")

        if not all_states: return

        # push all states to dashboard
        if _dash: _dash.set_rows(all_states)

        threads = []
        if video_states:
            threads.append(threading.Thread(target=self._run_video_group,
                                            args=(video_states,), name="VideoGroup", daemon=True))
        if hb_states:
            threads.append(threading.Thread(target=self._run_heartbeat_group,
                                            args=(hb_states,), name="HeartbeatGroup", daemon=True))
        for t in threads: t.start()
        for t in threads: t.join()
        log("All quest groups finished.", "ok")

    # ── Main loop ──────────────────────────────────────────────────────────
    def run(self):
        log("Discord Quest Auto-Completer started", "ok")
        log(f"Auto-accept: {'ON' if AUTO_ACCEPT else 'OFF'}  Poll: {POLL_INTERVAL}s", "info")
        cycle = 0

        while True:
            cycle += 1
            if _dash: _dash.set_cycle(cycle)
            log(f"Scan #{cycle}", "info")
            if _dash: _dash.set_status("SCANNING...", ok=True)

            quests = self.fetch_quests()

            if not quests:
                log("No quests found", "info")
                if _dash: _dash.set_rows([])
            else:
                total     = len(quests)
                enrolled  = sum(1 for q in quests if is_enrolled(q))
                completed = sum(1 for q in quests if is_completed(q))
                log(f"Total: {total}  Enrolled: {enrolled}  Completed: {completed}", "info")

                # log each quest (dashboard shows table, so keep these brief)
                for q in quests:
                    name = get_quest_name(q)
                    tt   = get_task_type(q)
                    mark = "✓" if is_completed(q) else ("▶" if is_enrolled(q) else "○")
                    expires = get_expires_at(q)
                    expiry_note = ""
                    if expires and not is_completed(q):
                        try:
                            h = (datetime.fromisoformat(expires.replace("Z", "+00:00"))
                                 - datetime.now(timezone.utc)).total_seconds() / 3600
                            if h < 1:   expiry_note = f" ⚠ {h*60:.0f}m left!"
                            elif h < 6: expiry_note = f" ⚠ {h:.1f}h left"
                        except Exception: pass
                    log(f"  {mark} {name} [{tt or '?'}]{expiry_note}", "info")

                quests     = self.auto_accept(quests)
                actionable = [q for q in quests
                              if is_enrolled(q) and not is_completed(q)
                              and is_completable(q) and not self.is_already_done(q.get("id"))]

                if actionable:
                    log(f"{len(actionable)} quest(s) ready — launching parallel groups", "info")
                    if _dash: _dash.set_status("RUNNING QUESTS...", ok=True)
                    t0 = time.time()
                    self.run_all_quests(actionable)
                    elapsed = time.time() - t0

                    # summary
                    done_n = sum(1 for q in actionable if self.is_already_done(q.get("id")))
                    log(f"Session done: {done_n}/{len(actionable)} completed in {elapsed/60:.1f}m", "ok")
                    for q in actionable:
                        mark = "✅" if self.is_already_done(q.get("id")) else "⏳"
                        log(f"  {mark} {get_quest_name(q)}", "info")
                else:
                    log("No quests need completion right now", "info")
                    if _dash: _dash.set_rows([])

            wait = jitter(POLL_INTERVAL, 0.10)
            if _dash:
                _dash.set_status("SYSTEM RUNNING...", ok=True)
                _dash.set_next_scan(time.time() + wait)
            log(f"Waiting {wait:.0f}s...", "info")
            time.sleep(wait)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    global _dash

    _dash = Dashboard()
    _dash.set_status("INITIALIZING...", ok=False)
    _dash.start()

    try:
        log("Fetching build number...", "info")
        build_number = fetch_latest_build_number()
        api          = DiscordAPI(TOKEN, build_number)
        if not api.validate_token():
            _dash.stop()
            sys.exit(1)
        completer = QuestAutocompleter(api)
        completer.run()
    except KeyboardInterrupt:
        pass
    finally:
        _dash.stop()
        print(f"\n{A.GRN}Stopped.{A.RST}")

if __name__ == "__main__":
    main()
