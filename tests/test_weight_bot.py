"""The bot as its owner uses it: messages and button taps go in, what Telegram would show comes out."""
import http.server
import io
import json
import threading
from datetime import date, datetime, timedelta

import pytest
from PIL import Image

import weight_bot

OWNER = 100
TODAY = date(2026, 11, 6)


class FakeApi:
    """Records what would go to Telegram; never touches the network."""

    def __init__(self):
        self.calls = []
        self.downloads = {}

    def __call__(self, method, fields=None, files=None):
        self.calls.append((method, fields or {}, files or {}))
        return {"message_id": len(self.calls)}

    def download(self, file_id):
        return self.downloads[file_id]

    def last(self, method):
        return next(c for c in reversed(self.calls) if c[0] == method)

    def count(self, method):
        return sum(c[0] == method for c in self.calls)

    def texts(self):
        return [c[1].get("text") or c[1].get("caption") or "" for c in self.calls]


class FakeUpdater:
    """The update mechanism as the bot sees it; the real one has its own tests below."""

    def __init__(self):
        self.tag = "v" + weight_bot.VERSION
        self.can_roll_back = False
        self.installed, self.rolled_back, self.failure, self.checks = [], 0, None, 0

    def latest(self):
        self.checks += 1
        if self.failure:
            raise self.failure
        return self.tag

    def install(self, tag):
        if self.failure:
            raise self.failure
        self.installed.append(tag)

    def rollback(self):
        self.rolled_back += 1


@pytest.fixture
def bot(tmp_path):
    clock = {"now": datetime(2026, 11, 6, 9, 0, tzinfo=weight_bot.TZ)}
    api = FakeApi()
    updater = FakeUpdater()
    b = weight_bot.Bot(weight_bot.Store(tmp_path / "weight.db"), api, OWNER, updater, now=lambda: clock["now"])
    b.fake, b.clock, b.fake_updater = api, clock, updater
    return b


def text(bot, value, uid=OWNER, chat=None, when=None, edited=False):
    moment = when or bot.clock["now"]
    message = {"message_id": 1, "date": int(moment.timestamp()), "text": value, "from": {"id": uid},
               "chat": {"id": chat if chat is not None else uid, "type": "private" if (chat or uid) > 0 else "group"}}
    bot.handle_update({"edited_message" if edited else "message": message})


def tap(bot, data, uid=OWNER, photo=False):
    message = {"message_id": 7, "chat": {"id": uid, "type": "private"}}
    if photo:
        message["photo"] = [{"file_id": "p"}]
    bot.handle_update({"callback_query": {"id": "q", "data": data, "from": {"id": uid}, "message": message}})


def buttons(call):
    return [b["callback_data"] for row in json.loads(call[1]["reply_markup"])["inline_keyboard"] for b in row]


def fill(bot, values, end=TODAY):
    for i, kg in enumerate(values):
        bot.store.put(end - timedelta(days=len(values) - 1 - i), kg)


# --- reading what the owner types -----------------------------------------------------------------

@pytest.mark.parametrize("typed, expected", [
    ("82,4", (82.4, TODAY)), ("82.4", (82.4, TODAY)), ("82", (82.0, TODAY)), (" 82,4 kg ", (82.4, TODAY)),
    ("82,45", (82.5, TODAY)), ("82,4 wczoraj", (82.4, TODAY - timedelta(days=1))),
    ("82,4 5.11", (82.4, date(2026, 11, 5))), ("82,4 5.11.2025", (82.4, date(2025, 11, 5))),
    ("82,4 2026-11-01", (82.4, date(2026, 11, 1))),
    ("82,4 24.12", (82.4, date(2025, 12, 24))),  # a day that hasn't come this year means last year
])
def test_reads_a_weight(typed, expected):
    assert weight_bot.parse_entry(typed, TODAY) == expected


@pytest.mark.parametrize("typed", ["hej", "", "czesc 82", "82,4 kiedys tam"])
def test_other_text_is_not_a_weight(typed):
    assert weight_bot.parse_entry(typed, TODAY) is None


@pytest.mark.parametrize("typed", ["8,24", "824", "0", "82,4 31.02", "82,4 2027-01-01"])
def test_impossible_values_are_refused(typed):
    with pytest.raises(weight_bot.Invalid):
        weight_bot.parse_entry(typed, TODAY)


# --- BMI, trend, tempo ----------------------------------------------------------------------------

@pytest.mark.parametrize("kg, name", [(55, "niedowaga"), (60, "norma"), (80.6, "norma"), (81, "nadwaga"),
                                      (97, "nadwaga"), (98, "otyłość")])
def test_bmi_zones_at_180_cm(kg, name):
    assert weight_bot.zone(weight_bot.bmi(kg, 180)).name == name


def test_healthy_range_for_a_height():
    assert weight_bot.healthy_range(180) == (59.9, 80.7)


def test_trend_is_the_mean_of_the_last_seven_days_not_seven_points():
    points = [(TODAY - timedelta(days=20), 90.0), (TODAY - timedelta(days=1), 80.0), (TODAY, 82.0)]
    assert weight_bot.trend(points) == [(points[0][0], 90.0), (points[1][0], 80.0), (TODAY, 81.0)]


def test_tempo_needs_two_weeks_of_data():
    short = [(TODAY - timedelta(days=d), 80 + d * 0.1) for d in range(6, -1, -1)]
    assert weight_bot.rate_per_week(short, TODAY) is None
    steady = [(TODAY - timedelta(days=d), 80 + d * 0.1) for d in range(20, -1, -1)]  # losing 0.1 kg a day
    assert weight_bot.rate_per_week(steady, TODAY) == pytest.approx(-0.7)


