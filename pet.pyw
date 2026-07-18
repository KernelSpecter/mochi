"""
Mochi — a living chibi cat for your Windows taskbar.

- Hand-drawn vector art (QPainter beziers, no sprites), modeled on soft flat
  "Cat Snack Bar" style chibi cats: taupe tabby, stripes, pink collar + bell.
- Emotion engine: continuous valence/arousal core-affect space with mood
  inertia, Ornstein-Uhlenbeck drift, circadian (crepuscular) rhythm, need
  coupling and a fixed random personality -> emotions emerge, never "switch".
- Needs: hunger / energy / fun / social / cleanliness, different decay rates,
  offline decay while the app is closed, JSON persistence.
- Utility-based behavior AI: patrols the taskbar, loafs, curls up to sleep,
  grooms, watches your cursor, begs when hungry, zoomies at dawn/dusk,
  occasionally brings you a gift.
- Interactions: stroke to pet, pick up & drag (he dangles), right-click to
  feed (kibble/fish/treat/milk), play (chases your cursor), brush, stats.
  Drop files/folders on him and he munches them into the Recycle Bin.
- Perching: he hops onto the top edge of your windows, rides them around,
  and naps up there; hops down by himself (or when you feed/play).
- Sounds: real CC0 cat recordings in sounds/ (see CREDITS.txt) — a purr that
  swells while you pet him and trails off after, chirps at butterflies, a
  meow when you come back; variants picked at random with volume jitter so
  he never repeats himself exactly. Mute in the menu. If the WAVs are
  deleted, stand-in sounds are synthesized on next launch.
- Bond: slow trust stat only interaction raises; unlocks perching on your
  active window, rarer gift kinds, and a tray "Call" he actually obeys.
- Power discipline: drops to ~12 fps while he sleeps/loafs undisturbed.
  He never auto-hides — he is always on top, over the taskbar, no exceptions
  (a self-healing check repairs Win11's silent topmost breakage).

Run:        double-click pet.pyw (pythonw, no console window)
Snapshot:   python pet.pyw --snapshot out.png    (render pose grid, no GUI)
Simulate:   python pet.pyw --simulate 48         (headless 48h systems test)
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QBrush, QColor, QCursor, QFont, QIcon,
                           QPainter, QPainterPath, QPen, QPixmap, QImage,
                           QRegion)
from PySide6.QtWidgets import (QApplication, QMenu, QSystemTrayIcon, QWidget,
                               QInputDialog)

try:
    from PySide6.QtMultimedia import QSoundEffect
except ImportError:
    QSoundEffect = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_PATH = os.path.join(APP_DIR, "cat_state.json")
SOUND_DIR = os.path.join(APP_DIR, "sounds")

FUR        = QColor("#A1978C")
FUR_LIGHT  = QColor("#C2B9AE")
FUR_DARK   = QColor("#8A8075")
STRIPE     = QColor("#6F675F")
EAR_INNER  = QColor("#E8A9B4")
NOSE       = QColor("#D98598")
BLUSH      = QColor(240, 168, 180, 95)
COLLAR     = QColor("#E88CA0")
BELL       = QColor("#F2C86B")
BELL_DARK  = QColor("#C89B3C")
EYE        = QColor("#332F2C")
WHISKER    = QColor(125, 116, 108, 110)
SHADOW     = QColor(60, 50, 45, 38)
HEART      = QColor("#F27E9B")
BUBBLE_BG  = QColor(255, 253, 249, 246)
BUBBLE_BRD = QColor(226, 213, 202, 255)
SPARKLE    = QColor("#F5D76E")
ZZZ        = QColor("#8FA6C8")

FOODS = {
    # name: (hunger, fun, weight_gain, is_favourite)
    "Kibble": (34, 2, 0.010, False),
    "Fish":   (55, 8, 0.018, True),
    "Treat":  (12, 14, 0.022, False),
    "Milk":   (16, 4, 0.012, False),
    "Trash":  (3, 12, 0.0, False),   # dropped files; never in the feed menus
}

FOOD_COLORS = {
    "Kibble": QColor("#B98A5B"),
    "Fish":   QColor("#8FB6C9"),
    "Treat":  QColor("#D9A66A"),
    "Milk":   QColor("#F5F2EC"),
}

SOUND_VOL = {"purr": 0.40, "chirp": 0.50, "meow": 0.50}


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def approach(cur, target, rate, dt):
    """Exponential smoothing toward target."""
    k = 1.0 - math.exp(-rate * dt)
    return cur + (target - cur) * k


@dataclass
class PetState:
    name: str = "Mochi"
    born: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    hunger: float = 72.0     # 0 starving .. 100 full
    energy: float = 80.0
    fun: float = 70.0
    social: float = 70.0
    clean: float = 85.0
    weight: float = 1.0
    x: float = -1.0          # world x on the taskbar (-1 = center on first run)
    size: float = 0.85
    gifts: int = 0
    files_eaten: int = 0
    bond: float = 12.0       # 0..100 trust; only interaction raises it
    muted: bool = False
    playful: float = 0.6
    needy: float = 0.5
    lazy: float = 0.5
    brave: float = 0.5

    def to_json(self):
        return self.__dict__.copy()

    @staticmethod
    def load() -> "PetState":
        st = PetState()
        try:
            with open(SAVE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(st, k):
                    setattr(st, k, v)
        except (OSError, ValueError):
            rng = random.Random()
            st.playful = rng.uniform(0.35, 0.9)
            st.needy = rng.uniform(0.3, 0.85)
            st.lazy = rng.uniform(0.25, 0.8)
            st.brave = rng.uniform(0.2, 0.9)
        return st

    def save(self):
        self.last_seen = time.time()
        tmp = SAVE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_json(), f, indent=2)
            os.replace(tmp, SAVE_PATH)
        except OSError:
            pass

    def apply_offline_decay(self):
        """He took care of himself while you were gone — decay at reduced
        rate, floor at 25 so he's never devastated, and he slept plenty."""
        hours = max(0.0, (time.time() - self.last_seen) / 3600.0)
        if hours < 0.05:
            return 0.0
        h = min(hours, 72.0)
        self.hunger = max(min(self.hunger, 25.0), self.hunger - 2.4 * h)
        self.fun = max(min(self.fun, 30.0), self.fun - 2.0 * h)
        self.social = max(min(self.social, 30.0), self.social - 1.6 * h)
        self.clean = max(min(self.clean, 35.0), self.clean - 0.8 * h)
        self.energy = clamp(self.energy + 12.0 * h, 0, 100)
        self.weight = clamp(self.weight - 0.002 * h, 0.85, 1.30)
        self.bond = clamp(self.bond - 0.4 * h / 24.0, 0, 100)
        return hours

    @property
    def age_days(self) -> float:
        return (time.time() - self.born) / 86400.0

    @property
    def growth(self) -> float:
        """Kittens grow over their first 10 days."""
        return 0.82 + 0.18 * clamp(self.age_days / 10.0, 0, 1)


class EmotionEngine:
    def __init__(self, st: PetState):
        self.st = st
        self.valence = 0.25
        self.arousal = 0.0
        self.rng = random.Random()
        self._noise_v = 0.0
        self._noise_a = 0.0

    @staticmethod
    def circadian_arousal(hour: float) -> float:
        """Cats are crepuscular: peaks near dawn (6h) and dusk (18h),
        troughs early afternoon and deep night."""
        def bump(center, width, amp):
            d = min(abs(hour - center), 24 - abs(hour - center))
            return amp * math.exp(-(d * d) / (2 * width * width))
        a = -0.35
        a += bump(6.5, 1.8, 0.8)
        a += bump(18.5, 2.2, 0.85)
        a += bump(12.0, 4.0, 0.25)
        a -= bump(2.5, 2.2, 0.35)
        return clamp(a, -0.75, 0.75)

    def sleep_pressure(self, hour: float) -> float:
        p = clamp((45 - self.st.energy) / 45.0, 0, 1)
        p += 0.30 * math.exp(-((hour - 14.0) ** 2) / 4.5)
        p += 0.35 * math.exp(-(min(abs(hour - 1.5), 24 - abs(hour - 1.5)) ** 2) / 6.0)
        return clamp(p, 0, 1.2)

    def event(self, dv: float, da: float):
        self.valence = clamp(self.valence + dv, -1, 1)
        self.arousal = clamp(self.arousal + da, -1, 1)

    def update(self, dt: float, hour: float):
        st = self.st
        press = lambda v, t: clamp((t - v) / t, 0, 1)  # noqa: E731
        hp, ep = press(st.hunger, 38), press(st.energy, 30)
        fp, sp = press(st.fun, 40), press(st.social, 35)
        cp = press(st.clean, 28)

        target_v = 0.45 - 0.95 * hp - 0.55 * fp - 0.5 * sp - 0.3 * cp - 0.2 * ep
        target_a = (self.circadian_arousal(hour) * (0.45 + 0.55 * st.playful)
                    + 0.35 * hp - 0.55 * ep + 0.15 * fp)

        s = math.sqrt(max(dt, 1e-3))
        self._noise_v += (-self._noise_v * dt / 70.0
                          + 0.055 * s * self.rng.gauss(0, 1))
        self._noise_a += (-self._noise_a * dt / 50.0
                          + 0.075 * s * self.rng.gauss(0, 1))
        self._noise_v = clamp(self._noise_v, -0.3, 0.3)
        self._noise_a = clamp(self._noise_a, -0.35, 0.35)

        self.valence = clamp(approach(self.valence, clamp(target_v + self._noise_v, -1, 1), 1 / 55.0, dt), -1, 1)
        self.arousal = clamp(approach(self.arousal, clamp(target_a + self._noise_a, -1, 1), 1 / 40.0, dt), -1, 1)

    def emotion(self) -> str:
        v, a = self.valence, self.arousal
        if v > 0.30:
            if a > 0.30:
                return "playful"
            if a < -0.30:
                return "content"
            return "happy"
        if v < -0.30:
            if a > 0.25:
                return "grumpy"
            if a < -0.25:
                return "sad"
            return "moody"
        if a > 0.45:
            return "curious"
        if a < -0.45:
            return "sleepy"
        return "calm"

    EMOJI = {"playful": "^-^", "content": "u_u", "happy": ":3",
             "grumpy": ">:(", "sad": ";_;", "moody": ":<",
             "curious": "o.o", "sleepy": "-_-zZ", "calm": ":]"}


IDLE_SIT, LOAF, SLEEP, WALK, GROOM, WATCH, BEG, ZOOMIES, EAT, PLAY, \
    DRAGGED, FALLING, GIFT, STRETCH, CHASE, GO_PERCH, HOP_DOWN, \
    CALLED = range(18)

POSE_OF_ACTION = {
    IDLE_SIT: "sit", LOAF: "loaf", SLEEP: "curl", WALK: "walk",
    GROOM: "groom", WATCH: "sit", BEG: "beg", ZOOMIES: "walk",
    EAT: "eat", PLAY: "walk", DRAGGED: "drag", FALLING: "drag",
    GIFT: "walk", STRETCH: "stretch", CHASE: "walk",
    GO_PERCH: "walk", HOP_DOWN: "walk", CALLED: "walk",
}


def tick_needs(st: PetState, dt: float, action: int):
    """Shared by the live app and --simulate so they can't drift apart."""
    sleeping = action == SLEEP
    st.hunger = clamp(st.hunger - dt / 3600 * (5.5 + 2.0 * st.playful)
                      * (0.35 if sleeping else 1), 0, 100)
    st.fun = clamp(st.fun - dt / 3600 * (4.0 + 3.0 * st.playful)
                   * (0.2 if sleeping else 1), 0, 100)
    if action == ZOOMIES:
        st.fun = clamp(st.fun + dt * 0.5, 0, 100)
    elif action == CHASE and st.fun < 65:
        st.fun = clamp(st.fun + dt * 0.12, 0, 65)     # staves off boredom, but
        # only real play with the owner gets fun above self-entertainment level
    elif action in (WALK, PLAY):
        st.fun = clamp(st.fun + dt * 0.06, 0, 100)
    st.social = clamp(st.social - dt / 3600 * (2.5 + 2.5 * st.needy)
                      * (0.3 if sleeping else 1), 0, 100)
    st.clean = clamp(st.clean - dt / 3600 * 2.2, 0, 100)
    if action == GROOM:
        st.clean = clamp(st.clean + dt * 0.55, 0, 100)
    if sleeping:
        st.energy = clamp(st.energy + dt / 3600 * 30.0, 0, 100)
    else:
        cost = 14.0 if action == ZOOMIES else \
            9.0 if action == CHASE else \
            6.0 if action in (WALK, PLAY, GO_PERCH, CALLED) else 3.2
        st.energy = clamp(st.energy - dt / 3600 * cost, 0, 100)
    st.bond = clamp(st.bond - dt / 86400.0, 0, 100)   # trust fades ~1/day


