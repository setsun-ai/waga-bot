#!/usr/bin/env python3
"""
Private Telegram bot: send your weight, get a chart back, with the trend and a BMI bar.

One file, standard library + Pillow. Answers only its owner, only in a private chat. Weights stay in one SQLite file;
they go nowhere else and are never logged.

  weight_bot.py            run the bot (TG_TOKEN, TG_OWNER_ID, WEIGHT_DB)
  weight_bot.py --whoami   print the Telegram ID of whoever last wrote to the bot
  weight_bot.py --selftest draw a chart, without Telegram (the installer runs it)
  weight_bot.py --version
"""
import calendar
import csv
import errno
import html
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

VERSION = "1.2.0"
REPO = "setsun-ai/weight-bot"
TZ = ZoneInfo(os.environ.get("WEIGHT_TZ", "Europe/Warsaw"))

MIN_KG, MAX_KG = 30, 300
MIN_CM, MAX_CM = 120, 230
TREND_DAYS = 7
DEFAULT_RANGE = 30
RANGES = ((14, "14 d"), (30, "30 d"), (90, "90 d"), (365, "Rok"), (0, "Max"))
TG_ROW_CHARS = 42  # a row of buttons on a phone fits this many characters; n buttons share it equally

Zone = namedtuple("Zone", "name lo hi colour")
ZONES = (Zone("niedowaga", 0, 18.5, (96, 165, 250)), Zone("norma", 18.5, 25, (74, 186, 122)),
         Zone("nadwaga", 25, 30, (245, 176, 65)), Zone("otyłość", 30, 99, (231, 96, 90)))
HEALTHY_TOP = 24.9  # the WHO's normal range ends "below 25"; what people read as the top of it


class Invalid(ValueError):
    """A value the owner typed that can't be right; the message says why."""


# --- reading what the owner types ---------------------------------------------------------------

ENTRY = re.compile(r"^\s*(\d{1,4}(?:[.,]\d+)?)\s*(?:kg)?\s*(.*?)\s*$", re.I)


def parse_day(word, today):
    """'' / wczoraj / 5.11 / 5.11.2025 / 2025-11-05 -> a date; None when it isn't a date at all."""
    word = word.lower()
    relative = {"": 0, "dzis": 0, "dziś": 0, "wczoraj": 1, "przedwczoraj": 2}
    if word in relative:
        return today - timedelta(days=relative[word])
    short = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", word)
    full = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", word)
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", word)
    if not (short or full or iso):
        return None
    try:
        if short:
            day = date(today.year, int(short[2]), int(short[1]))
            return day if day <= today else date(today.year - 1, int(short[2]), int(short[1]))
        return date(int(full[3]), int(full[2]), int(full[1])) if full else date(int(iso[1]), int(iso[2]), int(iso[3]))
    except ValueError:
        raise Invalid("Nie ma takiej daty.") from None


def parse_entry(text, today):
    """'82,4 wczoraj' -> (82.4, day). None when the text isn't a measurement; Invalid when it can't be a real one."""
    match = ENTRY.match(text or "")
    if not match:
        return None
    day = parse_day(match[2], today)
    if day is None:
        return None
    kg = round(float(match[1].replace(",", ".")), 1)
    if not MIN_KG <= kg <= MAX_KG:
        raise Invalid(f"Waga poza zakresem {MIN_KG}–{MAX_KG} kg. Przykład: 82,4")
    if day > today:
        raise Invalid("To data z przyszłości.")
    return kg, day


def parse_number(text):
    match = re.fullmatch(r"\s*(\d{1,4}(?:[.,]\d+)?)\s*(?:kg|cm)?\s*", text or "", re.I)
    return float(match[1].replace(",", ".")) if match else None


# --- the numbers --------------------------------------------------------------------------------

def bmi(kg, height_cm):
    return kg / (height_cm / 100) ** 2


def zone(value):
    return next(z for z in ZONES if value < z.hi)


def healthy_range(height_cm):
    metres = (height_cm / 100) ** 2
    return round(ZONES[1].lo * metres, 1), round(HEALTHY_TOP * metres, 1)


def trend(points):
    """For each measurement, the mean of the last TREND_DAYS calendar days (days can be missing)."""
    out = []
    for day, _ in points:
        recent = [kg for d, kg in points if timedelta(0) <= day - d < timedelta(days=TREND_DAYS)]
        out.append((day, sum(recent) / len(recent)))
    return out


def rate_per_week(points, today):
    """kg per week over the last 4 weeks (least squares), or None with too little data to say."""
    recent = [(d, kg) for d, kg in points if timedelta(0) <= today - d < timedelta(days=28)]
    if len(recent) < 5 or (recent[-1][0] - recent[0][0]).days < 14:
        return None
    xs = [(d - recent[0][0]).days for d, _ in recent]
    ys = [kg for _, kg in recent]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum((x - mean_x) ** 2 for x in xs)
    return slope * 7


def goal_date(points, goal, today):
    """When the current tempo reaches the goal; None if it isn't heading there, or not within two years."""
    rate = rate_per_week(points, today)
    if rate is None or abs(rate) < 0.05:
        return None
    left = goal - points[-1][1]
    if left * rate <= 0:
        return None
    days = round(left / rate * 7)
    return today + timedelta(days=days) if days <= 730 else None


def suspicious(kg, day, points):
    """Too far from the nearest other measurement to be a real change - most likely a typo."""
    others = [(abs((d - day).days), other) for d, other in points if d != day]
    if not others:
        return False
    days, nearest = min(others)
    return abs(kg - nearest) > 3 + 0.25 * days


# --- wording ------------------------------------------------------------------------------------

def num(value, digits=1):
    return f"{value:.{digits}f}".replace(".", ",")


def signed(value):
    rounded = round(value, 1)
    return ("+" if rounded > 0 else "−" if rounded < 0 else "±") + num(abs(rounded))