def test_goal_date_only_when_heading_towards_it():
    falling = [(TODAY - timedelta(days=d), 80 + d * 0.1) for d in range(20, -1, -1)]
    assert weight_bot.goal_date(falling, 73, TODAY) == TODAY + timedelta(days=70)
    assert weight_bot.goal_date(falling, 85, TODAY) is None
    assert weight_bot.goal_date([(TODAY, 80.0)], 73, TODAY) is None


def test_sudden_jump_looks_like_a_typo_but_slow_change_does_not():
    points = [(TODAY - timedelta(days=1), 82.4)]
    assert weight_bot.suspicious(28.4, TODAY, points)
    assert not weight_bot.suspicious(83.0, TODAY, points)
    assert not weight_bot.suspicious(78.0, TODAY, [(TODAY - timedelta(days=60), 85.0)])  # two months apart: plausible
    assert not weight_bot.suspicious(28.4, TODAY, [])


# --- the daily flow -------------------------------------------------------------------------------

def test_a_weight_comes_back_as_a_chart_with_a_caption(bot):
    bot.store.set("height", "180")
    fill(bot, [82.7], end=TODAY - timedelta(days=1))
    text(bot, "82,4")
    method, fields, files = bot.fake.last("sendPhoto")
    assert files["photo"][:8] == b"\x89PNG\r\n\x1a\n" and fields["chat_id"] == OWNER
    assert "82,4 kg" in fields["caption"] and "−0,3 kg" in fields["caption"]
    assert "BMI 25,4" in fields["caption"] and "nadwaga" in fields["caption"] and "59,9–80,7" in fields["caption"]
    assert "r:14:2026-11-06" in buttons(bot.fake.last("sendPhoto")) and "del:2026-11-06" in buttons(
        bot.fake.last("sendPhoto"))
    assert bot.store.points() == [(TODAY - timedelta(days=1), 82.7), (TODAY, 82.4)]


def test_second_weight_on_one_day_replaces_the_first(bot):
    text(bot, "82,4")
    text(bot, "82,1")
    assert bot.store.points() == [(TODAY, 82.1)]


def test_editing_the_message_corrects_the_measurement_of_its_day(bot):
    yesterday = bot.clock["now"] - timedelta(days=1)
    text(bot, "82,4", when=yesterday)
    text(bot, "81,4", when=yesterday, edited=True)
    assert bot.store.points() == [(TODAY - timedelta(days=1), 81.4)]


def test_date_is_when_it_was_sent_not_when_the_laptop_came_back(bot):
    text(bot, "82,4", when=bot.clock["now"] - timedelta(hours=20))
    assert bot.store.points() == [(TODAY - timedelta(days=1), 82.4)]


def test_day_follows_the_local_clock_at_night(bot):
    local_night = datetime(2026, 11, 6, 0, 30, tzinfo=weight_bot.TZ)  # still 5 Nov in UTC
    text(bot, "82,4", when=local_night)
    assert bot.store.points()[0][0] == TODAY


def test_a_jump_is_confirmed_first(bot):
    fill(bot, [82.4], end=TODAY - timedelta(days=1))
    text(bot, "42,8")
    assert len(bot.store.points()) == 1 and bot.fake.count("sendPhoto") == 0
    assert "ok:2026-11-06:428" in buttons(bot.fake.last("sendMessage"))
    tap(bot, "ok:2026-11-06:428")
    assert bot.store.points()[-1] == (TODAY, 42.8) and bot.fake.count("sendPhoto") == 1


def test_range_buttons_redraw_the_same_message(bot):
    fill(bot, [80 + i * 0.1 for i in range(40)])
    tap(bot, "r:90:2026-11-06")
    method, fields, files = bot.fake.last("editMessageMedia")
    assert fields["message_id"] == 7 and files["chart"][:4] == b"\x89PNG"
    assert json.loads(fields["media"])["media"] == "attach://chart"
    labels = [b["text"] for row in json.loads(fields["reply_markup"])["inline_keyboard"] for b in row]
    assert "• 90 d" in labels
    for row in json.loads(fields["reply_markup"])["inline_keyboard"]:
        for b in row:
            assert weight_bot.label_width(b["text"]) <= 42 // len(row) - 2, b["text"]


def test_tapping_the_range_already_shown_does_not_ask_telegram_to_redraw(bot):
    fill(bot, [80.0, 81.0])
    tap(bot, "r:30:2026-11-06")
    tap(bot, "r:30:2026-11-06")  # Telegram answers "message is not modified" otherwise
    assert bot.fake.count("editMessageMedia") == 1


def test_delete_button_removes_only_that_day(bot):
    fill(bot, [80.0, 81.0])
    tap(bot, "del:2026-11-06")
    assert bot.store.points() == [(TODAY - timedelta(days=1), 80.0)]
    assert "Usunięto" in json.loads(bot.fake.last("editMessageMedia")[1]["media"])["caption"]


def test_deleting_the_last_measurement_leaves_a_plain_message(bot):
    fill(bot, [80.0])
    tap(bot, "del:2026-11-06")
    assert bot.store.points() == [] and bot.fake.count("deleteMessage") == 1
    assert "Usunięto" in bot.fake.last("sendMessage")[1]["text"]


# --- settings by tiles and typed commands ---------------------------------------------------------