class Brain:
    def __init__(self, st: PetState, emo: EmotionEngine):
        self.st = st
        self.emo = emo
        self.rng = random.Random()
        self.now = time.time
        self.action = IDLE_SIT
        self.action_t = 0.0
        self.min_dur = 3.0
        self.walk_target = None
        self.woke_at = 0.0
        self.chase_x = None
        self.can_perch = False
        self.on_perch = False
        self.perch_minutes = 0.0

    def user_idle_minutes(self) -> float:
        try:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
                return ms / 60000.0
        except Exception:
            pass
        return 0.0

    def choose(self, hour: float, cursor_active: bool, cursor_near: bool):
        st, emo = self.st, self.emo
        if self.action in (DRAGGED, FALLING, EAT, PLAY, GIFT,
                           GO_PERCH, HOP_DOWN, CALLED):
            return self.action
        if self.action_t < self.min_dur:
            return self.action

        idle_min = self.user_idle_minutes()
        sleepy = emo.sleep_pressure(hour)
        awake_recently = (self.now() - self.woke_at) < 90

        u = {}
        u[IDLE_SIT] = 0.50
        u[LOAF] = 0.52 + 0.35 * st.lazy - 0.25 * max(0, emo.arousal)
        u[SLEEP] = (0.15 + 1.5 * sleepy + 0.25 * st.lazy
                    + (0.45 if idle_min > 6 else 0.0)
                    - (0.8 if awake_recently else 0.0)
                    + (0.5 if self.action == SLEEP else 0.0))
        u[WALK] = 0.32 + 0.55 * max(0.0, emo.arousal) + 0.25 * st.playful \
            + (0.15 if st.fun < 45 else 0.0)
        u[GROOM] = 0.15 + 1.05 * clamp((65 - st.clean) / 65.0, 0, 1) \
            - 0.3 * max(0, emo.arousal)
        u[WATCH] = (0.75 + 0.3 * st.playful) if cursor_active else 0.1
        if self.action == LOAF:
            u[WATCH] -= 0.5   # he watches from the loaf; no need to get up
        u[BEG] = (1.35 * clamp((36 - st.hunger) / 36.0, 0, 1)
                  + 0.25 * st.needy) if st.hunger < 42 else 0.0
        u[ZOOMIES] = 0.0
        if (emo.arousal > 0.5 and st.energy > 45 and not self.on_perch
                and self.rng.random() < 0.06):
            u[ZOOMIES] = 2.5
        if (st.social > 70 and emo.valence > 0.35 and st.gifts < 99
                and not self.on_perch
                and self.rng.random() < 0.004 + 0.012 * st.bond / 100.0):
            u[GIFT] = 2.2
        if self.chase_x is not None and st.energy > 25 and not self.on_perch:
            u[CHASE] = 0.9 + 0.9 * st.playful + 0.4 * max(0.0, emo.arousal)
        if (self.can_perch and not self.on_perch and st.energy > 35
                and self.rng.random() < 0.012):
            u[GO_PERCH] = 2.1
        if self.on_perch:
            u[HOP_DOWN] = (0.05 + 1.3 * clamp((36 - st.hunger) / 36.0, 0, 1)
                           + 0.03 * self.perch_minutes
                           + (0.6 if self.rng.random() < 0.01 else 0.0))
        u[self.action] = u.get(self.action, 0) + 0.18  # hysteresis

        best = max(u, key=u.get)
        if best != self.action:
            if self.action == SLEEP:
                self.set_action(STRETCH)
            else:
                self.set_action(best)
        return self.action

    def set_action(self, a: int, min_dur: float | None = None):
        if self.action == SLEEP and a != SLEEP:
            self.woke_at = self.now()
        self.action = a
        self.action_t = 0.0
        self.walk_target = None
        durs = {IDLE_SIT: (4, 10), LOAF: (8, 22), SLEEP: (120, 420),
                WALK: (4, 9), GROOM: (6, 12), WATCH: (4, 9), BEG: (6, 12),
                ZOOMIES: (4, 7), EAT: (900, 900), PLAY: (900, 900),
                DRAGGED: (900, 900), FALLING: (900, 900), GIFT: (900, 900),
                STRETCH: (2.0, 3.2), CHASE: (3, 6),
                GO_PERCH: (900, 900), HOP_DOWN: (900, 900),
                CALLED: (900, 900)}
        lo, hi = durs.get(a, (4, 9))
        self.min_dur = min_dur if min_dur is not None else self.rng.uniform(lo, hi)


@dataclass
class DrawParams:
    pose: str = "sit"
    dir: int = 1               # 1 facing right, -1 left
    breath: float = 0.0
    walk_phase: float = 0.0
    walk_speed: float = 1.0
    y_off: float = 0.0
    squash: float = 1.0
    head_dx: float = 0.0
    head_dy: float = 0.0
    head_tilt: float = 0.0
    ear_back: float = 0.0
    ear_twitch: float = 0.0
    ear_twitch_side: int = 1
    ear_perk: float = 0.0
    eye_open: float = 1.0
    eye_happy: float = 0.0
    pupil_dx: float = 0.0
    pupil_dy: float = 0.0
    eye_big: float = 0.0
    mouth: str = "smile"       # smile flat o munch frown
    blush: float = 0.5
    tail_wag: float = 0.0
    tail_lift: float = 0.3
    groom_t: float = 0.0
    dangle: float = 0.0
    weight: float = 1.0
    sleep_sink: float = 0.0