def caption(points, day, height, goal, today):
    """What the chart's message says about one measured day."""
    kg = dict(points)[day]
    lines = [f"<b>{num(kg)} kg</b> · {day:%d.%m.%Y}"]
    earlier = [(d, v) for d, v in points if d < day]
    if earlier:
        lines.append(f"{signed(kg - earlier[-1][1])} kg od ostatniego pomiaru ({earlier[-1][0]:%d.%m})")
    upto = [p for p in points if p[0] <= day]
    line = f"Średnia z 7 dni: {num(trend(upto)[-1][1])} kg" if len(upto) > 1 else ""
    rate = rate_per_week(points, today)
    if rate is not None:
        line += f" · tempo {signed(rate)} kg/tydz."
    if line:
        lines.append(line)
    if height:
        low, high = healthy_range(height)
        lines.append(f"BMI {num(bmi(kg, height))} – {zone(bmi(kg, height)).name} · "
                     f"norma dla {height} cm: {num(low)}–{num(high)} kg")
    else:
        lines.append("BMI pokażę po podaniu wzrostu: /wzrost 180")
    if goal:
        latest = points[-1][1]
        line = f"Cel {num(goal)} kg: zostało {num(abs(latest - goal))} kg"
        when = goal_date(points, goal, today)
        if when:
            line += f" · przy tym tempie ok. {when:%d.%m.%Y}"
        lines.append(line)
    return "\n".join(lines)


def week_summary(points, sunday):
    """The week ending on this Sunday next to the one before."""
    def mean(end):
        values = [kg for d, kg in points if timedelta(0) <= end - d < timedelta(days=7)]
        return (sum(values) / len(values), len(values)) if values else (None, 0)

    this, count = mean(sunday)
    before, _ = mean(sunday - timedelta(days=7))
    head = f"📅 Tydzień {sunday - timedelta(days=6):%d.%m}–{sunday:%d.%m}"
    if this is None:
        return f"{head}: bez pomiarów. W załączniku kopia wszystkich danych."
    change = f" ({signed(this - before)} kg)" if before is not None else ""
    return (f"{head}: średnia {num(this)} kg{change}, pomiarów: {count}.\n"
            "W załączniku kopia – odeślij ją, by przywrócić dane.")


def to_csv(points):
    return ("data,kg\n" + "".join(f"{d.isoformat()},{kg:.1f}\n" for d, kg in points)).encode()


def from_csv(data, today):
    """(rows, skipped): lines that aren't a real measurement are counted, not fatal."""
    rows, skipped = [], 0
    for row in csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))):
        if row[:2] == ["data", "kg"] or not row:
            continue
        try:
            day, kg = date.fromisoformat(row[0].strip()), float(row[1].replace(",", "."))
            if not MIN_KG <= kg <= MAX_KG or day > today:
                raise ValueError
            rows.append((day, kg))
        except (ValueError, IndexError):
            skipped += 1
    return rows, skipped


# --- storage ------------------------------------------------------------------------------------

class Store:
    """One SQLite file, one writer (the bot's only thread). Weights as tenths of a kg, so no float drift."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""CREATE TABLE IF NOT EXISTS weights(day TEXT PRIMARY KEY, tenths INTEGER NOT NULL);
                                 CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);""")

    def put(self, day, kg):
        self.db.execute("INSERT OR REPLACE INTO weights VALUES(?,?)", (day.isoformat(), round(kg * 10)))
        self.db.commit()

    def delete(self, day):
        self.db.execute("DELETE FROM weights WHERE day=?", (day.isoformat(),))
        self.db.commit()

    def points(self):
        rows = self.db.execute("SELECT day, tenths FROM weights ORDER BY day").fetchall()
        return [(date.fromisoformat(day), tenths / 10) for day, tenths in rows]

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
        self.db.commit()


# --- the chart ----------------------------------------------------------------------------------

WIDTH, HEIGHT = 1000, 780  # shown about 370 pt wide on a phone: big enough letters matter more than pixels
SCALE = 2  # drawn twice as big and scaled down, for smooth lines
INK, MUTED, GRID = (31, 41, 55), (107, 114, 128), (229, 231, 235)
RAW, TREND, GOAL, HIGHLIGHT = (156, 163, 175), (37, 99, 235), (147, 51, 234), (220, 38, 38)
FONTS = {False: ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"),
         True: ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf")}


def font(size, bold=False):
    for path in FONTS[bold]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size * SCALE)
    return ImageFont.load_default()


def nice_step(span, ticks=6):
    raw = max(span, 0.5) / ticks
    return next(s for s in (0.5, 1, 2, 5, 10, 20, 50, 100) if s >= raw)


def chart_window(points, days, today):
    """(first day on the axis, last day, what's shown): the range asked for, but no long empty start for a new user."""
    shown = [p for p in points if not days or (today - p[0]).days < days] or points[-1:]
    end = max(today, shown[-1][0])
    first = shown[0][0]
    return min(first, end - timedelta(days=6)), end, shown


def value_scale(values, goal):
    """(low, high) of the values and a goal close to them; at least 3 kg tall, so 0.2 kg doesn't look like a cliff."""
    low, high = min(values), max(values)
    if goal and low - 5 <= goal <= high + 5:  # a far-away goal would flatten the line; it stays in the caption
        low, high = min(low, goal), max(high, goal)
    if high - low < 3:
        middle = (low + high) / 2
        low, high = middle - 1.5, middle + 1.5
    return low, high