def test_first_start_asks_for_height_then_a_number_is_the_height(bot):
    text(bot, "/start")
    assert "wzrost" in bot.fake.last("sendMessage")[1]["text"].lower()
    text(bot, "180")
    assert bot.store.get("height") == "180" and bot.store.points() == []
    text(bot, "82,4")
    assert bot.store.points() == [(TODAY, 82.4)]


def test_menu_tiles_fit_a_phone_and_open_settings(bot):
    text(bot, "/menu")
    keyboard = json.loads(bot.fake.last("sendMessage")[1]["reply_markup"])["inline_keyboard"]
    for row in keyboard:
        for b in row:
            assert weight_bot.label_width(b["text"]) <= 42 // len(row) - 2, b["text"]
    tap(bot, "m:goal")
    assert "cel" in bot.fake.last("editMessageText")[1]["text"].lower()
    text(bot, "78")
    assert bot.store.get("goal") == "78.0" and bot.store.points() == []


def test_a_slash_command_cancels_waiting_for_a_setting(bot):
    tap(bot, "m:height")
    text(bot, "/menu")
    text(bot, "82,4")
    assert bot.store.get("height") is None and bot.store.points() == [(TODAY, 82.4)]


def test_bad_setting_is_explained_and_can_be_retried(bot):
    text(bot, "/wzrost 18")
    assert "120" in bot.fake.texts()[-1] and bot.store.get("height") is None
    text(bot, "/wzrost 180")
    assert bot.store.get("height") == "180"


def test_a_goal_in_the_underweight_range_is_not_tracked(bot):
    bot.store.set("height", "180")
    text(bot, "/cel 50")
    assert bot.store.get("goal") is None and "niedowag" in bot.fake.texts()[-1]
    text(bot, "/cel 75")
    assert bot.store.get("goal") == "75.0"
    text(bot, "/cel off")
    assert bot.store.get("goal") is None


def test_caption_shows_the_goal_and_its_date(bot):
    bot.store.set("goal", "73.0")
    for d in range(21):
        bot.store.put(TODAY - timedelta(days=d), 80 + d * 0.1)
    assert "tempo −0,7 kg/tydz." in weight_bot.caption(bot.store.points(), TODAY, None, 73.0, TODAY)
    caption = weight_bot.caption(bot.store.points(), TODAY, None, 73.0, TODAY)
    assert "zostało 7,0 kg" in caption and "15.01.2027" in caption


def test_waga_command_adds_a_past_measurement(bot):
    text(bot, "/waga 82,4 5.11")
    assert bot.store.points() == [(date(2026, 11, 5), 82.4)]


def test_recent_list(bot):
    fill(bot, [80.0, 81.5])
    text(bot, "/ostatnie")
    labels = [b["text"] for row in json.loads(bot.fake.last("sendMessage")[1]["reply_markup"])["inline_keyboard"]
              for b in row]
    assert labels[0].startswith("06.11") and "81,5" in labels[0] and labels[1].startswith("05.11")


# --- only the owner, only in private --------------------------------------------------------------

def test_anyone_else_gets_silence(bot):
    text(bot, "82,4", uid=999)
    tap(bot, "del:2026-11-06", uid=999)
    assert bot.store.points() == []
    assert [c[0] for c in bot.fake.calls] in ([], ["answerCallbackQuery"])


def test_owner_in_a_group_is_ignored(bot):
    text(bot, "82,4", chat=-5)
    assert bot.store.points() == [] and bot.fake.calls == []


def test_no_weight_is_ever_printed_to_the_log(bot, capsys):
    text(bot, "82,4")
    text(bot, "hej")
    out = capsys.readouterr()
    assert "82" not in out.out + out.err


# --- reminders and the weekly copy ----------------------------------------------------------------

def test_reminder_once_a_day_only_without_a_measurement(bot):
    text(bot, "/przypomnienie 8:30")
    before = bot.fake.count("sendMessage")
    bot.clock["now"] = datetime(2026, 11, 6, 8, 0, tzinfo=weight_bot.TZ)
    bot.tick()
    assert bot.fake.count("sendMessage") == before
    bot.clock["now"] = datetime(2026, 11, 6, 8, 31, tzinfo=weight_bot.TZ)
    bot.tick()
    bot.tick()
    assert bot.fake.count("sendMessage") == before + 1
    bot.clock["now"] = datetime(2026, 11, 7, 9, 0, tzinfo=weight_bot.TZ)
    text(bot, "82,4")
    sent = bot.fake.count("sendMessage")
    bot.tick()
    assert bot.fake.count("sendMessage") == sent


def test_reminder_is_off_until_asked_for(bot):
    bot.tick()
    assert bot.fake.calls == []
    text(bot, "/przypomnienie 8:30")
    text(bot, "/przypomnienie off")
    assert bot.store.get("remind") is None


def test_sunday_evening_summary_with_a_copy_once_a_week(bot):
    fill(bot, [80.0 + i * 0.1 for i in range(14)], end=date(2026, 11, 8))
    bot.clock["now"] = datetime(2026, 11, 8, 19, 0, tzinfo=weight_bot.TZ)
    bot.tick()
    assert bot.fake.count("sendDocument") == 0
    bot.clock["now"] = datetime(2026, 11, 8, 20, 5, tzinfo=weight_bot.TZ)
    bot.tick()
    bot.tick()
    method, fields, files = bot.fake.last("sendDocument")
    assert bot.fake.count("sendDocument") == 1 and fields["disable_notification"] == "true"
    assert files["document"].startswith(b"data,kg\n2026-10-26,80.0\n")
    assert "81,0" in fields["caption"]  # the week's mean, and how it changed
    assert "−0,7" in fields["caption"] or "+0,7" in fields["caption"]