class CatPainter:
    """Draws the chibi cat in local space: origin = feet center on the
    ground, +x right, -y up. Roughly 190 units tall seated."""

    def draw(self, p: QPainter, d: DrawParams, scale: float):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.scale(scale, scale)

        shade_w = {"sit": 118, "loaf": 138, "curl": 140, "walk": 130,
                   "eat": 120, "groom": 118, "beg": 112, "drag": 60,
                   "stretch": 152}.get(d.pose, 120)
        sh = QColor(SHADOW)
        if d.pose == "drag":
            sh.setAlpha(int(38 * clamp(1 - abs(d.y_off) / 260.0, 0.2, 1)))
        p.setBrush(sh)
        p.drawEllipse(QPointF(0, -3), shade_w * 0.5 * d.weight, 9)

        if d.dir < 0:
            p.scale(-1, 1)

        # drag pose: the window follows the cursor, y_off is shadow-only
        p.translate(0, 0 if d.pose == "drag" else d.y_off)
        p.scale(2.0 - d.squash, d.squash)

        if d.pose == "sit":
            self._sit(p, d)
        elif d.pose == "loaf":
            self._loaf(p, d)
        elif d.pose == "curl":
            self._curl(p, d)
        elif d.pose == "walk":
            self._walk(p, d)
        elif d.pose == "eat":
            self._eat(p, d)
        elif d.pose == "groom":
            self._sit(p, d, groom=True)
        elif d.pose == "beg":
            self._sit(p, d, beg=True)
        elif d.pose == "drag":
            self._drag(p, d)
        elif d.pose == "stretch":
            self._stretch(p, d)
        p.restore()


    def _tail_path(self, ox, oy, lift, wag, curl_front=True):
        """Tail as a cubic; returns (path, tip)."""
        path = QPainterPath(QPointF(ox, oy))
        if curl_front:
            wagx = wag * 8
            c1 = QPointF(ox + 26, oy + 10)
            c2 = QPointF(ox + 36, 4 - lift * 26)
            tip = QPointF(ox - 18 + wagx, 0 - lift * 40)
            path.cubicTo(c1, c2, tip)
        else:
            c1 = QPointF(ox - 16, oy - 26 - lift * 18)
            c2 = QPointF(ox - 34 + wag * 6, oy - 52 - lift * 26)
            tip = QPointF(ox - 18 + wag * 14, oy - 66 - lift * 34)
            path.cubicTo(c1, c2, tip)
        return path, tip

    def _draw_tail(self, p, path, tip, width=15.0):
        pen = QPen(FUR, width, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        pen2 = QPen(STRIPE, width, Qt.PenStyle.CustomDashLine,
                    Qt.PenCapStyle.FlatCap)
        pen2.setDashPattern([0.16, 0.55, 0.16, 2.0])
        pen2.setDashOffset(-2.6)
        p.setPen(pen2)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(STRIPE)
        p.drawEllipse(tip, width * 0.5, width * 0.5)

    def _head(self, p, d: DrawParams, cx, cy, r=52.0, lying=0.0):
        """Head with ears, face. cx,cy = head center."""
        p.save()
        p.translate(cx + d.head_dx, cy + d.head_dy)
        p.rotate(d.head_tilt + lying * 18)
        rx, ry = r, r * 0.88

        for side in (-1, 1):
            back = d.ear_back * (1.0 if side < 0 else 0.85)
            p.save()
            bx = side * rx * 0.62
            by = -ry * 0.62
            p.translate(bx, by)
            rot = side * (18 + back * 52 - d.ear_perk * 9)
            rot += side * math.sin(d.breath * 2 * math.pi + side) * 1.6
            if side == d.ear_twitch_side:
                rot += d.ear_twitch * side * 12
            p.rotate(rot)
            ear = QPainterPath(QPointF(-16, 6))
            ear.cubicTo(QPointF(-13, -16), QPointF(-6, -30), QPointF(0, -33))
            ear.cubicTo(QPointF(8, -28), QPointF(14, -12), QPointF(15, 7))
            ear.closeSubpath()
            p.setBrush(FUR)
            p.drawPath(ear)
            inner = QPainterPath(QPointF(-8, 2))
            inner.cubicTo(QPointF(-6, -10), QPointF(-3, -19), QPointF(1, -22))
            inner.cubicTo(QPointF(6, -17), QPointF(9, -7), QPointF(9, 3))
            inner.closeSubpath()
            p.setBrush(EAR_INNER)
            p.drawPath(inner)
            p.restore()

        head = QPainterPath()
        head.setFillRule(Qt.FillRule.WindingFill)
        head.addEllipse(QPointF(0, 0), rx, ry)
        head.addEllipse(QPointF(-rx * 0.62, ry * 0.28), rx * 0.42, ry * 0.34)
        head.addEllipse(QPointF(rx * 0.62, ry * 0.28), rx * 0.42, ry * 0.34)
        p.setBrush(FUR)
        p.drawPath(head.simplified())

        p.setBrush(STRIPE)
        for sx in (-15, 0, 15):
            ln = 16 if sx == 0 else 12
            path = QPainterPath()
            path.addRoundedRect(QRectF(sx - 3.2, -ry + 2, 6.4, ln), 3.2, 3.2)
            p.drawPath(path)
        for side in (-1, 1):
            for i in range(2):
                w = 12 - i * 3
                rect = QRectF(side * (rx - 2) - (w if side > 0 else 0),
                              -6 + i * 10, w, 5.4)
                path = QPainterPath()
                path.addRoundedRect(rect, 2.7, 2.7)
                p.drawPath(path)

        p.setBrush(QColor(FUR_LIGHT.red(), FUR_LIGHT.green(),
                          FUR_LIGHT.blue(), 80))
        p.drawEllipse(QPointF(0, ry * 0.44), rx * 0.30, ry * 0.17)

        if d.blush > 0.02:
            b = QColor(BLUSH)
            b.setAlpha(int(95 * d.blush))
            p.setBrush(b)
            p.drawEllipse(QPointF(-rx * 0.62, ry * 0.30), 10, 5.5)
            p.drawEllipse(QPointF(rx * 0.62, ry * 0.30), 10, 5.5)

        ex, ey = rx * 0.42, -ry * 0.05
        if d.eye_happy > 0.5:
            pen = QPen(EYE, 3.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for side in (-1, 1):
                path = QPainterPath(QPointF(side * ex - 7, ey + 2))
                path.quadTo(QPointF(side * ex, ey - 6), QPointF(side * ex + 7, ey + 2))
                p.drawPath(path)
            p.setPen(Qt.PenStyle.NoPen)
        elif d.eye_open <= 0.12:
            pen = QPen(EYE, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for side in (-1, 1):
                path = QPainterPath(QPointF(side * ex - 6.5, ey))
                path.quadTo(QPointF(side * ex, ey + 5), QPointF(side * ex + 6.5, ey))
                p.drawPath(path)
            p.setPen(Qt.PenStyle.NoPen)
        else:
            open_ = clamp(d.eye_open, 0.06, 1.0)
            er = 6.2 + 2.4 * d.eye_big
            for side in (-1, 1):
                cx_e = side * ex + d.pupil_dx
                cy_e = ey + d.pupil_dy
                p.setBrush(EYE)
                p.drawEllipse(QPointF(cx_e, cy_e), er, er * 1.28 * open_)
                if open_ > 0.25:
                    p.setBrush(QColor(255, 255, 255, 235))
                    p.drawEllipse(QPointF(cx_e - er * 0.28,
                                          cy_e - er * 0.45 * open_),
                                  er * 0.30, er * 0.30)
                    if d.eye_big > 0.3:
                        p.drawEllipse(QPointF(cx_e + er * 0.30,
                                              cy_e + er * 0.35 * open_),
                                      er * 0.16, er * 0.16)

        p.setBrush(NOSE)
        nose = QPainterPath(QPointF(-4.6, ry * 0.30))
        nose.quadTo(QPointF(0, ry * 0.30 - 3.4), QPointF(4.6, ry * 0.30))
        nose.quadTo(QPointF(1.8, ry * 0.30 + 4.6), QPointF(0, ry * 0.30 + 4.8))
        nose.quadTo(QPointF(-1.8, ry * 0.30 + 4.6), QPointF(-4.6, ry * 0.30))
        p.drawPath(nose)

        my = ry * 0.30 + 5.4
        pen = QPen(QColor(EYE.red(), EYE.green(), EYE.blue(), 200), 2.6,
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if d.mouth == "smile":
            m = QPainterPath(QPointF(0, my))
            m.quadTo(QPointF(-3.4, my + 5.2), QPointF(-8.2, my + 3.0))
            m.moveTo(QPointF(0, my))
            m.quadTo(QPointF(3.4, my + 5.2), QPointF(8.2, my + 3.0))
            p.drawPath(m)
        elif d.mouth == "flat":
            p.drawLine(QPointF(-5.5, my + 3.5), QPointF(5.5, my + 3.5))
        elif d.mouth == "frown":
            m = QPainterPath(QPointF(-6, my + 5.5))
            m.quadTo(QPointF(0, my + 1.5), QPointF(6, my + 5.5))
            p.drawPath(m)
        elif d.mouth == "o":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(EYE)
            p.drawEllipse(QPointF(0, my + 3.4), 2.7, 3.2)
        elif d.mouth == "munch":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(EYE)
            p.drawEllipse(QPointF(0, my + 4), 4.6, 3.2)
        p.setPen(Qt.PenStyle.NoPen)

        pen = QPen(WHISKER, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for side in (-1, 1):
            wx = side * rx * 0.88
            for i, dy in enumerate((-2, 6)):
                x2 = wx + side * 17
                p.drawLine(QPointF(wx, ry * 0.30 + dy),
                           QPointF(x2, ry * 0.30 + dy + i * 4 - 1))
        p.setPen(Qt.PenStyle.NoPen)
        p.restore()

    def _collar(self, p, cx, cy, w):
        p.save()
        p.translate(cx, cy)
        pen = QPen(COLLAR, 8.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(-w / 2, -3))
        path.quadTo(QPointF(0, 6), QPointF(w / 2, -3))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(BELL)
        p.drawEllipse(QPointF(0, 9), 5.6, 5.6)
        p.setBrush(BELL_DARK)
        p.drawEllipse(QPointF(0, 11.4), 1.6, 1.6)
        p.setBrush(QColor(255, 255, 255, 130))
        p.drawEllipse(QPointF(-1.8, 7.0), 1.7, 1.7)
        p.restore()

    def _body_stripes_side(self, p, rx, ry, cy, n=3):
        """Short stripes hugging the silhouette edge of an ellipse body."""
        p.setBrush(STRIPE)
        for side in (-1, 1):
            for i in range(n):
                y = cy - 14 + i * 13
                t = (y - cy) / ry
                half = rx * math.sqrt(max(0.0, 1 - t * t)) - 1.5
                w = 12 - i * 2
                rect = QRectF(side * half - (w if side > 0 else 0), y, w, 5.4)
                path = QPainterPath()
                path.addRoundedRect(rect, 2.7, 2.7)
                p.drawPath(path)


    def _sit(self, p, d: DrawParams, groom=False, beg=False):
        br = 1.0 + 0.022 * math.sin(d.breath * 2 * math.pi)
        wfat = 0.9 + 0.24 * (d.weight - 0.85) / 0.45

        wag = math.sin(d.tail_wag)
        path, tip = self._tail_path(40, -16, d.tail_lift, wag, curl_front=True)
        self._draw_tail(p, path, tip)

        body = QPainterPath()
        body.setFillRule(Qt.FillRule.WindingFill)
        body.addEllipse(QPointF(0, -40), 54 * wfat, 42 * br)
        body.addEllipse(QPointF(0, -62), 40 * wfat, 46 * br)
        p.setBrush(FUR)
        p.drawPath(body.simplified())
        self._body_stripes_side(p, 54 * wfat, 42, -40, 3)

        if beg:
            self._front_leg(p, -15, raised=0.0)
            self._front_leg(p, 15, raised=0.65)
        elif groom:
            self._front_leg(p, -13, raised=0.0)
            lick = 0.5 + 0.5 * math.sin(d.groom_t * 7.0)
            self._groom_leg(p, 16, lick)
        else:
            self._front_leg(p, -15)
            self._front_leg(p, 15)
            pen = QPen(QColor(FUR_DARK.red(), FUR_DARK.green(),
                              FUR_DARK.blue(), 110), 2.2,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(0, -26), QPointF(0, -5))
            p.setPen(Qt.PenStyle.NoPen)

        hy = -122 * br
        if groom:
            d2_tilt, d2_dx = 14, 8
        elif beg:
            d2_tilt, d2_dx = -4, 0
        else:
            d2_tilt = d2_dx = 0
        p.save()
        p.translate(d2_dx, 0)
        old_tilt = d.head_tilt
        d.head_tilt = old_tilt + d2_tilt
        self._head(p, d, 0, hy)
        d.head_tilt = old_tilt
        p.restore()

        self._collar(p, d.head_dx * 0.5, -86 * br, 58)

    def _front_leg(self, p, x, raised=0.0):
        p.setBrush(FUR)
        top = -58
        bottom = -raised * 34
        path = QPainterPath()
        path.addRoundedRect(QRectF(x - 8, top, 16, -top + bottom - 0), 8, 8)
        p.save()
        if raised > 0.05:
            p.translate(x, top)
            p.rotate(-raised * 28)
            p.translate(-x, -top)
        p.drawPath(path)
        pen = QPen(QColor(FUR_DARK.red(), FUR_DARK.green(), FUR_DARK.blue(), 120),
                   1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        fy = bottom - 1.5
        p.drawLine(QPointF(x - 2.8, fy), QPointF(x - 2.8, fy - 4))
        p.drawLine(QPointF(x + 2.8, fy), QPointF(x + 2.8, fy - 4))
        p.setPen(Qt.PenStyle.NoPen)
        p.restore()

    def _groom_leg(self, p, x, lick):
        """Front leg raised to the face, small bob for licking."""
        p.save()
        p.translate(x, -58)
        p.rotate(-58 - lick * 8)
        p.setBrush(FUR)
        path = QPainterPath()
        path.addRoundedRect(QRectF(-8, -6, 16, 52), 8, 8)
        p.drawPath(path)
        p.restore()

    def _leg(self, p, hx, hy, ph, color, width=13.0, stride=8.0):
        """One jointed walking leg: hip anchored inside the body, paw
        stepping along the ground. The knee bows toward the tail and bends
        more as the paw lifts — each leg gets its own gait phase."""
        lift = max(0.0, math.sin(ph)) * 8.5
        # planted paw drifts BACK under the body, lifted paw swings forward —
        # cos(ph) unnegated reads as moonwalking
        swing = -math.cos(ph) * stride
        fx, fy = hx + swing, -lift - width * 0.32
        d_ = math.hypot(fx - hx, fy - hy)
        mx, my = (hx + fx) / 2, (hy + fy) / 2
        nx, ny = -(fy - hy) / max(d_, 1e-3), (fx - hx) / max(d_, 1e-3)
        if nx > 0:
            nx, ny = -nx, -ny
        bend = 4.5 + lift * 0.55
        kx, ky = mx + nx * bend, my + ny * bend
        pen = QPen(color, width, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(hx, hy))
        path.quadTo(QPointF(kx, ky), QPointF(fx, fy))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(fx + 2.2, -lift - width * 0.30),
                      width * 0.60, width * 0.40)

    def _loaf(self, p, d: DrawParams):
        br = 1.0 + 0.02 * math.sin(d.breath * 2 * math.pi)
        wfat = 0.92 + 0.22 * (d.weight - 0.85) / 0.45
        wag = math.sin(d.tail_wag) * 0.5

        path, tip = self._tail_path(52, -14, 0.05, wag, curl_front=True)
        self._draw_tail(p, path, tip, width=13)

        body = QPainterPath()
        body.setFillRule(Qt.FillRule.WindingFill)
        body.addEllipse(QPointF(0, -32), 62 * wfat, 32 * br)
        body.addEllipse(QPointF(-6, -44), 46 * wfat, 26 * br)
        p.setBrush(FUR)
        p.drawPath(body.simplified())
        self._body_stripes_side(p, 62 * wfat, 32, -32, 2)

        self._head(p, d, 2, -94 * br, r=46)

    def _curl(self, p, d: DrawParams):
        """Sleeping curled into a crescent, head resting at the front."""
        br = 1.0 + 0.035 * math.sin(d.breath * 2 * math.pi)
        blob = QPainterPath()
        blob.setFillRule(Qt.FillRule.WindingFill)
        blob.addEllipse(QPointF(4, -26 * br), 58, 26 * br)
        blob.addEllipse(QPointF(12, -36 * br), 44, 22 * br)
        p.setBrush(FUR)
        p.drawPath(blob.simplified())
        p.setBrush(STRIPE)
        for sx in (-4, 14, 32):
            t = (sx - 12) / 44.0
            top = -36 * br - 22 * br * math.sqrt(max(0.0, 1 - t * t))
            rect = QRectF(sx - 3, top + 3, 6, 11)
            path2 = QPainterPath()
            path2.addRoundedRect(rect, 3, 3)
            p.drawPath(path2)
        pen = QPen(FUR, 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        path = QPainterPath(QPointF(48, -12))
        path.cubicTo(QPointF(60, 0), QPointF(18, 6), QPointF(-24, -4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        pen2 = QPen(STRIPE, 12, Qt.PenStyle.CustomDashLine, Qt.PenCapStyle.FlatCap)
        pen2.setDashPattern([0.14, 0.5, 0.14, 2.2])
        p.setPen(pen2)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(STRIPE)
        p.drawEllipse(QPointF(-24, -4), 6, 6)
        d2 = DrawParams(**{**d.__dict__})
        d2.eye_open = 0.0
        d2.eye_happy = 0.0
        d2.mouth = "flat"
        d2.head_dx = d2.head_dy = d2.head_tilt = 0.0
        d2.blush = min(d.blush, 0.4)
        self._head(p, d2, -28, -42 * br, r=42, lying=0.35)

    def _walk(self, p, d: DrawParams):
        ph = d.walk_phase
        bob = math.sin(ph * 2) * 2.6
        wfat = 0.92 + 0.22 * (d.weight - 0.85) / 0.45

        wag = math.sin(d.tail_wag)
        path, tip = self._tail_path(-46, -44, d.tail_lift, wag, curl_front=False)
        self._draw_tail(p, path, tip, width=13)

        self._leg(p, -30, -38, ph, QColor(FUR_DARK), width=12)
        self._leg(p, 24, -40, ph + math.pi * 0.5, QColor(FUR_DARK), width=12)

        body = QPainterPath()
        body.setFillRule(Qt.FillRule.WindingFill)
        body.addEllipse(QPointF(0, -46 + bob * 0.4), 58 * wfat, 33)
        body.addEllipse(QPointF(22, -52 + bob * 0.4), 40 * wfat, 30)
        p.setBrush(FUR)
        p.drawPath(body.simplified())
        p.setBrush(STRIPE)
        for i in range(3):
            rect = QRectF(-18 + i * 17, -76 + bob * 0.4 + (i % 2) * 2, 6, 13)
            sp = QPainterPath()
            sp.addRoundedRect(rect, 3, 3)
            p.drawPath(sp)

        self._leg(p, -22, -36, ph + math.pi, FUR, width=13)
        self._leg(p, 30, -38, ph + math.pi * 1.5, FUR, width=13)

        self._collar(p, 40, -56 + bob, 46)
        self._head(p, d, 42, -84 + bob, r=45)

    def _eat(self, p, d: DrawParams):
        """Standing with the head dipped down to the bowl in front."""
        wfat = 0.92 + 0.22 * (d.weight - 0.85) / 0.45
        bobble = math.sin(d.groom_t * 5.5)
        wag = math.sin(d.tail_wag)

        path, tip = self._tail_path(-46, -40, 0.15, wag * 0.6, curl_front=False)
        self._draw_tail(p, path, tip, width=13)

        self._leg(p, -30, -38, math.pi, QColor(FUR_DARK), width=12)
        self._leg(p, 24, -40, 0.0, QColor(FUR_DARK), width=12)

        body = QPainterPath()
        body.setFillRule(Qt.FillRule.WindingFill)
        body.addEllipse(QPointF(0, -46), 58 * wfat, 33)
        body.addEllipse(QPointF(20, -48), 40 * wfat, 30)
        p.setBrush(FUR)
        p.drawPath(body.simplified())
        p.setBrush(STRIPE)
        for i in range(3):
            rect = QRectF(-20 + i * 17, -76 + (i % 2) * 2, 6, 13)
            sp = QPainterPath()
            sp.addRoundedRect(rect, 3, 3)
            p.drawPath(sp)

        self._leg(p, -22, -36, 0.0, FUR, width=13)
        self._leg(p, 30, -38, math.pi, FUR, width=13)

        d2 = DrawParams(**{**d.__dict__})
        d2.head_tilt = 34 + bobble * 6
        d2.head_dx = d2.head_dy = 0.0
        d2.mouth = "munch" if bobble > 0.2 else "smile"
        d2.eye_happy = 1.0
        self._collar(p, 44, -52, 42)
        self._head(p, d2, 58, -58 + bobble * 3.0, r=44)

    def _stretch(self, p, d: DrawParams):
        """Classic wake-up stretch: butt up, chest low, front legs forward."""
        br = 1.0 + 0.02 * math.sin(d.breath * 2 * math.pi)
        wag = math.sin(d.tail_wag)

        path, tip = self._tail_path(-40, -84, 0.9, wag, curl_front=False)
        self._draw_tail(p, path, tip, width=13)

        p.setBrush(QColor(FUR_DARK))
        lp = QPainterPath()
        lp.addRoundedRect(QRectF(-50, -46, 13, 46), 6.5, 6.5)
        p.drawPath(lp)
        p.setBrush(FUR)
        lp = QPainterPath()
        lp.addRoundedRect(QRectF(-34, -48, 14, 48), 7, 7)
        p.drawPath(lp)

        body = QPainterPath()
        body.setFillRule(Qt.FillRule.WindingFill)
        body.addEllipse(QPointF(-32, -74 * br), 34, 30 * br)
        body.addEllipse(QPointF(-2, -52), 38, 26)
        body.addEllipse(QPointF(26, -32), 32, 20)
        p.setBrush(FUR)
        p.drawPath(body.simplified())
        p.setBrush(STRIPE)
        for sx, sy, rot in ((-38, -103, 6), (-18, -94, 22), (0, -80, 34)):
            p.save()
            p.translate(sx, sy)
            p.rotate(rot)
            sp = QPainterPath()
            sp.addRoundedRect(QRectF(-3, 0, 6, 12), 3, 3)
            p.drawPath(sp)
            p.restore()

        p.setBrush(QColor(FUR_DARK))
        lp = QPainterPath()
        lp.addRoundedRect(QRectF(26, -12, 46, 11), 5.5, 5.5)
        p.drawPath(lp)
        p.setBrush(FUR)
        lp = QPainterPath()
        lp.addRoundedRect(QRectF(32, -14, 50, 12), 6, 6)
        p.drawPath(lp)

        d2 = DrawParams(**{**d.__dict__})
        d2.head_tilt = -10
        d2.head_dx = d2.head_dy = 0.0
        self._collar(p, 42, -48, 44)
        self._head(p, d2, 62, -68, r=44)

    def _drag(self, p, d: DrawParams):
        """Held by the scruff — body hangs and stretches, legs dangle."""
        sway = math.sin(d.dangle) * 7
        p.save()
        p.rotate(sway * 0.35)

        pen = QPen(FUR, 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(10, -74))
        path.cubicTo(QPointF(26 + sway, -52), QPointF(20 + sway * 1.5, -30),
                     QPointF(10 + sway * 2, -12))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(STRIPE)
        p.drawEllipse(QPointF(10 + sway * 2, -12), 6.5, 6.5)

        body = QPainterPath()
        body.setFillRule(Qt.FillRule.WindingFill)
        body.addEllipse(QPointF(0, -96), 38, 52)
        body.addEllipse(QPointF(0, -70), 34, 34)
        p.setBrush(FUR)
        p.drawPath(body.simplified())

        for i, lx in enumerate((-16, 16)):
            p.save()
            p.translate(lx, -56)
            p.rotate(sway * (1.2 if i == 0 else 1.5))
            p.setBrush(FUR)
            lp = QPainterPath()
            lp.addRoundedRect(QRectF(-7, -4, 14, 40), 7, 7)
            p.drawPath(lp)
            p.restore()

        d2 = DrawParams(**{**d.__dict__})
        d2.ear_back = max(0.55, d.ear_back)
        d2.eye_big = 0.8
        d2.eye_happy = 0.0
        d2.eye_open = 1.0
        d2.mouth = "o"
        self._head(p, d2, 0, -150, r=50)
        self._collar(p, 0, -116, 54)
        p.restore()


class Particle:
    __slots__ = ("kind", "x", "y", "vx", "vy", "age", "life", "s")

    def __init__(self, kind, x, y, vx, vy, life, s=1.0):
        self.kind, self.x, self.y = kind, x, y
        self.vx, self.vy = vx, vy
        self.age, self.life, self.s = 0.0, life, s


def draw_heart(p: QPainter, x, y, s, color):
    path = QPainterPath(QPointF(x, y + 3.2 * s))
    path.cubicTo(QPointF(x - 5.4 * s, y - 1.4 * s), QPointF(x - 3.4 * s, y - 5.4 * s),
                 QPointF(x, y - 2.2 * s))
    path.cubicTo(QPointF(x + 3.4 * s, y - 5.4 * s), QPointF(x + 5.4 * s, y - 1.4 * s),
                 QPointF(x, y + 3.2 * s))
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(path)


def draw_fish(p: QPainter, x, y, s, color):
    body = QPainterPath()
    body.addEllipse(QPointF(x - 2 * s, y), 7.5 * s, 4.6 * s)
    tail = QPainterPath(QPointF(x + 4.6 * s, y))
    tail.lineTo(QPointF(x + 9.4 * s, y - 4 * s))
    tail.lineTo(QPointF(x + 9.4 * s, y + 4 * s))
    tail.closeSubpath()
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(body)
    p.drawPath(tail)
    p.setBrush(QColor(255, 255, 255, 220))
    p.drawEllipse(QPointF(x - 6 * s, y - 1.2 * s), 1.1 * s, 1.1 * s)


def draw_sparkle(p: QPainter, x, y, s, color):
    path = QPainterPath(QPointF(x, y - 5 * s))
    path.quadTo(QPointF(x + 1.2 * s, y - 1.2 * s), QPointF(x + 5 * s, y))
    path.quadTo(QPointF(x + 1.2 * s, y + 1.2 * s), QPointF(x, y + 5 * s))
    path.quadTo(QPointF(x - 1.2 * s, y + 1.2 * s), QPointF(x - 5 * s, y))
    path.quadTo(QPointF(x - 1.2 * s, y - 1.2 * s), QPointF(x, y - 5 * s))
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(path)


def _write_wav(path, samples, sr=22050):
    import struct
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(
            struct.pack("<h", int(clamp(s, -1.0, 1.0) * 32767))
            for s in samples))


def _gen_purr(sr):
    """Purr as a rolled low trill: harmonics of the 24 Hz purr rate
    (48/96/144/192 Hz — audible on laptop speakers, unlike the real ~25 Hz
    fundamental) amplitude-rolled at 24 Hz, plus a whisper of triple-lowpassed
    breath noise. Chopping broadband noise instead IS the helicopter sound.
    All periodic parts fit the 1.5 s loop exactly; the noise seam is
    crossfaded tail-into-head so infinite looping is seamless."""
    rng = random.Random(3)
    n, fade = int(sr * 1.5), int(sr * 0.05)
    lp1 = lp2 = lp3 = 0.0
    raw = []
    for i in range(n + fade):
        t = i / sr
        roll = 0.62 + 0.38 * math.sin(2 * math.pi * 24 * t)
        breath = 0.80 + 0.20 * math.sin(2 * math.pi * t / 0.75)
        rumble = (0.45 * math.sin(2 * math.pi * 48 * t)
                  + math.sin(2 * math.pi * 96 * t)
                  + 0.45 * math.sin(2 * math.pi * 144 * t)
                  + 0.18 * math.sin(2 * math.pi * 192 * t))
        lp1 += 0.055 * (rng.uniform(-1, 1) - lp1)
        lp2 += 0.055 * (lp1 - lp2)
        lp3 += 0.055 * (lp2 - lp3)
        noise = lp3 * 6.0 * (0.75 + 0.25 * math.sin(2 * math.pi * 24 * t))
        raw.append((0.30 * rumble * roll + 0.10 * noise) * breath)
    for i in range(fade):
        k = i / fade
        raw[i] = raw[i] * k + raw[n + i] * (1 - k)
    return raw[:n]


def _gen_chirp(sr):
    """Quick rising trill — the little 'brrp?' at a butterfly."""
    dur = 0.32
    out, phase = [], 0.0
    for i in range(int(sr * dur)):
        t = i / sr
        f = 620 + 900 * t / dur + 140 * math.sin(2 * math.pi * 27 * t)
        phase += 2 * math.pi * f / sr
        env = math.sin(math.pi * t / dur) ** 0.7
        out.append((math.sin(phase) + 0.35 * math.sin(2 * phase)) * env * 0.5)
    return out


def _gen_meow(sr):
    """One soft greeting meow: pitch rises then falls, brightness fades
    across the vowel so it reads 'mee-ow' rather than a beep."""
    dur = 0.55
    out, phase = [], 0.0
    for i in range(int(sr * dur)):
        t = i / sr
        x = t / dur
        f = 430 + 320 * math.sin(math.pi * min(x * 1.15, 1.0))
        phase += 2 * math.pi * f / sr
        bright = 0.15 + 0.55 * (1 - x)
        env = clamp(x / 0.08, 0, 1) * clamp((1 - x) / 0.25, 0, 1)
        out.append((math.sin(phase) + bright * math.sin(2 * phase)
                    + 0.3 * bright * math.sin(3 * phase)) * env * 0.42)
    return out


def ensure_sounds() -> dict:
    """Synthesize the three WAVs into sounds/ if missing; returns paths."""
    os.makedirs(SOUND_DIR, exist_ok=True)
    paths = {}
    for name, gen in (("purr", _gen_purr), ("chirp", _gen_chirp),
                      ("meow", _gen_meow)):
        path = os.path.join(SOUND_DIR, name + ".wav")
        if not os.path.exists(path):
            _write_wav(path, gen(22050))
        paths[name] = path
    return paths


def recycle(paths: list[str]) -> bool:
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p),
                    ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p)]
    src = "\0".join(os.path.abspath(q) for q in paths) + "\0\0"
    buf = ctypes.create_unicode_buffer(src, len(src) + 1)
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3                       # FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
    op.fFlags = 0x40 | 0x10 | 0x4      # ALLOWUNDO | NOCONFIRMATION | SILENT
    try:
        return (ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0
                and not op.fAnyOperationsAborted)
    except Exception:
        return False


def _hwnd_rect_dip(hwnd, dpr):
    rect = ctypes.wintypes.RECT()
    try:
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                ctypes.wintypes.HWND(hwnd), 9,   # DWMWA_EXTENDED_FRAME_BOUNDS
                ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            if not ctypes.windll.user32.GetWindowRect(
                    ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)):
                return None
    except Exception:
        return None
    return (rect.left / dpr, rect.top / dpr,
            rect.right / dpr, rect.bottom / dpr)


def _hwnd_alive(hwnd):
    u = ctypes.windll.user32
    try:
        if not (u.IsWindow(hwnd) and u.IsWindowVisible(hwnd)) \
                or u.IsIconic(hwnd):
            return False
        cloaked = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), 14,      # DWMWA_CLOAKED
            ctypes.byref(cloaked), 4)
        return cloaked.value == 0
    except Exception:
        return False


def find_perch(geo, taskbar_y, dpr, exclude_hwnd, allow_fg=True):
    """Best window ledge right now: the foreground window if suitable,
    else the top-most suitable one. Returns dict or None. allow_fg=False
    keeps the window you're using off-limits — napping on your active
    window is a privilege he earns at high bond."""
    u = ctypes.windll.user32
    fg = u.GetForegroundWindow()

    def consider(hwnd):
        if not hwnd or hwnd == exclude_hwnd or not _hwnd_alive(hwnd):
            return None
        if not allow_fg and hwnd == fg:
            return None
        if u.GetWindowTextLengthW(ctypes.wintypes.HWND(hwnd)) == 0:
            return None
        r = _hwnd_rect_dip(hwnd, dpr)
        if r is None:
            return None
        left, top, right, _bottom = r
        lo = max(left + 40, geo.left() + 60)
        hi = min(right - 40, geo.right() - 60)
        if (hi - lo < 240 or top < geo.top() + 170
                or top > taskbar_y - 220):
            return None
        return {"hwnd": hwnd, "left": left, "right": right, "top": top,
                "lo": lo, "hi": hi}

    best = consider(fg) if allow_fg else None
    if best:
        return best
    found = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND,
                               ctypes.wintypes.LPARAM)

    def cb(hwnd, _):
        c = consider(hwnd)
        if c:
            found.append(c)
            return False         # EnumWindows goes top-down; first hit wins
        return True

    try:
        u.EnumWindows(proto(cb), 0)
    except Exception:
        pass
    return found[0] if found else None