def render_chart(points, days, height, goal, highlight, today):
    """PNG of the chart: measurements, the 7-day mean, the goal, and below them the BMI bar with today's place on it."""
    image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    s = SCALE
    left, right, top = 86 * s, (WIDTH - 36) * s, 92 * s
    bottom = (HEIGHT - (276 if height else 124)) * s

    first_day, end, shown = chart_window(points, days, today)
    span = (end - first_day).days
    means = [m for m in trend(points) if m[0] >= first_day]

    low, high = value_scale([kg for _, kg in shown] + [kg for _, kg in means], goal)
    step = nice_step(high - low)
    y_min = (low // step) * step
    y_max = (high // step + 1) * step

    def x(day):
        return left + (right - left) * (day - first_day).days / span

    def y(kg):
        return bottom - (bottom - top) * (kg - y_min) / (y_max - y_min)

    measured = dict(points).get(highlight, shown[-1][1])
    draw.text((left, 20 * s), f"{num(measured)} kg", font=font(48, True), fill=INK)
    title = f"od {first_day:%d.%m.%Y}" if not days or first_day > today - timedelta(days=days - 1) else \
        ("ostatni rok" if days == 365 else f"ostatnie {days} dni")
    draw.text((right, 40 * s), title, font=font(26), fill=MUTED, anchor="ra")

    tick = y_min
    while tick <= y_max + 1e-9:
        draw.line([(left, y(tick)), (right, y(tick))], fill=GRID, width=2 * s)
        draw.text((left - 14 * s, y(tick)), f"{tick:g}".replace(".", ","), font=font(25), fill=MUTED, anchor="rm")
        tick += step

    gap = max(1, span // 5)
    for back in range(0, span + 1, gap):
        day = end - timedelta(days=back)
        draw.line([(x(day), bottom), (x(day), bottom + 8 * s)], fill=MUTED, width=2 * s)
        draw.text((x(day), bottom + 14 * s), f"{day:%d.%m}", font=font(25), fill=MUTED,
                  anchor="ra" if back == 0 else "ma")

    if goal and y_min <= goal <= y_max:
        for dash in range(int(left), int(right), 28 * s):
            draw.line([(dash, y(goal)), (min(dash + 16 * s, right), y(goal))], fill=GOAL, width=3 * s)
        draw.text((right, y(goal) - 8 * s), f"cel {num(goal)} kg", font=font(25), fill=GOAL, anchor="rd")

    if len(shown) > 1:
        draw.line([(x(d), y(kg)) for d, kg in shown], fill=RAW, width=2 * s, joint="curve")
    if len(means) > 1:
        draw.line([(x(d), y(kg)) for d, kg in means], fill=TREND, width=6 * s, joint="curve")
    dot = (3 if len(shown) > 60 else 6) * s
    for d, kg in shown:
        draw.ellipse([x(d) - dot, y(kg) - dot, x(d) + dot, y(kg) + dot], fill=RAW)
    for d, kg in shown:
        if d == highlight:
            mark = 12 * s
            draw.ellipse([x(d) - mark, y(kg) - mark, x(d) + mark, y(kg) + mark], fill=HIGHLIGHT)

    legend = bottom + 64 * s
    draw.line([(left, legend), (left + 44 * s, legend)], fill=TREND, width=6 * s)
    draw.text((left + 54 * s, legend), "średnia z 7 dni", font=font(25), fill=MUTED, anchor="lm")
    draw.ellipse([left + 290 * s - dot, legend - dot, left + 290 * s + dot, legend + dot], fill=RAW)
    draw.text((left + 306 * s, legend), "pomiary", font=font(25), fill=MUTED, anchor="lm")

    if height:
        _draw_bmi_bar(draw, bmi(measured, height), left, right, bottom + 146 * s)
    return _png(image)


def _draw_bmi_bar(draw, value, left, right, top):
    s = SCALE
    low, high = 14, 40
    tall = 42 * s

    def x(v):
        return left + (right - left) * (min(max(v, low), high) - low) / (high - low)

    for z in ZONES:
        draw.rectangle([x(max(z.lo, low)), top, x(min(z.hi, high)), top + tall], fill=z.colour)
        draw.text(((x(max(z.lo, low)) + x(min(z.hi, high))) / 2, top + tall / 2), z.name, font=font(24, True),
                  fill=(255, 255, 255), anchor="mm")
    for edge in (18.5, 25, 30):
        draw.text((x(edge), top + tall + 8 * s), f"{edge:g}".replace(".", ","), font=font(23), fill=MUTED, anchor="ma")
    mark = x(value)
    draw.polygon([(mark, top - 2 * s), (mark - 12 * s, top - 22 * s), (mark + 12 * s, top - 22 * s)], fill=INK)
    draw.text((min(max(mark, left + 60 * s), right - 60 * s), top - 26 * s), f"BMI {num(value)}", font=font(27, True),
              fill=INK, anchor="md")


def _png(image):
    out = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


# --- Telegram -----------------------------------------------------------------------------------

class TelegramError(Exception):
    """Carries Telegram's own words or the error's type, never the URL: it contains the token."""


class Telegram:
    FILE_NAMES = {"photo": "photo.png", "chart": "chart.png", "document": "weight.csv"}

    def __init__(self, token, base="https://api.telegram.org"):
        self.token, self.base = token, base

    def __call__(self, method, fields=None, files=None, timeout=60):
        url = f"{self.base}/bot{self.token}/{method}"
        fields = {k: str(v) for k, v in (fields or {}).items()}
        if files:
            boundary = "weight-" + os.urandom(8).hex()
            parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                     for k, v in fields.items()]
            for name, data in files.items():
                filename = self.FILE_NAMES.get(name, name)
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                             "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
            body, kind = b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"
        else:
            body, kind = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=body, headers={"Content-Type": kind})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read()).get("result")
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read()).get("description")
            except ValueError:
                reason = None
            raise TelegramError(f"{method}: {reason or e.code}") from None
        except (OSError, ValueError) as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None

    def download(self, file_id):
        path = self("getFile", {"file_id": file_id})["file_path"]
        try:
            with urllib.request.urlopen(f"{self.base}/file/bot{self.token}/{path}", timeout=60) as response:
                return response.read()
        except OSError as e:
            raise TelegramError(f"download: {type(e).__name__}") from None


# --- updating itself -------------------------------------------------------------------------------

class UpdateError(Exception):
    """Why an update can't go ahead, in words for the owner."""


def version_tuple(text):
    return tuple(int(part) for part in text.lstrip("v").split("."))