def test_no_copy_without_data(bot):
    bot.clock["now"] = datetime(2026, 11, 8, 21, 0, tzinfo=weight_bot.TZ)
    bot.tick()
    assert bot.fake.calls == []


def test_csv_goes_out_and_comes_back(bot):
    fill(bot, [80.0, 81.5])
    text(bot, "/csv")
    exported = bot.fake.last("sendDocument")[2]["document"]
    bot.store.delete(TODAY)
    bot.fake.downloads["f1"] = exported + b"nonsense,line\n2026-11-01,999\n"
    bot.handle_update({"message": {"message_id": 2, "date": int(bot.clock["now"].timestamp()), "from": {"id": OWNER},
                                   "chat": {"id": OWNER, "type": "private"},
                                   "document": {"file_id": "f1", "file_name": "weight.csv", "file_size": 60}}})
    assert bot.store.points() == [(TODAY - timedelta(days=1), 80.0), (TODAY, 81.5)]
    said = bot.fake.last("sendMessage")[1]["text"]
    assert "2" in said and "pominięto 2" in said.lower()


# --- the chart ------------------------------------------------------------------------------------

def picture(png):
    return Image.open(io.BytesIO(png)).convert("RGB")


def has_colour(image, colour):
    return colour in {c for _, c in image.getcolors(maxcolors=1 << 24)}


def test_chart_draws_the_bmi_bar_only_with_a_height():
    points = [(TODAY - timedelta(days=d), 82 - d * 0.1) for d in range(30)]
    with_bar = picture(weight_bot.render_chart(points, 30, 180, None, TODAY, TODAY))
    without = picture(weight_bot.render_chart(points, 30, None, None, TODAY, TODAY))
    assert with_bar.size == without.size == (weight_bot.WIDTH, weight_bot.HEIGHT)
    zone_colours = [z.colour for z in weight_bot.ZONES]
    assert all(has_colour(with_bar, c) for c in zone_colours)
    assert not any(has_colour(without, c) for c in zone_colours)


def test_chart_copes_with_one_point_flat_data_and_old_points():
    picture(weight_bot.render_chart([(TODAY, 82.4)], 30, 180, None, TODAY, TODAY))
    picture(weight_bot.render_chart([(TODAY, 82.0), (TODAY - timedelta(days=1), 82.0)], 14, 180, 78.0, TODAY, TODAY))
    picture(weight_bot.render_chart([(TODAY - timedelta(days=400), 82.0)], 14, None, None, TODAY, TODAY))
    heavy = [(TODAY - timedelta(days=d), 200 - d) for d in range(150)]
    picture(weight_bot.render_chart(heavy, 0, 150, None, TODAY, TODAY))


def test_highlighted_day_is_marked_on_the_chart():
    points = [(TODAY - timedelta(days=1), 80.0), (TODAY, 81.0)]
    assert has_colour(picture(weight_bot.render_chart(points, 30, None, None, TODAY, TODAY)), weight_bot.HIGHLIGHT)


# --- the real HTTP layer --------------------------------------------------------------------------