class CatWidget(QWidget):
    W, H = 380, 320
    GROUND = 306

    def __init__(self, state: PetState):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.setWindowTitle("Mochi")
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.resize(self.W, self.H)

        self.st = state
        self.emo = EmotionEngine(state)
        self.brain = Brain(state, self.emo)
        self.painter_cat = CatPainter()
        self.rng = random.Random()

        self.d = DrawParams()
        self.d.weight = state.weight

        self._eye_open_t = 1.0
        self._blink_at = time.time() + self.rng.uniform(2, 6)
        self._ear_twitch_at = time.time() + self.rng.uniform(4, 12)
        self._t = 0.0
        self._last_anim = time.time()
        self._last_sys = time.time()
        self._transition = 0.0
        self._last_pose = "sit"

        self.particles: list[Particle] = []
        self.bubble = None
        self.bubble_next_zzz = 0.0

        self.screen_geo = QApplication.primaryScreen().availableGeometry()
        span_lo = self.screen_geo.left() + 120
        span_hi = self.screen_geo.right() - 120
        if self.st.x < 0:
            self.st.x = (span_lo + span_hi) / 2
        self.world_x = clamp(self.st.x, span_lo, span_hi)
        self.vx = 0.0
        self.facing = 1

        self.dragging = False
        self.drag_off = QPoint()
        self.press_pos = None
        self.press_t = 0.0
        self.stroke_accum = 0.0
        self.petting = False
        self.fall_v = 0.0
        self.play_until = 0.0
        self.eat_food = None
        self.eat_progress = 0.0
        self.bowl_side = 1
        self.gift_drop_x = None
        self.gift_item = None
        self.cursor_hist = []
        self._cursor_active_until = 0.0
        self.away_greet = False
        self.pouncing = False
        self.pounce_vx = 0.0
        self.wiggle_t = 0.0
        self.swat_t = 0.0
        self._swat_ok_at = 0.0
        self.cur_vel = 0.0
        self._cur_prev = None
        self.butterfly = None
        self.tray = None
        self._notified = {}
        self._drool = False
        self._moved = False
        self._walk_phase = 0.0
        self._breath_phase = 0.0
        self._tray_emo = None
        self.call_x = None

        # sounds/{name}*.wav are variants of one sound (meow.wav, meow2.wav…);
        # _play picks one at random so he never repeats himself exactly
        self.sounds = {}
        self._purr_level = 0.0
        if QSoundEffect is not None:
            try:
                ensure_sounds()
                for name in SOUND_VOL:
                    fx = []
                    for fn in sorted(os.listdir(SOUND_DIR)):
                        if fn.startswith(name) and fn.endswith(".wav"):
                            s = QSoundEffect(self)
                            s.setSource(QUrl.fromLocalFile(
                                os.path.join(SOUND_DIR, fn)))
                            s.setVolume(SOUND_VOL[name])
                            if name == "purr":
                                # setLoopCount only takes int; the enum TypeErrors
                                s.setLoopCount(
                                    int(QSoundEffect.Loop.Infinite.value))
                            fx.append(s)
                    if fx:
                        self.sounds[name] = fx
            except Exception:
                self.sounds = {}

        self.taskbar_y = float(self.screen_geo.bottom() + 1)
        self.ground_y = self.taskbar_y
        self.perch = None
        self.perch_started = 0.0
        self.perch_target = None
        self.air = None
        self.pending_food = None
        self._dpr = QApplication.primaryScreen().devicePixelRatio() or 1.0

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._anim_tick)
        self.anim_timer.start(16)
        self.sys_timer = QTimer(self)
        self.sys_timer.timeout.connect(self._sys_tick)
        self.sys_timer.start(1000)
        self.save_timer = QTimer(self)
        self.save_timer.timeout.connect(self.st.save)
        self.save_timer.start(45000)

        self._place()

    def _place(self):
        self.move(int(self.world_x - self.W / 2),
                  int(self.ground_y - self.GROUND))

    def _span(self, margin=120):
        """Walkable x range on the current floor (taskbar or ledge)."""
        if self.perch is not None:
            return (max(self.perch["left"] + 40, self.screen_geo.left() + 60),
                    min(self.perch["right"] - 40, self.screen_geo.right() - 60))
        return (self.screen_geo.left() + margin,
                self.screen_geo.right() - margin)

    def _set_perch_mask(self, on: bool):
        """While perched, shrink the interactive window region to the cat —
        a translucent Qt window still eats clicks in its transparent areas,
        and up on a ledge that would block the host window's title bar."""
        if on:
            self.setMask(QRegion(int(self.W / 2 - 115), 60, 230,
                                 self.GROUND - 60 + 12))
        else:
            self.clearMask()

    def _covered_by_normal_window(self, u) -> bool:
        """Probe the point at his belly: if a NON-topmost window of another
        process is drawn there, Win11 desynced the z-bands — our ex-style
        still says topmost while the actual order says otherwise, so the
        flag check alone misses it. Topmost coverers (toasts, menus, other
        on-top apps) are legitimate and left alone."""
        pt = ctypes.wintypes.POINT(
            int((self.x() + self.W / 2) * self._dpr),
            int((self.y() + self.GROUND - 40) * self._dpr))
        u.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
        u.WindowFromPoint.restype = ctypes.wintypes.HWND
        u.GetAncestor.restype = ctypes.wintypes.HWND
        h = u.WindowFromPoint(pt)
        if not h:
            return False
        root = u.GetAncestor(h, 2)               # GA_ROOT
        if not root or root == int(self.winId()):
            return False
        pid = ctypes.wintypes.DWORD()
        u.GetWindowThreadProcessId(ctypes.wintypes.HWND(root),
                                   ctypes.byref(pid))
        if pid.value == os.getpid():
            return False                          # our own menu / stats popup
        cloaked = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(root), 14,       # DWMWA_CLOAKED
            ctypes.byref(cloaked), 4)
        if cloaked.value:
            return False                          # not actually drawn there
        return not (u.GetWindowLongW(ctypes.wintypes.HWND(root), -20) & 0x8)

    # NOTE: an auto-hide-during-fullscreen feature lived here briefly and was
    # removed on purpose: with an auto-hidden taskbar the work area equals the
    # monitor, so maximized windows, Alt-Tab overlays and shell hosts are
    # geometrically indistinguishable from real fullscreen apps — the cat
    # kept vanishing during normal use. He stays visible, always.

    def _sys_tick(self):
        now = time.time()
        dt = clamp(now - self._last_sys, 0.2, 5.0)
        self._last_sys = now
        st = self.st
        hour = time.localtime().tm_hour + time.localtime().tm_min / 60.0

        geo = QApplication.primaryScreen().availableGeometry()
        self._dpr = QApplication.primaryScreen().devicePixelRatio() or 1.0
        if geo != self.screen_geo:
            self.screen_geo = geo
            self.taskbar_y = float(geo.bottom() + 1)
            if self.perch is None and self.air is None:
                self.ground_y = self.taskbar_y
            self.world_x = clamp(self.world_x, geo.left() + 100, geo.right() - 100)
            if not self.dragging:
                self._place()

        if self.isVisible():
            try:
                u = ctypes.windll.user32
                hwnd = ctypes.wintypes.HWND(int(self.winId()))
                flags = 0x0010 | 0x0002 | 0x0001  # NOACTIVATE | NOMOVE | NOSIZE
                # Win11 breaks topmost two ways: it strips WS_EX_TOPMOST
                # silently, or desyncs the z-bands so the flag still reads
                # topmost while normal windows sit above us. Both look like
                # "the cat went behind my windows", both are invisible to a
                # plain TOPMOST re-assert (a no-op when the flag is set) —
                # only the demote-then-promote pair repairs them.
                broken = (not (u.GetWindowLongW(hwnd, -20) & 0x8)
                          or (not self.dragging
                              and self._covered_by_normal_window(u)))
                if broken:
                    u.SetWindowPos(hwnd, ctypes.wintypes.HWND(-2),
                                   0, 0, 0, 0, flags)
                u.SetWindowPos(hwnd, ctypes.wintypes.HWND(-1),
                               0, 0, 0, 0, flags)
            except Exception:
                pass

        if self.perch is None and self.air is None and not self.dragging:
            if self.brain.action == GO_PERCH:
                t = self.perch_target
                r = (_hwnd_rect_dip(t["hwnd"], self._dpr)
                     if t is not None and _hwnd_alive(t["hwnd"]) else None)
                if r is None:
                    cand = find_perch(self.screen_geo, self.taskbar_y,
                                      self._dpr, int(self.winId()),
                                      allow_fg=st.bond >= 60)
                    if cand is None:
                        self.perch_target = None
                        self.brain.set_action(IDLE_SIT, 2.0)
                    else:
                        self.perch_target = {
                            "hwnd": cand["hwnd"], "top": cand["top"],
                            "x": self.rng.uniform(cand["lo"], cand["hi"])}
                else:
                    t["top"] = r[1]
                    t["x"] = clamp(t["x"], r[0] + 60, r[2] - 60)
            else:
                cand = find_perch(self.screen_geo, self.taskbar_y,
                                  self._dpr, int(self.winId()),
                                  allow_fg=st.bond >= 60)
                self.brain.can_perch = cand is not None
        else:
            self.brain.can_perch = False
        self.brain.on_perch = self.perch is not None
        self.brain.perch_minutes = ((now - self.perch_started) / 60.0
                                    if self.perch is not None else 0.0)

        tick_needs(st, dt, self.brain.action)
        burn = 0.004 if self.brain.action in (ZOOMIES, PLAY, WALK) else 0.0008
        st.weight = clamp(st.weight + (1.0 - st.weight) * 0.0004 * dt - burn * dt / 60, 0.85, 1.30)

        self.emo.update(dt, hour)

        if self.brain.action == EAT and self.eat_food:
            h, f, wgain, fav = FOODS[self.eat_food]
            self.eat_progress += dt / 7.0
            st.hunger = clamp(st.hunger + h * dt / 7.0, 0, 100)
            if self.eat_progress >= 1.0:
                st.fun = clamp(st.fun + f, 0, 100)
                st.weight = clamp(st.weight + wgain * (1.5 if st.hunger > 80 else 1.0), 0.85, 1.30)
                self.emo.event(0.42 if fav else 0.3, 0.08)
                self._bond_up(0.8 if fav else 0.5)
                self.eat_food = None
                self._bubble("heart", 2.2)
                self._spawn_hearts(2)
                self.brain.set_action(GROOM if self.rng.random() < 0.5 else IDLE_SIT)
        if self.brain.action == PLAY and now > self.play_until:
            self.brain.set_action(IDLE_SIT)
            self.emo.event(0.25, -0.1)
            st.fun = clamp(st.fun + 18, 0, 100)
            self._bond_up(0.9)

        cur = QCursor.pos()
        self.cursor_hist.append((now, cur.x(), cur.y()))
        self.cursor_hist = [(t, x, y) for (t, x, y) in self.cursor_hist if now - t < 3.0]
        moved = 0.0
        if len(self.cursor_hist) >= 2:
            (_, x0, y0), (_, x1, y1) = self.cursor_hist[0], self.cursor_hist[-1]
            moved = math.hypot(x1 - x0, y1 - y0)
        # smoothed: brief mouse pauses shouldn't strobe the WATCH utility
        if moved > 60:
            self._cursor_active_until = now + 5.0
        cursor_active = now < self._cursor_active_until
        cursor_near = abs(cur.x() - self.world_x) < 420 and \
            abs(cur.y() - self.screen_geo.bottom()) < 500

        if (self.butterfly is None and 7 <= hour <= 20
                and self.brain.action != SLEEP and not self.dragging
                and self.perch is None and self.air is None
                and self.isVisible()
                and self.rng.random() < 0.0011 * dt):
            bx = clamp(self.world_x + self.rng.uniform(-260, 260),
                       self.screen_geo.left() + 140,
                       self.screen_geo.right() - 140)
            by = self.GROUND - self.rng.uniform(70, 130)
            self.butterfly = {"x": bx, "y": by, "base_y": by,
                              "t": self.rng.uniform(0, 6.28),
                              "until": now + self.rng.uniform(18, 35),
                              "fleeing": False,
                              "drift": self.rng.uniform(-14, 14)}
            self._play("chirp")
        self.brain.chase_x = (self.butterfly["x"]
                              if self.butterfly and self.perch is None else None)

        if not self.dragging:
            self.brain.choose(hour, cursor_active and cursor_near, cursor_near)
        self.brain.action_t += dt

        a = self.brain.action
        if a == BEG and self.rng.random() < 0.28:
            self._bubble("fish", 2.4)
        if a == SLEEP and now > self.bubble_next_zzz:
            self._spawn(Particle("zzz", self.W / 2 - 34 * self.facing,
                                 self.GROUND - 70, 6, -14,
                                 2.8, self.rng.uniform(0.8, 1.3)))
            self.bubble_next_zzz = now + self.rng.uniform(1.4, 2.4)
        if a == GIFT and self.gift_drop_x is None:
            self.gift_drop_x = clamp(cur.x(), self.screen_geo.left() + 140,
                                     self.screen_geo.right() - 140)

        if self.tray is not None:
            for key, low, msg in (
                    ("hunger", 18, "is hungry — right-click him to feed"),
                    ("fun", 12, "is bored — how about a play session?"),
                    ("clean", 12, "could really use a brush")):
                if getattr(st, key) < low and now - self._notified.get(key, 0) > 2700:
                    self._notified[key] = now
                    self.tray.showMessage(
                        st.name, f"{st.name} {msg}",
                        QSystemTrayIcon.MessageIcon.Information, 4000)

        if self.tray is not None:
            mood = "sleepy" if self.brain.action == SLEEP else self.emo.emotion()
            if mood != self._tray_emo:
                self._tray_emo = mood
                self.tray.setIcon(make_tray_icon(mood))

        st.x = self.world_x

    def _anim_tick(self):
        now = time.time()
        dt = clamp(now - self._last_anim, 0.001, 0.1)
        self._last_anim = now
        self._t += dt
        st, d = self.st, self.d
        a = self.brain.action
        emotion = self.emo.emotion()

        gp = QCursor.pos()
        if self._cur_prev is not None:
            v = math.hypot(gp.x() - self._cur_prev.x(),
                           gp.y() - self._cur_prev.y()) / max(dt, 1e-3)
            self.cur_vel = approach(self.cur_vel, v, 8, dt)
        self._cur_prev = gp

        b = self.butterfly
        if b is not None:
            b["t"] += dt
            if b["fleeing"]:
                b["base_y"] -= 130 * dt
                b["x"] += b["drift"] * 6 * dt
            else:
                b["x"] += (b["drift"] + math.sin(b["t"] * 0.7) * 26) * dt
                if now > b["until"]:
                    b["fleeing"] = True
            b["x"] = clamp(b["x"], self.screen_geo.left() + 60,
                           self.screen_geo.right() - 60)
            b["y"] = b["base_y"] + math.sin(b["t"] * 2.3) * 14
            if b["base_y"] < self.GROUND - 300:
                self.butterfly = None
                if self.brain.action == CHASE:
                    self.brain.set_action(IDLE_SIT, 1.5)

        self._moved = False
        if self.air is not None and not self.dragging:
            ar = self.air
            ar["v"] += 1500 * dt
            self.ground_y += ar["v"] * dt
            self._moved = True
            if ar.get("perch") and ar["v"] < 0 and self.ground_y <= ar["to_y"]:
                self.ground_y = ar["to_y"]
                self._land_perch()
            elif ar["v"] >= 0 and self.ground_y >= ar["to_y"]:
                self.ground_y = ar["to_y"]
                self._land_floor(ar)
            self._place()

        if self.perch is not None and self.air is None and not self.dragging:
            pr = self.perch
            r = (_hwnd_rect_dip(pr["hwnd"], self._dpr)
                 if _hwnd_alive(pr["hwnd"]) else None)
            ok = r is not None
            if ok:
                left, top, right, _b = r
                usable = (min(right - 40, self.screen_geo.right() - 60)
                          - max(left + 40, self.screen_geo.left() + 60))
                # looser than find_perch so small window moves don't eject him
                ok = (usable >= 160 and top >= self.screen_geo.top() + 150
                      and top <= self.taskbar_y - 120)
            if not ok:
                self.perch = None
                self._set_perch_mask(False)
                self.air = {"v": -80.0, "to_y": self.taskbar_y, "scared": True}
                self.brain.set_action(HOP_DOWN)
            elif (left, top, right) != (pr["left"], pr["top"], pr["right"]):
                self.world_x += left - pr["left"]
                pr["left"], pr["top"], pr["right"] = left, top, right
                self.ground_y = top
                self.world_x = clamp(self.world_x, left + 40, right - 40)
                self._place()

        if a == HOP_DOWN and self.air is None and not self.dragging:
            self.perch = None
            self._set_perch_mask(False)
            self.air = {"v": -240.0, "to_y": self.taskbar_y, "scared": False}

        speed = 0.0
        if a in (WALK, PLAY, ZOOMIES, GIFT, CHASE, GO_PERCH, CALLED) \
                and not self.dragging:
            if a == WALK:
                if self.brain.walk_target is None or abs(self.brain.walk_target - self.world_x) < 12:
                    lo, hi = self._span(130)
                    self.brain.walk_target = self.rng.uniform(lo, hi)
                speed = 62
                target = self.brain.walk_target
            elif a == ZOOMIES:
                if self.brain.walk_target is None or abs(self.brain.walk_target - self.world_x) < 30:
                    lo, hi = self._span(130)
                    self.brain.walk_target = self.rng.uniform(lo, hi)
                speed = 300
                target = self.brain.walk_target
            elif a == PLAY:
                target = clamp(QCursor.pos().x(),
                               self.screen_geo.left() + 120,
                               self.screen_geo.right() - 120)
                dxp = target - self.world_x
                speed = 0
                if self.wiggle_t > 0:
                    self.facing = 1 if dxp > 0 else -1
                    self.wiggle_t -= dt
                    if self.wiggle_t <= 0:
                        self._pounce(vx=clamp(dxp * 2.4, -280, 280))
                elif abs(dxp) < 85 and self.rng.random() < dt * 1.4:
                    self.wiggle_t = self.rng.uniform(0.35, 0.65)
                elif self.cur_vel > 900 and abs(dxp) < 260:
                    speed = 210
                else:
                    speed = 160 if abs(dxp) > 60 else 0
            elif a == CHASE:
                bfly = self.butterfly
                target, speed = self.world_x, 0
                if bfly is not None:
                    target = clamp(bfly["x"], self.screen_geo.left() + 120,
                                   self.screen_geo.right() - 120)
                    dxp = target - self.world_x
                    speed = 130 if abs(dxp) > 30 else 0
                    if (abs(dxp) < 60 and bfly["y"] > self.GROUND - 130
                            and self.rng.random() < dt * 1.2):
                        self._pounce(vx=clamp(dxp * 2.0, -240, 240))
            elif a == GO_PERCH:
                t = self.perch_target
                target, speed = self.world_x, 0
                if t is not None:
                    target = t["x"]
                    speed = 95
                    if self.air is None and abs(target - self.world_x) < 10:
                        dh = max(60.0, self.ground_y - t["top"])
                        self.air = {"v": -math.sqrt(2 * 1500 * (dh + 50)),
                                    "to_y": t["top"], "perch": True,
                                    "scared": False}
                        self._transition = 1.0
                        speed = 0
            elif a == CALLED:
                target = self.call_x if self.call_x is not None else self.world_x
                speed = 120
                if abs(target - self.world_x) < 16:
                    self.call_x = None
                    st.social = clamp(st.social + 6, 0, 100)
                    self._bond_up(0.6)
                    self.emo.event(0.15, 0.1)
                    self._spawn_hearts(2)
                    self._bubble("note", 1.8)
                    self._play("chirp")
                    self.brain.set_action(IDLE_SIT, 3.0)
            else:
                target = self.gift_drop_x or self.world_x
                speed = 85
                if abs(target - self.world_x) < 14:
                    self._deliver_gift()
            dx = target - self.world_x
            if abs(dx) > 4 and speed > 0:
                self.facing = 1 if dx > 0 else -1
                self.world_x += clamp(dx, -speed * dt, speed * dt)
                self._moved = True
                self._place()
        elif a == EAT:
            self.facing = self.bowl_side

        if a == FALLING:
            self.fall_v += 1500 * dt
            d.y_off += self.fall_v * dt
            if self.pounce_vx:
                self.world_x = clamp(self.world_x + self.pounce_vx * dt,
                                     self.screen_geo.left() + 100,
                                     self.screen_geo.right() - 100)
                self.pounce_vx *= max(0.0, 1.0 - 1.5 * dt)
                self._moved = True
                self._place()
            if d.y_off >= 0:
                d.y_off = 0
                self.fall_v = 0
                self.pounce_vx = 0.0
                self._transition = 1.0
                self._spawn_dust()
                if self.pouncing:
                    bfly = self.butterfly
                    cur0 = QCursor.pos()
                    if bfly is not None and abs(bfly["x"] - self.world_x) < 70:
                        if self.rng.random() < 0.35:
                            self.butterfly = None
                            if st.fun < 80:
                                st.fun = min(80.0, st.fun + 22)
                            self.emo.event(0.30, 0.15)
                            self._spawn_sparkles(7)
                            self._spawn_hearts(2)
                            self._bubble("!", 2.2)
                        else:
                            bfly["fleeing"] = True
                    elif (now < self.play_until
                          and abs(cur0.x() - self.world_x) < 60
                          and abs(cur0.y() - self.screen_geo.bottom()) < 280):
                        st.fun = clamp(st.fun + 8, 0, 100)
                        self.emo.event(0.18, 0.10)
                        self._spawn_hearts(2)
                        self._bubble("heart", 1.6)
                elif st.brave < 0.5:
                    self.emo.event(-0.06, 0.15)
                self.pouncing = False
                if now < self.play_until:
                    self.brain.set_action(PLAY)
                else:
                    self.brain.set_action(IDLE_SIT, 2.0)

        pose = POSE_OF_ACTION[a]
        if a == FALLING and self.pouncing:
            pose = "walk"
        if self.air is not None:
            pose = "drag" if self.air.get("scared") else "walk"
        if pose != self._last_pose:
            self._transition = 1.0
            self._last_pose = pose
        self._transition = max(0.0, self._transition - dt * 4.5)
        d.pose = pose
        d.dir = self.facing
        d.weight = st.weight
        # integrate breath phase by dt — multiplying wall-time by a rate that
        # drifts with arousal makes the phase (and the head, which rides the
        # breathing body) jump every frame, worse the longer he's been up
        b_rate = 0.55 if a == SLEEP else 0.9 + 0.5 * max(0, self.emo.arousal)
        self._breath_phase = (self._breath_phase + dt * b_rate) % 1.0
        d.breath = self._breath_phase
        rate = 16 if a == ZOOMIES else 8 if a in (PLAY, CHASE) else 6.2
        if self._moved:
            self._walk_phase += dt * rate
        else:
            rest = round(self._walk_phase / math.pi) * math.pi
            self._walk_phase = approach(self._walk_phase, rest, 12, dt)
        d.walk_phase = self._walk_phase
        d.squash = 1.0 + 0.10 * math.sin(self._transition * math.pi)
        if self.wiggle_t > 0:
            if a == PLAY:
                d.squash = 1.0 - 0.05 * math.sin(self._t * 24)
            else:
                self.wiggle_t = 0.0
        d.groom_t = self._t
        d.dangle = self._t * 4.0

        cur = QCursor.pos()
        local = self.mapFromGlobal(cur)
        dx = local.x() - self.W / 2
        dy = local.y() - (self.GROUND - 120)

        if (self.swat_t <= 0 and now > self._swat_ok_at and not self.dragging
                and a in (WATCH, IDLE_SIT, LOAF) and math.hypot(dx, dy) < 110
                and self.cur_vel > 520):
            self.swat_t = 0.55
            self._swat_ok_at = now + self.rng.uniform(2.5, 6.0)
            self.facing = 1 if dx > 0 else -1
            st.fun = clamp(st.fun + 3, 0, 100)
            st.social = clamp(st.social + 1.5, 0, 100)
            self.emo.event(0.05, 0.12)
            if self.rng.random() < 0.4:
                self._bubble("!", 1.0)
        if self.swat_t > 0:
            self.swat_t -= dt
            d.pose = "beg"

        if self.butterfly is not None and a in (IDLE_SIT, LOAF, WATCH, CHASE):
            dx = self.butterfly["x"] - self.world_x
            dy = self.butterfly["y"] - (self.GROUND - 120)
        dist = math.hypot(dx, dy)
        look = a in (WATCH, BEG, PLAY, IDLE_SIT, LOAF, CHASE) and dist < 600
        if look:
            ang = math.atan2(dy, dx)
            d.pupil_dx = clamp(math.cos(ang) * 3.0 * self.facing, -3, 3)
            d.pupil_dy = clamp(math.sin(ang) * 2.4, -2.4, 2.8)
            d.head_dx = clamp(dx * 0.02, -7, 7) * self.facing
            d.head_dy = clamp(dy * 0.012, -5, 6)
            d.head_tilt = clamp(dx * 0.012 * self.facing, -6, 6)
        else:
            d.pupil_dx = approach(d.pupil_dx, 0, 6, dt)
            d.pupil_dy = approach(d.pupil_dy, 0, 6, dt)
            d.head_dx = approach(d.head_dx, 0, 4, dt)
            d.head_dy = approach(d.head_dy, 0, 4, dt)
            d.head_tilt = approach(d.head_tilt, 0, 4, dt)

        v, ar = self.emo.valence, self.emo.arousal
        d.blush = clamp(0.35 + v * 0.6, 0, 1)
        d.tail_lift = clamp(0.35 + ar * 0.5 + v * 0.2, 0.05, 1.0)
        wag_speed = (1.2 + 3.2 * max(0.0, ar)
                     + (2.0 if a in (PLAY, ZOOMIES, CHASE) else 0)
                     + (3.0 if self.wiggle_t > 0 else 0))
        d.tail_wag = (d.tail_wag + wag_speed * dt * 2) % (2 * math.pi)
        target_ear = clamp(0.0 + (0.55 if emotion in ("grumpy", "moody") else 0)
                           + (0.3 if emotion == "sad" else 0), 0, 1)
        if a == DRAGGED:
            target_ear = max(target_ear, 0.55 * (1 - st.brave))
        d.ear_back = approach(d.ear_back, target_ear, 5, dt)

        if a == SLEEP:
            self._eye_open_t = approach(self._eye_open_t, 0.0, 3, dt)
            d.eye_happy = 0.0
        else:
            sleepy_lid = 0.55 if emotion == "sleepy" else 1.0
            if now > self._blink_at:
                self._eye_open_t = 0.05
                self._blink_at = now + self.rng.uniform(2.2, 6.5)
            self._eye_open_t = approach(self._eye_open_t, sleepy_lid, 10, dt)
            d.eye_happy = 1.0 if (self.petting or a in (EAT, STRETCH)
                                  or (emotion == "content" and a in (LOAF, IDLE_SIT))) else 0.0
        d.eye_open = self._eye_open_t
        d.eye_big = clamp(0.25 + 0.6 * max(0, ar), 0, 1) if emotion in ("playful", "curious") else \
            (0.85 if a == DRAGGED else 0.0)
        if self.wiggle_t > 0 or a == CHASE:
            d.eye_big = max(d.eye_big, 0.8)

        if a == DRAGGED:
            d.mouth = "o"
        elif a == STRETCH and self.brain.action_t < 1.2:
            d.mouth = "o"
        elif emotion in ("grumpy", "moody"):
            d.mouth = "flat"
        elif emotion == "sad":
            d.mouth = "frown"
        else:
            d.mouth = "smile"
        if self._drool:
            d.mouth = "o"
            d.eye_big = max(d.eye_big, 0.9)
        if self.air is not None and self.air.get("scared"):
            d.mouth = "o"
            d.eye_big = max(d.eye_big, 0.85)
            d.ear_back = max(d.ear_back, 0.6)

        perk_t = 0.0
        if (a in (WATCH, BEG, CHASE, PLAY) or self.wiggle_t > 0
                or self.swat_t > 0 or self._drool):
            perk_t = 1.0
        elif emotion in ("curious", "playful"):
            perk_t = 0.7
        elif look and self.cur_vel > 300:
            perk_t = 0.55
        d.ear_perk = approach(d.ear_perk, perk_t, 6, dt)
        if now > self._ear_twitch_at:
            d.ear_twitch = 1.0
            d.ear_twitch_side = self.rng.choice((-1, 1))
            lo, hi = (3, 9) if a == SLEEP else (5, 16)
            self._ear_twitch_at = now + self.rng.uniform(lo, hi)
        d.ear_twitch = max(0.0, d.ear_twitch - dt * 6)

        alive = []
        for pt in self.particles:
            pt.age += dt
            if pt.age < pt.life:
                pt.x += pt.vx * dt
                pt.y += pt.vy * dt
                if pt.kind in ("heart", "zzz", "note"):
                    pt.vy -= 6 * dt
                    pt.x += math.sin(pt.age * 3 + pt.s * 9) * 12 * dt
                elif pt.kind == "scrap":
                    pt.vy += 160 * dt
                alive.append(pt)
        self.particles = alive

        if self.petting and self.rng.random() < dt * 2.2:
            self._spawn_hearts(1)

        if (a == EAT and self.eat_food == "Trash"
                and self.rng.random() < dt * 4):
            self._spawn(Particle("scrap",
                                 self.W / 2 + 78 * self.facing + self.rng.uniform(-12, 12),
                                 self.GROUND - 34,
                                 self.rng.uniform(-30, 30),
                                 -self.rng.uniform(25, 70),
                                 0.7, self.rng.uniform(0.7, 1.2)))

        fx = self.sounds.get("purr")
        if fx:
            purr = fx[0]
            target = 1.0 if (self.petting and not self.st.muted) else 0.0
            # spin up quickly under your hand, trail off slowly after —
            # a hard stop on mouse-release sounds like a switch, not a cat
            rate = 2.5 if target > self._purr_level else 0.8
            self._purr_level = approach(self._purr_level, target, rate, dt)
            if self._purr_level > 0.02:
                purr.setVolume(SOUND_VOL["purr"] * self._purr_level)
                if not purr.isPlaying():
                    purr.play()
            elif purr.isPlaying():
                purr.stop()

        # power discipline: while he sleeps/loafs undisturbed (zzz particles
        # are slow enough to survive it) or is hidden, ~12 fps is plenty
        calm = (a in (SLEEP, LOAF) and not self.dragging and not self.petting
                and self.air is None and self.butterfly is None
                and self.bubble is None and self.swat_t <= 0
                and self._transition <= 0
                and now >= self._cursor_active_until
                and all(pt.kind == "zzz" for pt in self.particles))
        want = 80 if calm else 16
        if self.anim_timer.interval() != want:
            self.anim_timer.setInterval(want)

        self.update()

    def _spawn(self, pt: Particle):
        if len(self.particles) < 80:
            self.particles.append(pt)

    def _spawn_hearts(self, n):
        for _ in range(n):
            self._spawn(Particle("heart",
                                 self.W / 2 + self.rng.uniform(-30, 30),
                                 self.GROUND - 150 + self.rng.uniform(-20, 10),
                                 self.rng.uniform(-8, 8), -34,
                                 self.rng.uniform(1.2, 1.9),
                                 self.rng.uniform(0.8, 1.4)))

    def _spawn_dust(self):
        for _ in range(6):
            self._spawn(Particle("dust",
                                 self.W / 2 + self.rng.uniform(-46, 46),
                                 self.GROUND - 6,
                                 self.rng.uniform(-30, 30), -self.rng.uniform(6, 20),
                                 0.55, self.rng.uniform(0.7, 1.2)))

    def _spawn_sparkles(self, n=6):
        for _ in range(n):
            self._spawn(Particle("sparkle",
                                 self.W / 2 + self.rng.uniform(-45, 45),
                                 self.GROUND - 90 + self.rng.uniform(-50, 30),
                                 self.rng.uniform(-10, 10), -self.rng.uniform(8, 22),
                                 0.9, self.rng.uniform(0.7, 1.3)))

    def _bubble(self, kind, secs):
        self.bubble = (kind, time.time() + secs)

    def _pounce(self, vx=0.0):
        self.fall_v = -420
        self.pounce_vx = vx
        self.pouncing = True
        self.d.y_off = -1
        self.brain.action = FALLING
        self.emo.event(0.08, 0.1)

    def _land_perch(self):
        t, self.perch_target = self.perch_target, None
        self.air = None
        r = (_hwnd_rect_dip(t["hwnd"], self._dpr)
             if t is not None and _hwnd_alive(t["hwnd"]) else None)
        if r is None:
            self.air = {"v": 0.0, "to_y": self.taskbar_y, "scared": True}
            self.brain.set_action(HOP_DOWN)
            return
        left, top, right, _b = r
        self.perch = {"hwnd": t["hwnd"], "left": left, "right": right,
                      "top": top}
        self.perch_started = time.time()
        self.ground_y = top
        self.world_x = clamp(self.world_x, left + 40, right - 40)
        self.butterfly = None
        self._set_perch_mask(True)
        self._transition = 1.0
        self.emo.event(0.06, 0.05)
        self.brain.set_action(IDLE_SIT, 2.5)
        self._place()

    def _land_floor(self, ar):
        self.air = None
        self.ground_y = self.taskbar_y
        self._transition = 1.0
        self._spawn_dust()
        if ar.get("scared"):
            self.emo.event(-0.06, 0.15)
        pf, self.pending_food = self.pending_food, None
        if pf == "Trash":
            self.eat_food = "Trash"
            self.eat_progress = 0.0
            self.brain.set_action(EAT)
        elif pf is not None:
            self.brain.set_action(IDLE_SIT, 0.5)
            self.feed(pf)
        elif self.call_x is not None:
            self.brain.set_action(CALLED)
        elif time.time() < self.play_until:
            self.brain.set_action(PLAY)
        else:
            self.brain.set_action(IDLE_SIT, 2.0)
        self._place()

    def _deliver_gift(self):
        kinds = ["leaf", "sock", "bug"]
        if self.st.bond >= 70:
            kinds.append("flower")
        self.gift_item = (self.world_x + 40 * self.facing,
                          self.rng.choice(kinds))
        self.gift_drop_x = None
        self.st.gifts += 1
        self._bubble("!", 2.5)
        self.brain.set_action(IDLE_SIT, 3.0)
        self.emo.event(0.2, 0.1)

    def feed(self, food: str):
        if self.dragging or self.brain.action in (DRAGGED, FALLING):
            return
        if self.st.hunger > 88:
            self._bubble("?", 2.2)
            if self.perch is None and self.air is None:
                self.brain.set_action(WALK, 3.0)
            return
        if self.perch is not None or self.air is not None:
            self.pending_food = food
            if self.air is None:
                self.brain.set_action(HOP_DOWN)
            return
        self.eat_food = food
        self.eat_progress = 0.0
        self.bowl_side = 1 if self.world_x < (self.screen_geo.center().x()) else -1
        self.brain.set_action(EAT)
        self.emo.event(0.15, 0.2)

    def start_play(self):
        if self.dragging or self.brain.action in (DRAGGED, FALLING):
            return
        self.play_until = time.time() + 25
        self.emo.event(0.2, 0.45)
        if self.perch is not None or self.air is not None:
            if self.air is None:
                self.brain.set_action(HOP_DOWN)
            return
        self.brain.set_action(PLAY)

    def _bond_up(self, amt: float):
        self.st.bond = clamp(self.st.bond + amt, 0, 100)

    def _play(self, name: str):
        fx = self.sounds.get(name)
        if fx and not self.st.muted and self.isVisible():
            s = self.rng.choice(fx)
            s.setVolume(SOUND_VOL[name] * self.rng.uniform(0.80, 1.10))
            s.play()

    def toggle_mute(self):
        self.st.muted = not self.st.muted

    def call_over(self):
        if self.dragging or self.brain.action in (DRAGGED, FALLING, EAT):
            return
        if self.st.bond < 40:
            # he heard you — he just doesn't care enough yet
            self._bubble("?", 2.2)
            self.d.ear_twitch = 1.0
            self.d.ear_twitch_side = self.rng.choice((-1, 1))
            return
        self.call_x = clamp(QCursor.pos().x(),
                            self.screen_geo.left() + 120,
                            self.screen_geo.right() - 120)
        if self.perch is not None or self.air is not None:
            if self.air is None:
                self.brain.set_action(HOP_DOWN)
            return
        self.brain.set_action(CALLED)

    def _eat_files(self, paths: list[str]):
        appdir = os.path.normpath(APP_DIR).lower() + os.sep
        safe = [q for q in paths
                if not (os.path.normpath(os.path.abspath(q)).lower() + os.sep)
                .startswith(appdir)]
        if not safe:
            self._bubble("?", 2.4)
            return
        if not recycle(safe):
            self._bubble("?", 2.4)
            self.emo.event(-0.05, 0.1)
            return
        self.st.files_eaten += len(safe)
        self._bond_up(0.3)
        self.emo.event(0.2, 0.15)
        if self.dragging or self.brain.action in (DRAGGED, FALLING):
            self._bubble("heart", 2.0)
            return
        if self.perch is not None or self.air is not None:
            self.pending_food = "Trash"
            if self.air is None:
                self.brain.set_action(HOP_DOWN)
            return
        self.eat_food = "Trash"
        self.eat_progress = 0.0
        self.brain.set_action(EAT)

    def dragEnterEvent(self, ev):
        md = ev.mimeData()
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            ev.acceptProposedAction()
            self._drool = True

    def dragLeaveEvent(self, ev):
        self._drool = False

    def dropEvent(self, ev):
        self._drool = False
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.bowl_side = 1 if ev.position().x() >= self.W / 2 else -1
            self._eat_files(paths)
        ev.acceptProposedAction()

    def brush(self):
        self.st.clean = clamp(self.st.clean + 40, 0, 100)
        self.st.social = clamp(self.st.social + 14, 0, 100)
        self._bond_up(0.5)
        self.emo.event(0.3, -0.1)
        self._spawn_sparkles(9)
        self._bubble("heart", 2.0)

    def _cat_hit(self, pos) -> bool:
        cx = self.W / 2
        return (abs(pos.x() - cx) < 85 and
                self.GROUND - 215 < pos.y() < self.GROUND + 6)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            if self.gift_item is not None:
                gx = self.gift_item[0] - (self.world_x - self.W / 2)
                if abs(ev.position().x() - gx) < 30 and ev.position().y() > self.GROUND - 40:
                    self.gift_item = None
                    self._bond_up(0.4)
                    self._spawn_hearts(3)
                    self._spawn_sparkles(6)
                    self.emo.event(0.15, 0.05)
                    return
            if self._cat_hit(ev.position()):
                self.press_pos = ev.globalPosition().toPoint()
                self.press_t = time.time()
                self.stroke_accum = 0.0
                self.drag_off = ev.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, ev):
        if self.press_pos is None:
            return
        gp = ev.globalPosition().toPoint()
        dx = gp.x() - self.press_pos.x()
        dy = gp.y() - self.press_pos.y()
        dist = math.hypot(dx, dy)
        if not self.dragging:
            if dy < -26 or dist > 60:
                self.dragging = True
                self.petting = False
                self.perch = None
                self.perch_target = None
                self.air = None
                self._set_perch_mask(False)
                self.brain.set_action(DRAGGED)
                self.emo.event(-0.05 * (1 - self.st.brave), 0.35)
            elif dist > 6:
                self.petting = True
                self.stroke_accum += dist
                self.press_pos = gp
                if self.stroke_accum > 55:
                    self.stroke_accum = 0
                    self.st.social = clamp(self.st.social + 4.5, 0, 100)
                    self._bond_up(0.12)
                    self.emo.event(0.07, -0.03)
                    self._spawn_hearts(1)
                    if self.rng.random() < 0.5:
                        self.d.ear_twitch = 1.0
                        self.d.ear_twitch_side = self.rng.choice((-1, 1))
        if self.dragging:
            np_ = gp - self.drag_off
            self.world_x = np_.x() + self.W / 2
            y = min(np_.y(), int(self.taskbar_y) - self.GROUND)
            self.move(np_.x(), y)
            # y_off feeds only the shadow fade — the window itself follows
            # the cursor, so translating the sprite too would clip it away
            self.d.y_off = y - (self.taskbar_y - self.GROUND)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self.dragging:
            self.dragging = False
            self.fall_v = 0.0
            self.d.y_off = 0.0
            self.world_x = clamp(self.x() + self.W / 2,
                                 self.screen_geo.left() + 100,
                                 self.screen_geo.right() - 100)
            self.ground_y = min(float(self.y() + self.GROUND), self.taskbar_y)
            self.air = {"v": 0.0, "to_y": self.taskbar_y,
                        "scared": self.st.brave < 0.5}
            self.brain.set_action(HOP_DOWN)
            self._place()
        elif self.press_pos is not None and self._cat_hit(ev.position()):
            if time.time() - self.press_t < 0.35 and not self.petting:
                self.st.social = clamp(self.st.social + 2.0, 0, 100)
                self._bond_up(0.05)
                self.emo.event(0.05, 0.05)
                self._spawn_hearts(1)
                self._bubble("note", 1.4)
        self.petting = False
        self.press_pos = None

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        # a full cat refusing with a silent "?" reads as a bug — grey the
        # food out instead and say why
        full = self.st.hunger > 88
        feed = menu.addMenu("Feed" + ("  (not hungry)" if full else ""))
        for name in FOODS:
            if name == "Trash":
                continue
            fav = " ♥" if FOODS[name][3] else ""
            act = QAction(f"{name}{fav}", menu,
                          triggered=lambda _=False, n=name: self.feed(n))
            act.setEnabled(not full)
            feed.addAction(act)
        menu.addAction(QAction("Play (chase your cursor)", menu, triggered=self.start_play))
        menu.addAction(QAction("Brush", menu, triggered=self.brush))
        if self.sounds:
            menu.addAction(QAction("Sounds", menu, checkable=True,
                                   checked=not self.st.muted,
                                   triggered=self.toggle_mute))
        menu.addSeparator()
        menu.addAction(QAction("Stats", menu, triggered=self.show_stats))
        menu.addAction(QAction("Rename…", menu, triggered=self.rename))
        auto = QAction("Start with Windows", menu, checkable=True,
                       checked=autostart_enabled(), triggered=toggle_autostart)
        menu.addAction(auto)
        menu.addSeparator()
        menu.addAction(QAction("Quit", menu, triggered=QApplication.quit))
        menu.exec(ev.globalPos())

    def rename(self):
        name, ok = QInputDialog.getText(None, "Rename", "Cat's name:",
                                        text=self.st.name)
        if ok and name.strip():
            self.st.name = name.strip()[:20]
            self.setWindowTitle(self.st.name)
            if self.tray is not None:
                self.tray.setToolTip(self.st.name)

    def show_stats(self):
        # keep a reference — a parentless QWidget is GC'd immediately otherwise
        self._stats_popup = StatsPopup(self)
        self._stats_popup.popup()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        scale = self.st.size * self.st.growth
        if self.gift_item is not None:
            gx = self.gift_item[0] - (self.world_x - self.W / 2)
            if -20 < gx < self.W + 20:
                self._draw_gift(p, gx, self.GROUND - 8, self.gift_item[1])

        if self.brain.action == EAT and self.eat_food:
            scale_b = self.st.size * self.st.growth
            self._draw_bowl(p, self.W / 2 + 100 * scale_b * self.bowl_side,
                            self.GROUND, self.eat_food, 1.0 - self.eat_progress)

        p.save()
        p.translate(self.W / 2, self.GROUND)
        self.painter_cat.draw(p, self.d, scale)
        p.restore()

        if self.butterfly is not None:
            b = self.butterfly
            bx = b["x"] - (self.world_x - self.W / 2)
            if -30 < bx < self.W + 30:
                self._draw_butterfly(p, bx, b["y"], b["t"])

        if self.brain.action == ZOOMIES:
            pen = QPen(QColor(150, 140, 130, 110), 3,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            for i in range(3):
                y = self.GROUND - 40 - i * 26
                x0 = self.W / 2 - self.facing * (95 + i * 14)
                p.drawLine(QPointF(x0, y), QPointF(x0 - self.facing * 26, y))
            p.setPen(Qt.PenStyle.NoPen)

        for pt in self.particles:
            k = clamp(1 - pt.age / pt.life, 0, 1)
            k *= clamp(pt.age / 0.15, 0, 1)
            if pt.kind == "heart":
                c = QColor(HEART)
                c.setAlpha(int(235 * k))
                draw_heart(p, pt.x, pt.y, pt.s * (0.8 + 0.4 * k), c)
            elif pt.kind == "zzz":
                c = QColor(ZZZ)
                c.setAlpha(int(220 * k))
                f = QFont("Segoe UI", int(10 + 5 * pt.s))
                f.setBold(True)
                p.setFont(f)
                p.setPen(c)
                p.drawText(QPointF(pt.x, pt.y), "z")
                p.setPen(Qt.PenStyle.NoPen)
            elif pt.kind == "sparkle":
                c = QColor(SPARKLE)
                c.setAlpha(int(230 * k))
                draw_sparkle(p, pt.x, pt.y, pt.s, c)
            elif pt.kind == "dust":
                c = QColor(180, 170, 160, int(120 * k))
                p.setBrush(c)
                p.drawEllipse(QPointF(pt.x, pt.y), 4 * pt.s * (1 + pt.age), 3 * pt.s)
            elif pt.kind == "note":
                c = QColor(150, 130, 160, int(220 * k))
                f = QFont("Segoe UI", 13)
                p.setFont(f)
                p.setPen(c)
                p.drawText(QPointF(pt.x, pt.y), "♪")
                p.setPen(Qt.PenStyle.NoPen)
            elif pt.kind == "scrap":
                p.setBrush(QColor(245, 242, 235, int(225 * k)))
                p.save()
                p.translate(pt.x, pt.y)
                p.rotate(pt.age * 300 * pt.s)
                p.drawRect(QRectF(-2.6 * pt.s, -2.6 * pt.s,
                                  5.2 * pt.s, 5.2 * pt.s))
                p.restore()

        if self.bubble and time.time() < self.bubble[1]:
            self._draw_bubble(p, self.bubble[0])
        elif self.bubble:
            self.bubble = None

    def _draw_bubble(self, p, kind):
        cx = self.W / 2 + 52
        cy = self.GROUND - 218
        r = QRectF(cx - 26, cy - 20, 52, 40)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        tail = QPainterPath(QPointF(cx - 12, cy + 18))
        tail.lineTo(QPointF(cx - 22, cy + 32))
        tail.lineTo(QPointF(cx + 2, cy + 19))
        tail.closeSubpath()
        p.setBrush(BUBBLE_BG)
        pen = QPen(BUBBLE_BRD, 2)
        p.setPen(pen)
        p.drawPath(path.united(tail).simplified())
        p.setPen(Qt.PenStyle.NoPen)
        if kind == "fish":
            draw_fish(p, cx - 2, cy, 1.5, QColor("#7FA8BC"))
        elif kind == "heart":
            draw_heart(p, cx, cy, 2.4, HEART)
        elif kind in ("?", "!"):
            f = QFont("Segoe UI", 16)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#8A7E74"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, kind)
            p.setPen(Qt.PenStyle.NoPen)
        elif kind == "note":
            f = QFont("Segoe UI", 15)
            p.setFont(f)
            p.setPen(QColor("#9C8FB0"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "♪")
            p.setPen(Qt.PenStyle.NoPen)

    def _draw_bowl(self, p, x, gy, food, fill):
        p.save()
        p.translate(x, gy)
        p.setPen(Qt.PenStyle.NoPen)
        if food == "Trash":
            s = 0.55 + 0.45 * clamp(fill, 0, 1)
            p.setBrush(QColor(60, 50, 45, 30))
            p.drawEllipse(QPointF(0, -2), 18 * s, 4.5)
            p.scale(s, s)
            page = QPainterPath()
            page.addRoundedRect(QRectF(-11, -34, 22, 30), 3, 3)
            p.setBrush(QColor("#F7F5F0"))
            p.drawPath(page)
            fold = QPainterPath(QPointF(4, -34))
            fold.lineTo(QPointF(11, -27))
            fold.lineTo(QPointF(4, -27))
            fold.closeSubpath()
            p.setBrush(QColor("#DDD8CE"))
            p.drawPath(fold)
            pen = QPen(QColor("#C9C2B6"), 1.8, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            for i in range(3):
                p.drawLine(QPointF(-6, -21 + i * 6), QPointF(6, -21 + i * 6))
            p.setPen(Qt.PenStyle.NoPen)
            p.restore()
            return
        p.setBrush(QColor(60, 50, 45, 30))
        p.drawEllipse(QPointF(0, -2), 30, 6)
        bowl = QPainterPath(QPointF(-27, -22))
        bowl.cubicTo(QPointF(-25, -4), QPointF(-16, -2), QPointF(0, -2))
        bowl.cubicTo(QPointF(16, -2), QPointF(25, -4), QPointF(27, -22))
        bowl.closeSubpath()
        p.setBrush(QColor("#E8A0B4"))
        p.drawPath(bowl)
        p.setBrush(QColor("#D186A0"))
        p.drawEllipse(QPointF(0, -22), 27, 7)
        if fill > 0.05:
            p.setBrush(FOOD_COLORS[food])
            p.drawEllipse(QPointF(0, -23), 22 * fill ** 0.5, 5.5 * fill ** 0.5 + 1)
        p.restore()

    def _draw_butterfly(self, p, x, y, t):
        p.save()
        p.translate(x, y)
        flap = abs(math.sin(t * 11))
        p.setPen(Qt.PenStyle.NoPen)
        for side in (-1, 1):
            w = 7.5 * (0.25 + 0.75 * flap)
            p.setBrush(QColor("#C9A9DD"))
            p.drawEllipse(QPointF(side * w * 0.70, -2.5), w, 5.0)
            p.setBrush(QColor("#D9BCE8"))
            p.drawEllipse(QPointF(side * w * 0.55, 2.5), w * 0.7, 3.6)
        p.setBrush(QColor("#6B5F55"))
        p.drawEllipse(QPointF(0, 0), 1.6, 5.0)
        p.restore()

    def _draw_gift(self, p, x, y, kind):
        p.save()
        p.translate(x, y)
        if kind == "leaf":
            p.setBrush(QColor("#9BB07C"))
            path = QPainterPath(QPointF(0, -14))
            path.quadTo(QPointF(10, -8), QPointF(0, 2))
            path.quadTo(QPointF(-10, -8), QPointF(0, -14))
            p.drawPath(path)
        elif kind == "sock":
            p.setBrush(QColor("#B8C4D8"))
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.WindingFill)
            path.addRoundedRect(QRectF(-4, -16, 9, 12), 3, 3)
            path.addRoundedRect(QRectF(-8, -7, 13, 8), 4, 4)
            p.drawPath(path.simplified())
        elif kind == "flower":
            pen = QPen(QColor("#8FA678"), 2.2, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(0, 0), QPointF(2, -7))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#E8919E"))
            for k in range(5):
                ang = k * 2 * math.pi / 5 - math.pi / 2
                p.drawEllipse(QPointF(2 + math.cos(ang) * 5.5,
                                      -12 + math.sin(ang) * 5.5), 4.0, 4.0)
            p.setBrush(QColor("#F2C86B"))
            p.drawEllipse(QPointF(2, -12), 3.0, 3.0)
        else:
            p.setBrush(QColor("#8A7A5C"))
            p.drawEllipse(QPointF(0, -5), 6, 4.5)
            p.drawEllipse(QPointF(5, -6), 3, 2.5)
        p.restore()


class StatsPopup(QWidget):
    def __init__(self, cat: CatWidget):
        super().__init__(None, Qt.WindowType.Popup
                         | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.cat = cat
        self.resize(250, 322)
        refresh = QTimer(self)
        refresh.timeout.connect(self.update)
        refresh.start(500)

    def popup(self):
        g = self.cat.geometry()
        x = clamp(g.center().x() - self.width() // 2,
                  self.cat.screen_geo.left() + 8,
                  self.cat.screen_geo.right() - self.width() - 8)
        y = max(self.cat.screen_geo.top() + 8, g.top() - self.height() + 66)
        self.move(int(x), int(y))
        self.show()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(4, 4, self.width() - 8, self.height() - 8)
        p.setPen(QPen(QColor(214, 198, 188), 2))
        p.setBrush(QColor(255, 251, 246, 250))
        p.drawRoundedRect(r, 18, 18)
        p.setPen(Qt.PenStyle.NoPen)

        st, emo = self.cat.st, self.cat.emo
        x0, y = 24, 40
        f = QFont("Segoe UI", 13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#6B5F55"))
        p.drawText(QPointF(x0, y), f"{st.name}")
        f2 = QFont("Segoe UI", 9)
        p.setFont(f2)
        p.setPen(QColor("#9A8C80"))
        age = st.age_days
        age_txt = f"{age:.1f} days" if age < 30 else f"{age/30:.1f} months"
        emoname = emo.emotion()
        face = EmotionEngine.EMOJI.get(emoname, "")
        p.drawText(QPointF(x0, y + 18), f"{age_txt} old · feeling {emoname} {face}")
        p.drawText(QPointF(x0, y + 34),
                   f"weight {st.weight:.2f} · {st.gifts} gifts · "
                   f"{st.files_eaten} files eaten")

        bars = [("Food", st.hunger, "#E8A87C"), ("Energy", st.energy, "#9BB8D3"),
                ("Fun", st.fun, "#C9A9DD"), ("Love", st.social, "#F2A0B5"),
                ("Clean", st.clean, "#9CCDB8"), ("Bond", st.bond, "#D9B36C")]
        by = y + 56
        for label, val, col in bars:
            p.setPen(QColor("#8A7E74"))
            p.setFont(f2)
            p.drawText(QPointF(x0, by + 11), label)
            track = QRectF(x0 + 52, by, 140, 14)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(236, 228, 220))
            p.drawRoundedRect(track, 7, 7)
            w = max(14.0, 140 * clamp(val, 0, 100) / 100)
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(x0 + 52, by, w, 14), 7, 7)
            by += 26

        p.setPen(QColor("#B0A297"))
        p.setFont(QFont("Segoe UI", 8))
        traits = (f"playful {st.playful:.1f} · needy {st.needy:.1f} · "
                  f"lazy {st.lazy:.1f} · brave {st.brave:.1f}")
        p.drawText(QPointF(x0, by + 14), traits)
        p.drawText(QPointF(x0, by + 30),
                   f"mood v={emo.valence:+.2f}  a={emo.arousal:+.2f}")


AUTOSTART_NAME = "DesktopCatMochi"


def _run_key(access):
    import winreg
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Run",
                          0, access)


def autostart_enabled() -> bool:
    try:
        import winreg
        with _run_key(winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def toggle_autostart():
    import winreg
    try:
        if autostart_enabled():
            with _run_key(winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, AUTOSTART_NAME)
        else:
            pyw = sys.executable.replace("python.exe", "pythonw.exe")
            cmd = f'"{pyw}" "{os.path.abspath(__file__)}"'
            with _run_key(winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
    except OSError:
        pass


def make_tray_icon(emotion: str = "calm") -> QIcon:
    """The tray face is a mood ring: ears flatten when he's cross, eyes
    close when he's content or asleep, narrow when he's grumpy."""
    img = QImage(64, 64, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(FUR)
    ears_back = emotion in ("grumpy", "moody", "sad")
    for side in (-1, 1):
        if ears_back:
            path = QPainterPath(QPointF(32 + side * 26, 32))
            path.lineTo(QPointF(32 + side * 31, 12))
            path.lineTo(QPointF(32 + side * 8, 17))
        else:
            path = QPainterPath(QPointF(32 + side * 26, 30))
            path.lineTo(QPointF(32 + side * 22, 4))
            path.lineTo(QPointF(32 + side * 6, 16))
        path.closeSubpath()
        p.drawPath(path)
    p.drawEllipse(QPointF(32, 36), 27, 24)
    if emotion in ("sleepy", "content"):
        pen = QPen(EYE, 3.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for ex in (22, 42):
            arc = QPainterPath(QPointF(ex - 5, 33))
            arc.quadTo(QPointF(ex, 37.5), QPointF(ex + 5, 33))
            p.drawPath(arc)
        p.setPen(Qt.PenStyle.NoPen)
    else:
        p.setBrush(EYE)
        ry = 2.6 if ears_back else 4.6
        for ex in (22, 42):
            p.drawEllipse(QPointF(ex, 34), 3.6, ry)
    p.setBrush(NOSE)
    p.drawEllipse(QPointF(32, 43), 3.4, 2.6)
    if emotion in ("grumpy", "moody", "sad"):
        pen = QPen(EYE, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        m = QPainterPath(QPointF(27, 52))
        m.quadTo(QPointF(32, 49 if emotion == "sad" else 52), QPointF(37, 52))
        p.drawPath(m)
        p.setPen(Qt.PenStyle.NoPen)
    p.end()
    return QIcon(QPixmap.fromImage(img))


def snapshot(out_path: str):
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    painter_cat = CatPainter()
    cells = []

    def P(**kw):
        d = DrawParams()
        for k, v in kw.items():
            setattr(d, k, v)
        return d

    cells.append(("sit happy", P(pose="sit", blush=0.7, tail_wag=0.6)))
    cells.append(("sit content(eyes)", P(pose="sit", eye_happy=1.0, blush=0.9)))
    cells.append(("sit grumpy", P(pose="sit", mouth="flat", ear_back=0.65, blush=0.1)))
    cells.append(("sit look-left", P(pose="sit", pupil_dx=-3, head_dx=-6, head_tilt=-5)))
    cells.append(("loaf", P(pose="loaf", eye_open=0.55, blush=0.5)))
    cells.append(("curl sleep", P(pose="curl", eye_open=0.0)))
    cells.append(("walk", P(pose="walk", walk_phase=1.2, tail_lift=0.7, blush=0.5)))
    cells.append(("walk left", P(pose="walk", walk_phase=3.6, dir=-1, tail_lift=0.7)))
    cells.append(("eat", P(pose="eat", groom_t=0.4)))
    cells.append(("groom", P(pose="groom", groom_t=0.8, eye_happy=1.0)))
    cells.append(("beg", P(pose="beg", eye_big=0.9, blush=0.8)))
    cells.append(("dragged", P(pose="drag", dangle=0.8, y_off=-40)))
    cells.append(("stretch", P(pose="stretch", eye_happy=1.0, tail_wag=0.5)))
    cells.append(("stretch yawn", P(pose="stretch", mouth="o", eye_happy=1.0)))
    cells.append(("stretch left", P(pose="stretch", dir=-1, eye_happy=1.0)))
    cells.append(("alert ears+drool", P(pose="sit", ear_perk=1.0, eye_big=0.9,
                                        mouth="o", pupil_dy=-2)))

    cols = 4
    rows = (len(cells) + cols - 1) // cols
    cw, ch = 300, 330
    img = QImage(cols * cw, rows * ch, QImage.Format.Format_ARGB32)
    img.fill(QColor("#F5E9E4"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    f = QFont("Segoe UI", 10)
    for i, (label, d) in enumerate(cells):
        cx = (i % cols) * cw + cw // 2
        gy = (i // cols) * ch + ch - 40
        p.save()
        p.translate(cx, gy)
        painter_cat.draw(p, d, 1.05)
        p.restore()
        p.setPen(QColor("#8A7E74"))
        p.setFont(f)
        p.drawText(QRectF((i % cols) * cw, (i // cols) * ch + ch - 28, cw, 24),
                   Qt.AlignmentFlag.AlignCenter, label)
        p.setPen(Qt.PenStyle.NoPen)
    p.end()
    img.save(out_path)
    print("wrote", out_path)


def simulate(hours: float):
    st = PetState()
    emo = EmotionEngine(st)
    brain = Brain(st, emo)
    brain.user_idle_minutes = lambda: 0.0
    rng = random.Random(7)
    t0 = time.time()
    step = 60.0
    n = int(hours * 3600 / step)
    fed = 0
    names = {v: k for k, v in globals().items()
             if k in ("IDLE_SIT", "LOAF", "SLEEP", "WALK", "GROOM", "WATCH",
                      "BEG", "ZOOMIES", "EAT", "PLAY", "DRAGGED", "FALLING",
                      "GIFT", "STRETCH", "CHASE", "GO_PERCH", "HOP_DOWN",
                      "CALLED")}
    stretches = chases = perches = 0
    for i in range(n):
        sim_t = t0 + i * step
        brain.now = (lambda t=sim_t: t)
        hour = (time.localtime(sim_t).tm_hour
                + time.localtime(sim_t).tm_min / 60.0)
        tick_needs(st, step, brain.action)
        emo.update(step, hour)
        if brain.action == BEG and rng.random() < 0.25:
            st.hunger = clamp(st.hunger + 50, 0, 100)
            emo.event(0.35, 0.1)
            brain.set_action(IDLE_SIT)
            fed += 1
        if brain.action == EAT:
            brain.set_action(IDLE_SIT)
        if brain.chase_x is None and rng.random() < 0.01:
            brain.chase_x = 500.0
        elif brain.chase_x is not None and rng.random() < 0.30:
            brain.chase_x = None
        brain.can_perch = not brain.on_perch
        if brain.action == GO_PERCH:
            brain.on_perch = True
            perches += 1
            brain.set_action(IDLE_SIT)
        elif brain.action == HOP_DOWN:
            brain.on_perch = False
            brain.set_action(IDLE_SIT)
        brain.perch_minutes = brain.perch_minutes + 1 if brain.on_perch else 0
        brain.choose(hour, cursor_active=rng.random() < 0.3, cursor_near=True)
        brain.action_t += step
        if brain.action == STRETCH:
            stretches += 1
        elif brain.action == CHASE:
            chases += 1
        assert -1 <= emo.valence <= 1 and -1 <= emo.arousal <= 1
        for v in (st.hunger, st.energy, st.fun, st.social, st.clean, st.bond):
            assert 0 <= v <= 100
        if i % 120 == 0:
            print(f"h{i*step/3600:5.1f} clock={hour:4.1f} "
                  f"hun={st.hunger:5.1f} en={st.energy:5.1f} fun={st.fun:5.1f} "
                  f"soc={st.social:5.1f} cln={st.clean:5.1f} "
                  f"v={emo.valence:+.2f} a={emo.arousal:+.2f} "
                  f"{emo.emotion():8s} {names.get(brain.action, '?')}")
    print(f"OK — {n} steps, fed {fed} times, {stretches} stretch ticks, "
          f"{chases} chase ticks, {perches} perch climbs, "
          f"no assertion failures")


def already_running() -> bool:
    """Single-instance guard via a named mutex (Windows)."""
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False,
                                            "DesktopCatMochi_SingleInstance")
        return ctypes.windll.kernel32.GetLastError() == 183  # ALREADY_EXISTS
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", metavar="PNG", help="render pose grid and exit")
    ap.add_argument("--simulate", type=float, metavar="HOURS",
                    help="headless systems simulation")
    args = ap.parse_args()

    if args.simulate:
        simulate(args.simulate)
        return
    if args.snapshot:
        snapshot(args.snapshot)
        return

    if already_running():
        print("Mochi is already on the taskbar.")
        return

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    st = PetState.load()
    away_h = st.apply_offline_decay()
    cat = CatWidget(st)
    cat.show()
    if away_h > 4:
        cat._bubble("heart", 4.0)
        cat._spawn_hearts(4)
        QTimer.singleShot(900, lambda: cat._play("meow"))

    tray = QSystemTrayIcon(make_tray_icon(cat.emo.emotion()))
    tray.setToolTip(st.name)
    cat.tray = tray
    tmenu = QMenu()
    feed = tmenu.addMenu("Feed")
    for name in FOODS:
        if name == "Trash":
            continue
        feed.addAction(QAction(name, tmenu,
                               triggered=lambda _=False, n=name: cat.feed(n)))
    feed.aboutToShow.connect(
        lambda: [a.setEnabled(cat.st.hunger <= 88) for a in feed.actions()])
    tmenu.addAction(QAction("Play", tmenu, triggered=cat.start_play))
    tmenu.addAction(QAction("Brush", tmenu, triggered=cat.brush))
    tmenu.addAction(QAction(f"Call {st.name}", tmenu, triggered=cat.call_over))
    if cat.sounds:
        snd = QAction("Sounds", tmenu, checkable=True, checked=not st.muted,
                      triggered=cat.toggle_mute)
        tmenu.addAction(snd)
        tmenu.aboutToShow.connect(lambda: snd.setChecked(not cat.st.muted))
    tmenu.addAction(QAction("Stats", tmenu, triggered=cat.show_stats))
    tmenu.addSeparator()
    tmenu.addAction(QAction("Quit", tmenu, triggered=app.quit))
    tray.setContextMenu(tmenu)
    tray.activated.connect(lambda r: cat.show_stats()
                           if r == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    app.aboutToQuit.connect(st.save)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