def version_in(source):
    match = re.search(rb'^VERSION = "(\d+\.\d+\.\d+)"', source, re.M)
    return match[1].decode() if match else None


def web_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "weight-bot"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise UpdateError(f"GitHub odpowiedział błędem {e.code}") from None
    except OSError:
        raise UpdateError("brak połączenia z GitHubem") from None


class Updater:
    """
    The service's sandbox can't write to /opt, so a downloaded version lives next to the data: a copy that passed its
    selftest, and the one before it for rollback. main() starts the copy when it is newer than the installed file.
    """

    def __init__(self, folder, fetch=web_get):
        self.fetch = fetch
        self.copy = Path(folder) / "weight_bot.py"
        self.previous = Path(folder) / "weight_bot.py.bak"
        self.new = Path(folder) / "weight_bot.py.new"

    @property
    def can_roll_back(self):
        return self.copy.exists()

    def latest(self):
        try:
            tag = json.loads(self.fetch(f"https://api.github.com/repos/{REPO}/releases/latest")).get("tag_name")
        except ValueError:
            raise UpdateError("dziwna odpowiedź GitHuba") from None
        if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise UpdateError("dziwna odpowiedź GitHuba")
        return tag

    def install(self, tag):
        """Only a file of the right version that passes its own selftest replaces what's there."""
        source = self.fetch(f"https://raw.githubusercontent.com/{REPO}/{tag}/weight_bot.py")
        if version_in(source) != tag[1:]:
            raise UpdateError("pobrany plik ma inną wersję")
        self.copy.parent.mkdir(parents=True, exist_ok=True)
        self.new.write_bytes(source)
        try:
            check = subprocess.run([sys.executable, str(self.new), "--selftest"], capture_output=True, timeout=120)
            if check.returncode != 0:
                raise UpdateError("nowa wersja nie przeszła selftestu")
            if self.copy.exists():
                os.replace(self.copy, self.previous)
            os.replace(self.new, self.copy)
        except (OSError, subprocess.TimeoutExpired):
            raise UpdateError("nie udało się sprawdzić nowej wersji") from None
        finally:
            self.new.unlink(missing_ok=True)

    def rollback(self):
        """One step back; with no earlier copy, back to the installed file."""
        if not self.copy.exists():
            raise UpdateError("nie ma wersji, do której można wrócić")
        if self.previous.exists():
            os.replace(self.previous, self.copy)
        else:
            self.copy.unlink()


def newer_copy(copy):
    """The downloaded copy if it is newer than this file; an older one (after a fresh install) is ignored."""
    try:
        version = version_in(Path(copy).read_bytes())
    except OSError:
        return None
    return copy if version and version_tuple(version) > version_tuple(VERSION) else None


def label_width(text):
    """Width of a button text in characters: emoji and symbols take two, variation selectors none."""
    return sum(0 if ch in "️‍" else 2 if ord(ch) >= 0x2300 else 1 for ch in text)


def keyboard(rows):
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows if row]})


MENU = keyboard([[("⚖️ Dodaj pomiar", "m:add"), ("📈 Wykres", "m:chart")],
                 [("📋 Ostatnie", "m:recent"), ("🎯 Cel", "m:goal")],
                 [("📏 Wzrost", "m:height"), ("⏰ Przypomnienie", "m:remind")],
                 [("💾 Kopia CSV", "m:csv"), ("❓ Pomoc", "m:help")],
                 [("🔄 Aktualizacja", "m:update")]])
MENU_TEXT = "⚖️ <b>Menu</b>\nWyślij wagę, np. 82,4, albo wybierz kafelek."
BACK = [("↩️ Menu", "m:menu")]
MONTHS = ("Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec", "Lipiec", "Sierpień", "Wrzesień", "Październik",
          "Listopad", "Grudzień")
PAGE = 7  # measurements on one screen of "Ostatnie"
DAY_CHOICE_MINUTES = 10  # how long a typed number goes to the picked day, not to today
HELP = ("⚖️ Wyślij wagę, np. <code>82,4</code>, a odeślę wykres.\n\n"
        "⚖️ Dodaj pomiar – inny dzień z kalendarza, waga przyciskami\n"
        "<code>82,4 wczoraj</code>, <code>82,4 5.11</code> – to samo pisemnie\n"
        "Drugi pomiar tego samego dnia zastępuje pierwszy; edycja wiadomości też poprawia pomiar.\n\n"
        "/wzrost 180 – do BMI\n/cel 78 – cel (/cel off wyłącza)\n"
        "/przypomnienie 8:00 – gdy nie ma pomiaru (off wyłącza)\n"
        "/wykres · /ostatnie · /csv · /menu · /update · /rollback\nOdesłanie pliku CSV przywraca dane.\n\n"
        "Waga waha się z dnia na dzień o 1–2 kg; ważniejsza jest średnia z 7 dni. "
        "BMI to ogólna skala dla dorosłych: nie odróżnia mięśni od tłuszczu.")
PROMPTS = {"height": "📏 Podaj wzrost w cm, np. 180.", "goal": "🎯 Podaj cel w kg, np. 78 (off wyłącza).",
           "remind": "⏰ O której przypominać, gdy nie ma pomiaru? Np. 8:00 (off wyłącza)."}