def test_telegram_call_sends_fields_and_a_file_and_hides_the_token_in_errors():
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["type"] = self.headers["Content-Type"]
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            fail = self.path.endswith("/fail")
            self.send_response(400 if fail else 200)
            self.end_headers()
            answer = {"ok": not fail, "description": "Bad Request: nope", "result": {"x": 1}}
            self.wfile.write(json.dumps(answer).encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = weight_bot.Telegram("123:SECRET", base=f"http://127.0.0.1:{server.server_port}")
    assert api("sendPhoto", {"chat_id": 5, "caption": "cześć"}, {"photo": b"\x89PNGdata"}) == {"x": 1}
    assert seen["path"] == "/bot123:SECRET/sendPhoto" and seen["type"].startswith("multipart/form-data")
    assert b'name="chat_id"\r\n\r\n5' in seen["body"] and "cześć".encode() in seen["body"]
    assert b'name="photo"; filename="photo.png"' in seen["body"] and b"\x89PNGdata" in seen["body"]
    with pytest.raises(weight_bot.TelegramError) as error:
        api("fail")
    assert "SECRET" not in str(error.value) and "nope" in str(error.value)
    server.shutdown()


def test_an_old_measurement_opens_a_range_that_shows_it(bot):
    fill(bot, [80.0, 81.0])
    text(bot, "/waga 82,4 2026-02-01")
    labels = [b["text"] for row in json.loads(bot.fake.last("sendPhoto")[1]["reply_markup"])["inline_keyboard"]
              for b in row]
    assert "• Rok" in labels
    text(bot, "/waga 82,4 2024-02-01")
    labels = [b["text"] for row in json.loads(bot.fake.last("sendPhoto")[1]["reply_markup"])["inline_keyboard"]
              for b in row]
    assert "• Max" in labels


def test_chart_starts_where_the_data_starts_and_never_magnifies_tiny_changes():
    points = [(TODAY - timedelta(days=1), 82.4), (TODAY, 82.5)]
    assert weight_bot.chart_window(points, 30, TODAY)[0] == TODAY - timedelta(days=6)  # not 30 empty days
    long = [(TODAY - timedelta(days=d), 80.0) for d in range(100, -1, -1)]
    assert weight_bot.chart_window(long, 30, TODAY)[0] == TODAY - timedelta(days=29)
    low, high = weight_bot.value_scale([82.4, 82.5], None)
    assert high - low >= 3 and low < 82.4 and high > 82.5


def test_selftest_renders_a_chart_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("TG_TOKEN", raising=False)
    assert weight_bot.main(["--selftest"]) == 0
    assert weight_bot.VERSION in capsys.readouterr().out


def test_selftest_fails_when_the_chart_cannot_be_drawn(monkeypatch):
    def broken(*args):
        raise OSError("no font")

    monkeypatch.setattr(weight_bot, "render_chart", broken)
    assert weight_bot.main(["--selftest"]) == 1


# --- the main loop -------------------------------------------------------------------------------------------

class ScriptedApi(FakeApi):
    """getUpdates answers from a script: a list of updates, an exception to raise, and in the end it stops the loop."""

    def __init__(self, script):
        super().__init__()
        self.script, self.offsets = list(script), []

    def __call__(self, method, fields=None, files=None):
        if method == "getUpdates":
            self.offsets.append((fields or {}).get("offset"))
            step = self.script.pop(0) if self.script else KeyboardInterrupt()
            if isinstance(step, BaseException):
                raise step
            return step
        return super().__call__(method, fields, files)


def message_update(update_id, value, moment):
    return {"update_id": update_id, "message": {"message_id": update_id, "date": int(moment.timestamp()), "text": value,
                                                "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}}}


def test_a_broken_update_is_reported_once_and_the_bot_goes_on(bot, monkeypatch, capsys):
    now = bot.clock["now"]
    api = ScriptedApi([[message_update(5, "82,4", now)], [message_update(6, "82,1", now)]])
    bot.api = api
    real = bot.send_chart
    calls = []

    def breaks_first(day):
        calls.append(day)
        if len(calls) == 1:
            raise KeyError("boom")
        real(day)

    monkeypatch.setattr(bot, "send_chart", breaks_first)
    with pytest.raises(KeyboardInterrupt):
        weight_bot.run(api, bot)
    assert api.offsets == [None, 6, 7]  # never asks for the broken update again
    assert any("Coś nie wyszło" in said for said in api.texts()) and api.count("sendPhoto") == 1
    assert bot.store.points() == [(TODAY, 82.1)]
    out = capsys.readouterr().out
    assert "82" not in out and "KeyError" in out


def test_a_network_failure_only_delays_the_bot(bot, monkeypatch):
    sleeps = []
    monkeypatch.setattr(weight_bot.time, "sleep", sleeps.append)
    offline = weight_bot.TelegramError("getUpdates: URLError")
    api = ScriptedApi([offline, offline, [message_update(1, "82,4", bot.clock["now"])]])
    bot.api = api
    with pytest.raises(KeyboardInterrupt):
        weight_bot.run(api, bot)
    assert sleeps == [5, 10] and bot.store.points() == [(TODAY, 82.4)]


def test_whoami_prints_the_id_or_explains(capsys):
    weight_bot.whoami(ScriptedApi([[message_update(1, "hej", datetime(2026, 11, 6, tzinfo=weight_bot.TZ))]]))
    assert capsys.readouterr().out.strip() == str(OWNER)
    weight_bot.whoami(ScriptedApi([weight_bot.TelegramError("getUpdates: Unauthorized")]))
    assert "Unauthorized" in capsys.readouterr().out


def test_chart_headline_shows_the_day_it_is_about():
    points = [(TODAY - timedelta(days=1), 80.0), (TODAY, 91.0)]
    header = (0, 0, 400, 80)
    about_today = picture(weight_bot.render_chart(points, 30, None, None, TODAY, TODAY)).crop(header)
    yesterday = TODAY - timedelta(days=1)
    about_yesterday = picture(weight_bot.render_chart(points, 30, None, None, yesterday, TODAY)).crop(header)
    assert about_today.tobytes() != about_yesterday.tobytes()


def test_correcting_todays_typo_does_not_ask_again(bot):
    bot.store.put(TODAY, 42.8)
    text(bot, "82,4")
    assert bot.store.points() == [(TODAY, 82.4)] and bot.fake.count("sendPhoto") == 1


def test_data_file_is_readable_only_by_its_owner(tmp_path, monkeypatch):
    import os
    import stat

    db = tmp_path / "state" / "weight.db"
    monkeypatch.setenv("TG_TOKEN", "1:x")
    monkeypatch.setenv("TG_OWNER_ID", "100")
    monkeypatch.setenv("WEIGHT_DB", str(db))
    monkeypatch.setattr(weight_bot, "run", lambda api, bot: None)
    before = os.umask(0o022)
    try:
        weight_bot.main([])
    finally:
        os.umask(before)
    assert stat.S_IMODE(db.stat().st_mode) & 0o077 == 0


# --- tiles, the day picker and the stepper ----------------------------------------------------

def assert_fits(call):
    for row in json.loads(call[1]["reply_markup"])["inline_keyboard"]:
        for b in row:
            assert weight_bot.label_width(b["text"]) <= 42 // len(row) - 2, b["text"]


def test_menu_has_every_action_as_a_tile_and_tiles_open_in_place(bot):
    text(bot, "/menu")
    assert buttons(bot.fake.last("sendMessage")) == ["m:add", "m:chart", "m:recent", "m:goal", "m:height", "m:remind",
                                                     "m:csv", "m:help", "m:update"]
    tap(bot, "m:help")
    edit = bot.fake.last("editMessageText")
    assert "82,4" in edit[1]["text"] and buttons(edit) == ["m:menu"] and edit[1]["message_id"] == 7
    tap(bot, "m:menu")
    assert buttons(bot.fake.last("editMessageText"))[0] == "m:add"


def test_a_chart_is_a_photo_so_its_menu_button_sends_a_new_message(bot):
    sent = bot.fake.count("sendMessage")
    tap(bot, "m:menu", photo=True)
    assert bot.fake.count("sendMessage") == sent + 1 and bot.fake.count("editMessageText") == 0


def test_day_picker_offers_quick_days_and_a_calendar_without_the_future(bot):
    fill(bot, [80.0], end=date(2026, 11, 3))
    tap(bot, "m:add")
    edit = bot.fake.last("editMessageText")
    assert_fits(edit)
    data = buttons(edit)
    assert data[:3] == ["day:2026-11-06", "day:2026-11-05", "day:2026-11-04"]
    assert "day:2026-11-01" in data and "day:2026-11-06" in data
    assert not any(d.startswith("day:2026-11-07") or d == "day:2026-11-30" for d in data)  # the future
    assert "cal:2026-10" in data and not any(d.startswith("cal:2026-12") for d in data)  # no month ahead
    labels = [b["text"] for row in json.loads(edit[1]["reply_markup"])["inline_keyboard"] for b in row]
    assert "3•" in labels and "listopad 2026" in edit[1]["text"].lower()  # days with a measurement are marked


def test_day_picker_goes_back_through_months_and_years(bot):
    tap(bot, "cal:2026-01")
    edit = bot.fake.last("editMessageText")
    assert_fits(edit)
    assert "styczeń 2026" in edit[1]["text"].lower() and "day:2026-01-31" in buttons(edit)
    assert "cal:2025-12" in buttons(edit) and "cal:2026-02" in buttons(edit)


def test_stepper_starts_from_the_nearest_measurement_and_changes_nothing_until_saved(bot):
    fill(bot, [82.4], end=date(2026, 11, 2))
    bot.store.put(date(2026, 10, 1), 90.0)
    tap(bot, "day:2026-11-04")
    edit = bot.fake.last("editMessageText")
    assert_fits(edit)
    assert "04.11.2026" in edit[1]["text"] and "82,4 kg" in edit[1]["text"]
    assert buttons(edit)[:5] == ["st:2026-11-04:814", "st:2026-11-04:823", "st:2026-11-04:825", "st:2026-11-04:834",
                                 "sv:2026-11-04:824"]
    tap(bot, "st:2026-11-04:825")
    assert "82,5 kg" in bot.fake.last("editMessageText")[1]["text"]
    assert len(bot.store.points()) == 2


def test_saving_from_the_stepper_stores_that_day_and_answers_with_its_chart(bot):
    tap(bot, "day:2026-11-04")
    tap(bot, "sv:2026-11-04:825")
    assert bot.store.points() == [(date(2026, 11, 4), 82.5)]
    assert "04.11.2026" in bot.fake.last("sendPhoto")[1]["caption"] and bot.fake.count("deleteMessage") == 1


def test_stepper_stays_within_possible_weights(bot):
    tap(bot, f"st:2026-11-04:{weight_bot.MAX_KG * 10}")
    assert f"st:2026-11-04:{weight_bot.MAX_KG * 10}" in buttons(bot.fake.last("editMessageText"))[:4]
    assert f"st:2026-11-04:{weight_bot.MAX_KG * 10 + 1}" not in buttons(bot.fake.last("editMessageText"))


def test_a_number_typed_after_picking_a_day_goes_to_that_day_then_back_to_today(bot):
    tap(bot, "day:2026-11-04")
    text(bot, "82,4")
    assert bot.store.points() == [(date(2026, 11, 4), 82.4)] and bot.fake.count("deleteMessage") == 1
    text(bot, "82,2")
    assert bot.store.points()[-1] == (TODAY, 82.2)


def test_a_forgotten_day_choice_expires_so_tomorrows_weight_lands_on_tomorrow(bot):
    tap(bot, "day:2026-11-04")
    bot.clock["now"] += timedelta(minutes=11)
    text(bot, "82,4")
    assert bot.store.points() == [(TODAY, 82.4)]


def test_a_date_typed_with_the_number_beats_the_picked_day_and_a_command_cancels_it(bot):
    tap(bot, "day:2026-11-04")
    text(bot, "82,4 wczoraj")
    assert bot.store.points() == [(date(2026, 11, 5), 82.4)]
    tap(bot, "day:2026-11-04")
    text(bot, "/menu")
    text(bot, "82,1")
    assert bot.store.points()[-1] == (TODAY, 82.1)


def test_recent_tiles_open_the_stepper_with_delete(bot):
    fill(bot, [80.0, 81.5])
    tap(bot, "m:recent")
    edit = bot.fake.last("editMessageText")
    assert_fits(edit)
    assert buttons(edit)[:2] == ["day:2026-11-06", "day:2026-11-05"]
    tap(bot, "day:2026-11-05")
    edit = bot.fake.last("editMessageText")
    assert "80,0 kg" in edit[1]["text"] and "dx:2026-11-05" in buttons(edit)
    tap(bot, "dx:2026-11-05")
    assert bot.store.points() == [(TODAY, 81.5)] and "Usunięto" in bot.fake.last("editMessageText")[1]["text"]


def test_a_day_without_a_measurement_has_nothing_to_delete(bot):
    tap(bot, "day:2026-11-04")
    assert not any(d.startswith("dx:") for d in buttons(bot.fake.last("editMessageText")))


def test_recent_tiles_page_through_older_measurements(bot):
    fill(bot, [80.0 + i / 10 for i in range(10)])
    tap(bot, "m:recent")
    first = buttons(bot.fake.last("editMessageText"))
    assert "rec:7" in first and first.count("rec:0") == 0
    tap(bot, "rec:7")
    second = buttons(bot.fake.last("editMessageText"))
    assert "day:2026-10-28" in second and "rec:0" in second and "rec:14" not in second


def test_waga_alone_opens_the_day_picker(bot):
    text(bot, "/waga")
    assert buttons(bot.fake.last("sendMessage"))[0] == "day:2026-11-06"


def test_a_keyboard_never_has_an_empty_row(bot):
    fill(bot, [80.0, 81.5])  # fits one screen: no older or newer page
    tap(bot, "m:recent")
    rows = json.loads(bot.fake.last("editMessageText")[1]["reply_markup"])["inline_keyboard"]
    assert all(rows)


def test_typing_a_date_ends_the_day_choice(bot):
    tap(bot, "day:2026-11-04")
    text(bot, "82,4 wczoraj")
    text(bot, "82,2")
    assert bot.store.points()[-1] == (TODAY, 82.2) and (date(2026, 11, 4), 82.2) not in bot.store.points()


def test_leaving_the_stepper_by_a_button_ends_the_day_choice(bot):
    tap(bot, "day:2026-11-04")
    tap(bot, "m:menu")
    text(bot, "82,1")
    assert bot.store.points() == [(TODAY, 82.1)]


def test_telegram_saying_not_modified_is_not_an_error_but_others_are(bot):
    def answers(error):
        def api(method, fields=None, files=None):
            if method == "editMessageText":
                raise weight_bot.TelegramError(error)
            return {}
        return api

    bot.api = answers("editMessageText: Bad Request: message is not modified")
    tap(bot, "st:2026-11-04:3000")  # + at the top of the range changes nothing
    bot.api = answers("editMessageText: Forbidden")
    with pytest.raises(weight_bot.TelegramError):
        tap(bot, "st:2026-11-04:3000")


# --- updating itself from Telegram -----------------------------------------------------------

def newer(version=None):
    major, minor, patch = weight_bot.version_tuple(weight_bot.VERSION)
    return version or f"v{major}.{minor}.{patch + 1}"


def test_update_tile_offers_a_newer_version_and_installs_it(bot):
    bot.fake_updater.tag = newer()
    tap(bot, "m:update")
    edit = bot.fake.last("editMessageText")
    assert_fits(edit)
    assert weight_bot.VERSION in edit[1]["text"] and newer()[1:] in edit[1]["text"]
    assert f"up:{newer()}" in buttons(edit) and "rb" not in buttons(edit)
    tap(bot, f"up:{newer()}")
    assert bot.fake_updater.installed == [newer()] and bot.restart
    assert bot.store.get("updated_from") == weight_bot.VERSION


def test_update_tile_says_when_there_is_nothing_new(bot):
    tap(bot, "m:update")
    edit = bot.fake.last("editMessageText")
    assert "najnowsz" in edit[1]["text"] and not any(d.startswith("up:") for d in buttons(edit))


def test_failed_update_changes_nothing_and_says_why(bot):
    bot.fake_updater.tag = newer()
    bot.fake_updater.failure = weight_bot.UpdateError("selftest nie przeszedł")
    tap(bot, f"up:{newer()}")
    assert not bot.restart and bot.store.get("updated_from") is None
    assert "selftest nie przeszedł" in bot.fake.last("editMessageText")[1]["text"]


def test_no_connection_to_github_is_explained_on_the_tile(bot):
    bot.fake_updater.failure = weight_bot.UpdateError("brak połączenia z GitHubem")
    tap(bot, "m:update")
    assert "brak połączenia" in bot.fake.last("editMessageText")[1]["text"]


def test_rollback_is_offered_only_when_possible_and_restarts(bot):
    bot.fake_updater.can_roll_back = True
    tap(bot, "m:update")
    assert "rb" in buttons(bot.fake.last("editMessageText"))
    tap(bot, "rb")
    assert bot.fake_updater.rolled_back == 1 and bot.restart


def test_typed_update_and_rollback_commands(bot):
    bot.fake_updater.tag = newer()
    text(bot, "/update")
    assert bot.fake_updater.installed == [newer()] and bot.restart
    bot.restart = False
    text(bot, "/rollback")
    assert bot.fake_updater.rolled_back == 0 and "Nie ma" in bot.fake.texts()[-1]


def test_a_new_version_is_announced_once_with_a_button(bot):
    bot.fake_updater.tag = newer()
    bot.tick()
    bot.tick()
    assert bot.fake.count("sendMessage") == 1 and bot.fake_updater.checks == 1  # once a day, not every poll
    assert f"up:{newer()}" in buttons(bot.fake.last("sendMessage"))
    bot.clock["now"] += timedelta(days=1)
    bot.tick()
    assert bot.fake.count("sendMessage") == 1 and bot.fake_updater.checks == 2  # the same version: no second message
    bot.fake_updater.tag = newer("v99.0.0")
    bot.clock["now"] += timedelta(days=1)
    bot.tick()
    assert bot.fake.count("sendMessage") == 2


def test_daily_check_failing_stays_quiet(bot):
    bot.fake_updater.failure = weight_bot.UpdateError("brak połączenia")
    bot.tick()
    assert bot.fake.calls == []


def test_after_restart_the_bot_says_what_changed(bot):
    bot.store.set("updated_from", "1.0.0")
    bot.announce_update()
    assert "1.0.0" in bot.fake.texts()[-1] and weight_bot.VERSION in bot.fake.texts()[-1]
    calls = len(bot.fake.calls)
    bot.announce_update()
    assert len(bot.fake.calls) == calls


def test_restart_confirms_received_updates_first_so_it_does_not_install_twice(bot):
    bot.fake_updater.tag = newer()
    moment = bot.clock["now"]
    message = message_update(5, "/update", moment)
    api = ScriptedApi([[message], []])
    bot.api = api
    with pytest.raises(SystemExit):
        weight_bot.run(api, bot)
    assert api.offsets[-1] == 6 and bot.fake_updater.installed == [newer()]


# --- the real mechanism: files, selftest, starting the newest copy ------------------------------

SOURCE = open(weight_bot.__file__, encoding="utf-8").read()


def source_of(version):
    return SOURCE.replace(f'VERSION = "{weight_bot.VERSION}"', f'VERSION = "{version}"').encode()


def real_updater(tmp_path, responses):
    def fetch(url):
        for fragment, body in responses.items():
            if fragment in url:
                return body
        raise weight_bot.UpdateError("brak połączenia z GitHubem")

    return weight_bot.Updater(tmp_path / "update", fetch)


def test_latest_reads_the_release_tag(tmp_path):
    updater = real_updater(tmp_path, {"releases/latest": b'{"tag_name": "v1.2.3"}'})
    assert updater.latest() == "v1.2.3"
    for bad in (b'{"tag_name": "../../evil"}', b"not json", b"{}"):
        with pytest.raises(weight_bot.UpdateError):
            real_updater(tmp_path, {"releases/latest": bad}).latest()


def test_install_runs_the_selftest_then_puts_the_copy_in_place(tmp_path):
    updater = real_updater(tmp_path, {"/v9.9.9/weight_bot.py": source_of("9.9.9")})
    updater.install("v9.9.9")
    assert (tmp_path / "update" / "weight_bot.py").read_bytes() == source_of("9.9.9")
    assert updater.can_roll_back


def test_a_broken_or_wrong_download_is_refused_and_leaves_the_old_copy(tmp_path):
    good = real_updater(tmp_path, {"/v9.9.9/weight_bot.py": source_of("9.9.9")})
    good.install("v9.9.9")
    crashes = b'VERSION = "9.9.10"\nimport does_not_exist_anywhere\n'  # the right number, but it can't even start
    for body in (crashes, b"", source_of("9.9.8")):
        updater = real_updater(tmp_path, {"/v9.9.10/weight_bot.py": body})
        with pytest.raises(weight_bot.UpdateError):
            updater.install("v9.9.10")
        assert (tmp_path / "update" / "weight_bot.py").read_bytes() == source_of("9.9.9")
    assert not (tmp_path / "update" / "weight_bot.py.new").exists()


def test_rollback_goes_one_step_back_then_to_the_installed_version(tmp_path):
    updater = real_updater(tmp_path, {"/v9.9.9/": source_of("9.9.9"), "/v9.9.10/": source_of("9.9.10")})
    updater.install("v9.9.9")
    updater.install("v9.9.10")
    updater.rollback()
    assert (tmp_path / "update" / "weight_bot.py").read_bytes() == source_of("9.9.9")
    updater.rollback()
    assert not (tmp_path / "update" / "weight_bot.py").exists() and not updater.can_roll_back
    with pytest.raises(weight_bot.UpdateError):
        updater.rollback()


def start_main(tmp_path, monkeypatch):
    started, ran = [], []
    monkeypatch.setenv("TG_TOKEN", "1:x")
    monkeypatch.setenv("TG_OWNER_ID", "100")
    monkeypatch.setenv("WEIGHT_DB", str(tmp_path / "weight.db"))
    monkeypatch.setattr(weight_bot.os, "execv", lambda path, args: started.append(args[1]))
    monkeypatch.setattr(weight_bot, "run", lambda api, bot: ran.append(True))
    weight_bot.main([])
    return started, ran


def test_start_prefers_a_newer_downloaded_copy(tmp_path, monkeypatch):
    copy = tmp_path / "update" / "weight_bot.py"
    copy.parent.mkdir()
    copy.write_bytes(source_of("99.0.0"))
    started, _ = start_main(tmp_path, monkeypatch)
    assert started == [str(copy)]


@pytest.mark.parametrize("version", [weight_bot.VERSION, "0.0.1"])
def test_start_ignores_a_copy_that_is_not_newer(tmp_path, monkeypatch, version):
    copy = tmp_path / "update" / "weight_bot.py"  # the running copy itself, or an older one left after an install
    copy.parent.mkdir()
    copy.write_bytes(source_of(version))
    started, ran = start_main(tmp_path, monkeypatch)
    assert started == [] and ran == [True]


def test_version_compares_as_numbers_not_text():
    assert weight_bot.version_tuple("v1.10.0") > weight_bot.version_tuple("1.9.0")


def test_typed_update_when_already_newest_installs_nothing(bot):
    text(bot, "/update")
    assert bot.fake_updater.installed == [] and not bot.restart and "najnowsz" in bot.fake.texts()[-1]