class Bot:
    def __init__(self, store, api, owner, updater, now=None):
        self.store, self.api, self.owner, self.updater = store, api, owner, updater
        self.restart = False  # a new version is in place: the main loop ends and systemd starts it
        self.now = now or (lambda: datetime.now(TZ))
        self.waiting = None  # the setting the next plain message is an answer for
        self.shown_range = {}  # message id -> the range its chart shows (no redraw of what's already there)
        self.day_choice = None  # (day, valid until, stepper's message id): where the next typed number goes

    # --- settings --------------------------------------------------------------------------------

    def height(self):
        value = self.store.get("height")
        return int(value) if value else None

    def goal(self):
        value = self.store.get("goal")
        return float(value) if value else None

    def say(self, text, markup=None):
        fields = {"chat_id": self.owner, "text": text, "parse_mode": "HTML"}
        if markup:
            fields["reply_markup"] = markup
        return self.api("sendMessage", fields)

    def show(self, message, text, markup=None):
        """In place of the tapped message; a chart is a photo and can't become text, so there it's a new message."""
        if "photo" in message:
            return self.say(text, markup)
        fields = {"chat_id": self.owner, "message_id": message.get("message_id"), "text": text, "parse_mode": "HTML"}
        if markup:
            fields["reply_markup"] = markup
        try:
            return self.api("editMessageText", fields)
        except TelegramError as e:
            if "not modified" not in str(e):  # a stepper already at its limit
                raise
            return None

    def set_setting(self, name, value):
        """Saves a setting from what was typed; a bad value is explained and the next message can retry."""
        self.waiting = None
        handler = {"height": self.set_height, "goal": self.set_goal, "remind": self.set_remind}[name]
        try:
            handler(value.strip())
        except Invalid as e:
            self.waiting = name
            self.say(f"⚠️ {e}")

    def set_height(self, value):
        cm = parse_number(value)
        if cm is None or not MIN_CM <= cm <= MAX_CM:
            raise Invalid(f"Wzrost podaj w cm, od {MIN_CM} do {MAX_CM}. Przykład: 180")
        self.store.set("height", round(cm))
        low, high = healthy_range(round(cm))
        self.say(f"📏 Zapisane: {round(cm)} cm. Norma BMI to {num(low)}–{num(high)} kg.\nWyślij wagę, np. 82,4.")

    def set_goal(self, value):
        if value.lower() == "off":
            self.store.set("goal", None)
            self.say("🎯 Cel wyłączony.")
            return
        kg = parse_number(value)
        if kg is None or not MIN_KG <= kg <= MAX_KG:
            raise Invalid(f"Cel podaj w kg, od {MIN_KG} do {MAX_KG}. Przykład: 78")
        height = self.height()
        if height and bmi(kg, height) < ZONES[0].hi:
            low, _ = healthy_range(height)
            raise Invalid(f"Taki cel to niedowaga przy {height} cm – nie będę go śledzić. "
                          f"Norma zaczyna się od {num(low)} kg.")
        self.store.set("goal", round(kg, 1))
        self.say(f"🎯 Cel: {num(kg)} kg. Pokażę go na wykresie.")

    def set_remind(self, value):
        if value.lower() == "off":
            self.store.set("remind", None)
            self.say("⏰ Przypomnienia wyłączone.")
            return
        match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value)
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise Invalid("Podaj godzinę, np. 8:00 (off wyłącza).")
        moment = f"{int(match[1]):02d}:{match[2]}"
        self.store.set("remind", moment)
        self.say(f"⏰ Przypomnę o {moment}, jeśli tego dnia nie będzie pomiaru.")

    # --- the chart message -----------------------------------------------------------------------

    def chart_markup(self, days, day):
        rows = [[(("• " if d == days else "") + label, f"r:{d}:{day.isoformat()}") for d, label in RANGES],
                [("🗑 Usuń pomiar", f"del:{day.isoformat()}"), ("☰ Menu", "m:menu")]]
        return keyboard(rows)

    def chart_parts(self, days, day):
        today = self.now().date()
        points = self.store.points()
        return (render_chart(points, days, self.height(), self.goal(), day, today),
                caption(points, day, self.height(), self.goal(), today))

    def send_chart(self, day):
        """The default range, or the shortest one that still shows this day (an old measurement added later)."""
        age = (self.now().date() - day).days
        days = next((d for d, _ in RANGES if d >= DEFAULT_RANGE and age < d), 0)
        png, text = self.chart_parts(days, day)
        sent = self.api("sendPhoto", {"chat_id": self.owner, "caption": text, "parse_mode": "HTML",
                                      "reply_markup": self.chart_markup(days, day)}, {"photo": png})
        self.shown_range[(sent or {}).get("message_id")] = days

    def redraw(self, message_id, days, day, prefix=""):
        png, text = self.chart_parts(days, day)
        media = {"type": "photo", "media": "attach://chart", "caption": prefix + text, "parse_mode": "HTML"}
        try:
            self.api("editMessageMedia", {"chat_id": self.owner, "message_id": message_id, "media": json.dumps(media),
                                          "reply_markup": self.chart_markup(days, day)}, {"chart": png})
        except TelegramError as e:
            if "not modified" not in str(e):  # same picture after a restart forgot what was shown
                raise
        self.shown_range[message_id] = days

    # --- screens of buttons ---------------------------------------------------------------------------

    def picker_view(self, year=None, month=None):
        """Quick days and a month's calendar: no future days, days with a measurement marked."""
        today = self.now().date()
        year, month = year or today.year, month or today.month
        measured = {d for d, _ in self.store.points()}
        quick = [(label, f"day:{(today - timedelta(days=back)).isoformat()}")
                 for back, label in enumerate(("Dziś", "Wczoraj", "Przedwczoraj"))]
        rows = [quick, [(name, "x") for name in ("Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd")]]
        for week in calendar.monthcalendar(year, month):
            row = []
            for number in week:
                day = date(year, month, number) if number else None
                if day is None or day > today:
                    row.append(("·", "x"))
                    continue
                label = f"[{number}]" if day == today else str(number)
                row.append((label + ("•" if day in measured else ""), f"day:{day.isoformat()}"))
            rows.append(row)
        before = date(year, month, 1) - timedelta(days=1)
        after = date(year, month, calendar.monthrange(year, month)[1]) + timedelta(days=1)
        nav = [("◀ Poprzedni", f"cal:{before:%Y-%m}")]
        if after <= today:
            nav.append(("Następny ▶", f"cal:{after:%Y-%m}"))
        return (f"📅 <b>{MONTHS[month - 1]} {year}</b>\nWybierz dzień pomiaru. • to dzień z zapisaną wagą.",
                keyboard(rows + [nav, BACK]))

    def stepper_view(self, day, tenths=None):
        """The weight of one day set with buttons. Each button carries the value it leads to; nothing is saved yet."""
        points = self.store.points()
        saved = dict(points).get(day)
        if tenths is None:
            nearest = min(points, key=lambda p: abs((p[0] - day).days))[1] if points else 80.0
            tenths = round(nearest * 10)  # a day that has a value is its own nearest
        low, high = MIN_KG * 10, MAX_KG * 10

        def step(label, change):
            return label, f"st:{day.isoformat()}:{min(max(tenths + change, low), high)}"

        kg = num(tenths / 10)
        rows = [[step("−1", -10), step("−0,1", -1), step("+0,1", 1), step("+1", 10)],
                [(f"✅ Zapisz {kg} kg", f"sv:{day.isoformat()}:{tenths}")]]
        lower = [("🗑 Usuń", f"dx:{day.isoformat()}")] if saved else []
        rows += [lower + [("📅 Zmień dzień", "m:add")], BACK]
        note = f"Zapisane teraz: {num(saved)} kg\n" if saved else ""
        text = f"⚖️ Pomiar z {day:%d.%m.%Y}\n<b>{kg} kg</b>\n{note}Ustaw przyciskami albo wpisz liczbę."
        return text, keyboard(rows)

    def recent_view(self, offset=0):
        points = self.store.points()[::-1]
        if not points:
            return "Nie ma jeszcze pomiarów.", keyboard([[("⚖️ Dodaj pomiar", "m:add")], BACK])
        rows = [[(f"{d:%d.%m.%Y} · {num(kg)} kg", f"day:{d.isoformat()}")] for d, kg in points[offset:offset + PAGE]]
        pages = []
        if offset:
            pages.append(("Nowsze ▶", f"rec:{max(offset - PAGE, 0)}"))
        if offset + PAGE < len(points):
            pages.append(("◀ Starsze", f"rec:{offset + PAGE}"))
        return "📋 <b>Ostatnie pomiary</b>\nStuknij, żeby poprawić lub usunąć.", keyboard(rows + [pages, BACK])

    def open_day(self, message, day, tenths=None):
        self.waiting = None
        until = self.now() + timedelta(minutes=DAY_CHOICE_MINUTES)
        self.day_choice = (day, until, message.get("message_id"))
        self.show(message, *self.stepper_view(day, tenths))

    def chosen_day(self):
        """The picked day the next typed number belongs to, while the choice is fresh."""
        if self.day_choice and self.now() >= self.day_choice[1]:
            self.day_choice = None
        return self.day_choice

    def close_stepper(self, message_id):
        self.day_choice = None
        self.api("deleteMessage", {"chat_id": self.owner, "message_id": message_id})

    # --- updates -----------------------------------------------------------------------------------------

    def tell(self, message, text, markup=None):
        return self.show(message, text, markup) if message else self.say(text, markup)

    def update_view(self):
        rows, head = [], f"🔄 Wersja {VERSION}"
        try:
            latest = self.updater.latest()
            if version_tuple(latest) > version_tuple(VERSION):
                head += f", najnowsza to {latest[1:]}."
                rows.append([(f"⬆️ Zainstaluj {latest[1:]}", f"up:{latest}")])
            else:
                head += " – to najnowsza."
        except UpdateError as e:
            head += f"\n⚠️ Nie udało się sprawdzić: {html.escape(str(e))}."
        if self.updater.can_roll_back:
            rows.append([("⏪ Wróć do poprzedniej", "rb")])
        return head, keyboard(rows + [BACK])

    def install_update(self, tag, message=None):
        self.tell(message, f"⏳ Pobieram i sprawdzam {tag[1:]}…")
        try:
            self.updater.install(tag)
        except UpdateError as e:
            self.tell(message, f"⚠️ Aktualizacja się nie udała: {html.escape(str(e))}. Działa dalej {VERSION}.",
                      keyboard([BACK]))
            return
        self.restarting(message)

    def roll_back(self, message=None):
        if not self.updater.can_roll_back:
            self.tell(message, "Nie ma wersji, do której można wrócić.", keyboard([BACK]))
            return
        try:
            self.updater.rollback()
        except (UpdateError, OSError) as e:
            self.tell(message, f"⚠️ Nie udało się wrócić: {html.escape(str(e))}.", keyboard([BACK]))
            return
        self.restarting(message)

    def restarting(self, message):
        self.store.set("updated_from", VERSION)
        self.restart = True
        self.tell(message, "♻️ Uruchamiam ponownie, za chwilę dam znać.")

    def announce_update(self):
        """The first thing after a restart caused by /update or /rollback."""
        old = self.store.get("updated_from")
        if old:
            self.say(f"✅ Działa wersja {VERSION} (było {old}).")
            self.store.set("updated_from", None)

    def check_for_update(self, now):
        """Once a day; each new version is announced once. No connection isn't worth a message."""
        today = now.date().isoformat()
        if self.store.get("update_checked") == today:
            return
        self.store.set("update_checked", today)
        try:
            latest = self.updater.latest()
        except UpdateError:
            return
        if version_tuple(latest) > version_tuple(VERSION) and self.store.get("update_told") != latest:
            self.say(f"🔄 Jest nowa wersja: {latest[1:]} (masz {VERSION}).",
                     keyboard([[(f"⬆️ Zainstaluj {latest[1:]}", f"up:{latest}")]]))
            self.store.set("update_told", latest)

    def latest_chart(self):
        points = self.store.points()
        if points:
            self.send_chart(points[-1][0])
        else:
            self.say("Nie ma jeszcze pomiarów. Wyślij wagę, np. 82,4.")

    def save(self, kg, day):
        self.store.put(day, kg)
        self.send_chart(day)

    def measurement(self, kg, day, edited=False):
        if suspicious(kg, day, self.store.points()) and not edited:
            tenths = round(kg * 10)
            self.say(f"🤔 {num(kg)} kg to duży skok od poprzedniego pomiaru. Zapisać?",
                     keyboard([[("✅ Tak, zapisz", f"ok:{day.isoformat()}:{tenths}"), ("❌ Nie", "no")]]))
        else:
            self.save(kg, day)

    # --- incoming --------------------------------------------------------------------------------

    def handle_update(self, update):
        query = update.get("callback_query")
        if query:
            if query.get("from", {}).get("id") == self.owner:
                self.handle_tap(query)
            return
        edited = "edited_message" in update
        message = update.get("edited_message") or update.get("message") or {}
        if message.get("from", {}).get("id") != self.owner or message.get("chat", {}).get("type") != "private":
            return
        sent = datetime.fromtimestamp(message.get("date", 0), TZ).date()
        if message.get("document"):
            self.handle_document(message["document"])
        elif message.get("text"):
            self.handle_text(message["text"].strip(), sent, edited)

    def handle_text(self, text, sent, edited):
        if text.startswith("/"):
            if not edited:
                self.waiting = self.day_choice = None
                self.handle_command(text, sent)
            return
        if self.waiting and not edited:
            self.set_setting(self.waiting, text)
            return
        choice = self.chosen_day()
        number = parse_number(text)
        if choice and number is not None and MIN_KG <= number <= MAX_KG and not edited:
            self.close_stepper(choice[2])
            self.measurement(round(number, 1), choice[0])
            return
        try:
            entry = parse_entry(text, sent)
        except Invalid as e:
            self.say(f"⚠️ {e}")
            return
        if entry:
            self.day_choice = None
            self.measurement(*entry, edited=edited)
        elif not edited:
            self.say("Wyślij wagę, np. <code>82,4</code>, albo /menu.")

    def handle_command(self, text, sent):
        parts = text.split(maxsplit=1)
        command = parts[0][1:].split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        settings = {"wzrost": "height", "cel": "goal", "przypomnienie": "remind"}
        if command == "start":
            if self.height():
                self.say(MENU_TEXT, MENU)
            else:
                self.waiting = "height"
                self.say("⚖️ Cześć! Wyślesz wagę, a ja odeślę wykres z trendem i BMI.\n\n" + PROMPTS["height"])
        elif command == "menu":
            self.say(MENU_TEXT, MENU)
        elif command == "help":
            self.say(HELP)
        elif command == "wykres":
            self.latest_chart()
        elif command == "ostatnie":
            self.say(*self.recent_view())
        elif command == "csv":
            self.send_copy(self.now().date().strftime("💾 Kopia z %d.%m.%Y – odeślij ją, by przywrócić dane."))
        elif command == "update":
            self.update_command()
        elif command == "rollback":
            self.roll_back()
        elif command == "waga":
            self.handle_text(args, sent, False) if args else self.say(*self.picker_view())
        elif command in settings:
            if args:
                self.set_setting(settings[command], args)
            else:
                self.waiting = settings[command]
                self.say(PROMPTS[settings[command]])
        else:
            self.say(HELP)

    def update_command(self):
        try:
            latest = self.updater.latest()
        except UpdateError as e:
            self.say(f"⚠️ Nie udało się sprawdzić: {html.escape(str(e))}.")
            return
        if version_tuple(latest) > version_tuple(VERSION):
            self.install_update(latest)
        else:
            self.say(f"✅ Wersja {VERSION} – to najnowsza.")

    def handle_tap(self, query):
        data, message = query.get("data", ""), query.get("message", {})
        message_id = message.get("message_id")
        self.api("answerCallbackQuery", {"callback_query_id": query["id"]})
        kind, _, rest = data.partition(":")
        if kind == "x":  # a calendar cell or a day's name: nothing to do
            return
        if kind == "st":
            day, tenths = rest.split(":")
            self.open_day(message, date.fromisoformat(day), int(tenths))
            return
        self.day_choice = None  # any other button leaves the stepper
        if kind == "day":
            self.open_day(message, date.fromisoformat(rest))
        elif kind == "cal":
            year, month = rest.split("-")
            self.show(message, *self.picker_view(int(year), int(month)))
        elif kind == "rec":
            self.show(message, *self.recent_view(int(rest)))
        elif kind == "sv":
            day, tenths = rest.split(":")
            self.close_stepper(message_id)
            self.save(int(tenths) / 10, date.fromisoformat(day))
        elif kind == "up":
            self.install_update(rest, message)
        elif kind == "rb":
            self.roll_back(message)
        elif kind == "dx":
            day = date.fromisoformat(rest)
            self.store.delete(day)
            self.show(message, f"🗑 Usunięto pomiar z {day:%d.%m.%Y}.", keyboard([[("📋 Ostatnie", "m:recent")], BACK]))
        elif kind == "r":
            days, day = rest.split(":")
            if self.shown_range.get(message_id) != int(days) and self.store.points():
                self.redraw(message_id, int(days), self.visible_day(date.fromisoformat(day)))
        elif kind == "del":
            self.delete_measurement(date.fromisoformat(rest), message_id)
        elif kind == "ok":
            day, tenths = rest.split(":")
            self.api("deleteMessage", {"chat_id": self.owner, "message_id": message_id})
            self.save(int(tenths) / 10, date.fromisoformat(day))
        elif kind == "no":
            self.api("deleteMessage", {"chat_id": self.owner, "message_id": message_id})
        elif kind == "m":
            self.waiting = None
            self.handle_menu(rest, message)

    def visible_day(self, day):
        """The day a chart was about, or the latest one if it has been deleted since."""
        points = dict(self.store.points())
        return day if day in points else max(points)

    def delete_measurement(self, day, message_id):
        self.store.delete(day)
        note = f"🗑 Usunięto pomiar z {day:%d.%m}."
        points = self.store.points()
        if points:
            self.redraw(message_id, self.shown_range.get(message_id, DEFAULT_RANGE), points[-1][0], note + "\n\n")
        else:
            self.api("deleteMessage", {"chat_id": self.owner, "message_id": message_id})
            self.say(note)

    def handle_menu(self, name, message):
        if name == "chart":
            self.latest_chart()
        elif name == "recent":
            self.show(message, *self.recent_view())
        elif name == "add":
            self.show(message, *self.picker_view())
        elif name == "csv":
            self.handle_command("/csv", self.now().date())
        elif name == "help":
            self.show(message, HELP, keyboard([BACK]))
        elif name == "update":
            self.show(message, *self.update_view())
        elif name == "menu":
            self.show(message, MENU_TEXT, MENU)
        elif name in PROMPTS:
            self.waiting = name
            current = {"height": self.height(), "goal": self.goal(), "remind": self.store.get("remind")}[name]
            self.show(message, PROMPTS[name] + (f"\nTeraz: {current}" if current else ""), keyboard([BACK]))

    def send_copy(self, text, silent=False):
        points = self.store.points()
        if not points:
            self.say("Nie ma jeszcze pomiarów.")
            return
        fields = {"chat_id": self.owner, "caption": text}
        if silent:
            fields["disable_notification"] = "true"
        self.api("sendDocument", fields, {"document": to_csv(points)})

    def handle_document(self, document):
        if not document.get("file_name", "").lower().endswith(".csv") or document.get("file_size", 0) > 1_000_000:
            self.say("Przyjmuję tylko plik CSV z kopią (do 1 MB).")
            return
        rows, skipped = from_csv(self.api.download(document["file_id"]), self.now().date())
        for day, kg in rows:
            self.store.put(day, kg)
        self.say(f"📥 Wczytano pomiarów: {len(rows)} (pominięto {skipped}).")

    # --- things that happen on their own ---------------------------------------------------------

    def tick(self):
        now = self.now()
        self.remind(now)
        self.weekly(now)
        self.check_for_update(now)

    def remind(self, now):
        moment, today = self.store.get("remind"), now.date().isoformat()
        if not moment or now.strftime("%H:%M") < moment or self.store.get("reminded") == today:
            return
        if not any(d == now.date() for d, _ in self.store.points()):
            self.say("⚖️ Dziś nie ma jeszcze pomiaru. Wyślij wagę, np. 82,4.")
        self.store.set("reminded", today)

    def weekly(self, now):
        """Sunday from 20:00; on Monday it catches up after a laptop that was off."""
        day = now.date()
        if not (day.weekday() == 6 and now.hour >= 20 or day.weekday() == 0):
            return
        sunday = day if day.weekday() == 6 else day - timedelta(days=1)
        points = self.store.points()
        if points and self.store.get("weekly") != sunday.isoformat():
            self.send_copy(week_summary(points, sunday), silent=True)
            self.store.set("weekly", sunday.isoformat())


# --- running ------------------------------------------------------------------------------------

def log(message):
    print(message, flush=True)


def whoami(api):
    try:
        updates = api("getUpdates", {"timeout": 0}) or []
    except TelegramError as e:
        print(e)  # e.g. a wrong token; the installer shows this as it is
        return
    people = [u.get("message", {}).get("from", {}).get("id") for u in updates]
    people = [p for p in people if p]
    print(people[-1] if people else "Napisz coś do bota i uruchom to jeszcze raz.")


def run(api, bot):
    log(f"weight-bot {VERSION} started")
    try:
        api("setMyCommands", {"commands": json.dumps([{"command": "menu", "description": "Menu z kafelkami"},
                                                      {"command": "help", "description": "Jak to działa"}])})
        bot.announce_update()
    except TelegramError as e:  # no connection yet: the loop below waits for it
        log(f"start: {e}")
    offset, failures = None, 0
    while True:
        try:
            bot.tick()
            fields = {"timeout": 30, "allowed_updates": json.dumps(["message", "edited_message", "callback_query"])}
            if offset:
                fields["offset"] = offset
            updates = api("getUpdates", fields) or []
            failures = 0
        except (TelegramError, sqlite3.Error) as e:
            failures += 1
            log(f"poll failed: {e}")
            time.sleep(min(60, 5 * failures))
            continue
        for update in updates:
            offset = update["update_id"] + 1  # first: an update that breaks something must not come back forever
            try:
                bot.handle_update(update)
            except Exception as e:  # the message's content isn't logged: it's a weight
                log(f"update failed: {type(e).__name__}")
                try:
                    disk_full = isinstance(e, OSError) and e.errno == errno.ENOSPC
                    bot.say("❌ Brak miejsca na dysku." if disk_full else f"❌ Coś nie wyszło ({type(e).__name__}).")
                except Exception:
                    pass
        if bot.restart:
            try:  # confirm what was received, or the same /update would come back after the restart
                api("getUpdates", {"offset": offset, "timeout": 0})
            except TelegramError as e:
                log(f"confirm: {e}")
            sys.exit(0)  # Restart=always starts the new version


def selftest():
    """Everything a chart needs on this machine: Pillow, the font, the time zone."""
    try:
        today = datetime.now(TZ).date()
        points = [(today - timedelta(days=1), 82.5), (today, 82.4)]
        render_chart(points, DEFAULT_RANGE, 180, 78.0, today, today)
    except Exception as e:
        print(f"Selftest failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"OK {VERSION}")
    return 0


def main(argv):
    if "--version" in argv:
        print(VERSION)
        return 0
    if "--selftest" in argv:
        return selftest()
    token, owner = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_OWNER_ID", "")
    if not token:
        print("Brak TG_TOKEN", file=sys.stderr)
        return 2
    api = Telegram(token)
    if "--whoami" in argv:
        whoami(api)
        return 0
    if not owner.isdigit():
        print("Brak TG_OWNER_ID (sprawdzisz go przez --whoami)", file=sys.stderr)
        return 2
    db = Path(os.environ.get("WEIGHT_DB", "weight.db"))
    updates = db.parent / "update"
    if newer_copy(updates / "weight_bot.py"):
        os.execv(sys.executable, [sys.executable, str(updates / "weight_bot.py")])
    os.umask(0o077)
    store = Store(db)
    try:
        run(api, Bot(store, api, int(owner), Updater(updates)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
