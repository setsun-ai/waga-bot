"""The bot as its owner uses it: messages and button taps go in, what Telegram would show comes out."""
import http.server
import io
import json
import threading
from datetime import date, datetime, timedelta

import pytest
from PIL import Image

import waga

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


@pytest.fixture
def bot(tmp_path):
    clock = {"now": datetime(2026, 11, 6, 9, 0, tzinfo=waga.TZ)}
    api = FakeApi()
    b = waga.Bot(waga.Store(tmp_path / "waga.db"), api, OWNER, now=lambda: clock["now"])
    b.fake, b.clock = api, clock
    return b


def text(bot, value, uid=OWNER, chat=None, when=None, edited=False):
    moment = when or bot.clock["now"]
    message = {"message_id": 1, "date": int(moment.timestamp()), "text": value, "from": {"id": uid},
               "chat": {"id": chat if chat is not None else uid, "type": "private" if (chat or uid) > 0 else "group"}}
    bot.handle_update({"edited_message" if edited else "message": message})


def tap(bot, data, uid=OWNER):
    bot.handle_update({"callback_query": {"id": "q", "data": data, "from": {"id": uid},
                                          "message": {"message_id": 7, "chat": {"id": uid, "type": "private"}}}})


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
    assert waga.parse_entry(typed, TODAY) == expected


@pytest.mark.parametrize("typed", ["hej", "", "czesc 82", "82,4 kiedys tam"])
def test_other_text_is_not_a_weight(typed):
    assert waga.parse_entry(typed, TODAY) is None


@pytest.mark.parametrize("typed", ["8,24", "824", "0", "82,4 31.02", "82,4 2027-01-01"])
def test_impossible_values_are_refused(typed):
    with pytest.raises(waga.Invalid):
        waga.parse_entry(typed, TODAY)


# --- BMI, trend, tempo ----------------------------------------------------------------------------

@pytest.mark.parametrize("kg, name", [(55, "niedowaga"), (60, "norma"), (80.6, "norma"), (81, "nadwaga"),
                                      (97, "nadwaga"), (98, "otyłość")])
def test_bmi_zones_at_180_cm(kg, name):
    assert waga.zone(waga.bmi(kg, 180)).name == name


def test_healthy_range_for_a_height():
    assert waga.healthy_range(180) == (59.9, 80.7)


def test_trend_is_the_mean_of_the_last_seven_days_not_seven_points():
    points = [(TODAY - timedelta(days=20), 90.0), (TODAY - timedelta(days=1), 80.0), (TODAY, 82.0)]
    assert waga.trend(points) == [(points[0][0], 90.0), (points[1][0], 80.0), (TODAY, 81.0)]


def test_tempo_needs_two_weeks_of_data():
    assert waga.rate_per_week([(TODAY - timedelta(days=d), 80 + d * 0.1) for d in range(6, -1, -1)], TODAY) is None
    steady = [(TODAY - timedelta(days=d), 80 + d * 0.1) for d in range(20, -1, -1)]  # losing 0.1 kg a day
    assert waga.rate_per_week(steady, TODAY) == pytest.approx(-0.7)


def test_goal_date_only_when_heading_towards_it():
    falling = [(TODAY - timedelta(days=d), 80 + d * 0.1) for d in range(20, -1, -1)]
    assert waga.goal_date(falling, 73, TODAY) == TODAY + timedelta(days=70)
    assert waga.goal_date(falling, 85, TODAY) is None
    assert waga.goal_date([(TODAY, 80.0)], 73, TODAY) is None


def test_sudden_jump_looks_like_a_typo_but_slow_change_does_not():
    points = [(TODAY - timedelta(days=1), 82.4)]
    assert waga.suspicious(28.4, TODAY, points)
    assert not waga.suspicious(83.0, TODAY, points)
    assert not waga.suspicious(78.0, TODAY, [(TODAY - timedelta(days=60), 85.0)])  # two months apart: plausible
    assert not waga.suspicious(28.4, TODAY, [])


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
    local_night = datetime(2026, 11, 6, 0, 30, tzinfo=waga.TZ)  # still 5 Nov in UTC
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
            assert waga.label_width(b["text"]) <= 42 // len(row) - 2, b["text"]


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
            assert waga.label_width(b["text"]) <= 42 // len(row) - 2, b["text"]
    tap(bot, "m:goal")
    assert "cel" in bot.fake.last("sendMessage")[1]["text"].lower()
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
    assert "tempo −0,7 kg/tydz." in waga.caption(bot.store.points(), TODAY, None, 73.0, TODAY)
    caption = waga.caption(bot.store.points(), TODAY, None, 73.0, TODAY)
    assert "zostało 7,0 kg" in caption and "15.01.2027" in caption


def test_waga_command_adds_a_past_measurement(bot):
    text(bot, "/waga 82,4 5.11")
    assert bot.store.points() == [(date(2026, 11, 5), 82.4)]


def test_recent_list(bot):
    fill(bot, [80.0, 81.5])
    text(bot, "/ostatnie")
    said = bot.fake.last("sendMessage")[1]["text"]
    assert "06.11" in said and "81,5" in said and said.index("06.11") < said.index("05.11")


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
    bot.clock["now"] = datetime(2026, 11, 6, 8, 0, tzinfo=waga.TZ)
    bot.tick()
    assert bot.fake.count("sendMessage") == before
    bot.clock["now"] = datetime(2026, 11, 6, 8, 31, tzinfo=waga.TZ)
    bot.tick()
    bot.tick()
    assert bot.fake.count("sendMessage") == before + 1
    bot.clock["now"] = datetime(2026, 11, 7, 9, 0, tzinfo=waga.TZ)
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
    bot.clock["now"] = datetime(2026, 11, 8, 19, 0, tzinfo=waga.TZ)
    bot.tick()
    assert bot.fake.count("sendDocument") == 0
    bot.clock["now"] = datetime(2026, 11, 8, 20, 5, tzinfo=waga.TZ)
    bot.tick()
    bot.tick()
    method, fields, files = bot.fake.last("sendDocument")
    assert bot.fake.count("sendDocument") == 1 and fields["disable_notification"] == "true"
    assert files["document"].startswith(b"data,kg\n2026-10-26,80.0\n")
    assert "81,0" in fields["caption"]  # the week's mean, and how it changed
    assert "−0,7" in fields["caption"] or "+0,7" in fields["caption"]


def test_no_copy_without_data(bot):
    bot.clock["now"] = datetime(2026, 11, 8, 21, 0, tzinfo=waga.TZ)
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
                                   "document": {"file_id": "f1", "file_name": "waga.csv", "file_size": 60}}})
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
    with_bar = picture(waga.render_chart(points, 30, 180, None, TODAY, TODAY))
    without = picture(waga.render_chart(points, 30, None, None, TODAY, TODAY))
    assert with_bar.size == without.size == (waga.WIDTH, waga.HEIGHT)
    zone_colours = [z.colour for z in waga.ZONES]
    assert all(has_colour(with_bar, c) for c in zone_colours)
    assert not any(has_colour(without, c) for c in zone_colours)


def test_chart_copes_with_one_point_flat_data_and_old_points():
    picture(waga.render_chart([(TODAY, 82.4)], 30, 180, None, TODAY, TODAY))
    picture(waga.render_chart([(TODAY, 82.0), (TODAY - timedelta(days=1), 82.0)], 14, 180, 78.0, TODAY, TODAY))
    picture(waga.render_chart([(TODAY - timedelta(days=400), 82.0)], 14, None, None, TODAY, TODAY))
    picture(waga.render_chart([(TODAY - timedelta(days=d), 200 - d) for d in range(150)], 0, 150, None, TODAY, TODAY))


def test_highlighted_day_is_marked_on_the_chart():
    points = [(TODAY - timedelta(days=1), 80.0), (TODAY, 81.0)]
    assert has_colour(picture(waga.render_chart(points, 30, None, None, TODAY, TODAY)), waga.HIGHLIGHT)


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
    api = waga.Telegram("123:SECRET", base=f"http://127.0.0.1:{server.server_port}")
    assert api("sendPhoto", {"chat_id": 5, "caption": "cześć"}, {"photo": b"\x89PNGdata"}) == {"x": 1}
    assert seen["path"] == "/bot123:SECRET/sendPhoto" and seen["type"].startswith("multipart/form-data")
    assert b'name="chat_id"\r\n\r\n5' in seen["body"] and "cześć".encode() in seen["body"]
    assert b'name="photo"; filename="photo.png"' in seen["body"] and b"\x89PNGdata" in seen["body"]
    with pytest.raises(waga.TelegramError) as error:
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
    assert waga.chart_window(points, 30, TODAY)[0] == TODAY - timedelta(days=6)  # not 30 empty days
    long = [(TODAY - timedelta(days=d), 80.0) for d in range(100, -1, -1)]
    assert waga.chart_window(long, 30, TODAY)[0] == TODAY - timedelta(days=29)
    low, high = waga.value_scale([82.4, 82.5], None)
    assert high - low >= 3 and low < 82.4 and high > 82.5


def test_selftest_renders_a_chart_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("TG_TOKEN", raising=False)
    assert waga.main(["--selftest"]) == 0
    assert waga.VERSION in capsys.readouterr().out


def test_selftest_fails_when_the_chart_cannot_be_drawn(monkeypatch):
    def broken(*args):
        raise OSError("no font")

    monkeypatch.setattr(waga, "render_chart", broken)
    assert waga.main(["--selftest"]) == 1


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
        waga.run(api, bot)
    assert api.offsets == [None, 6, 7]  # never asks for the broken update again
    assert any("Coś nie wyszło" in said for said in api.texts()) and api.count("sendPhoto") == 1
    assert bot.store.points() == [(TODAY, 82.1)]
    out = capsys.readouterr().out
    assert "82" not in out and "KeyError" in out


def test_a_network_failure_only_delays_the_bot(bot, monkeypatch):
    sleeps = []
    monkeypatch.setattr(waga.time, "sleep", sleeps.append)
    api = ScriptedApi([waga.TelegramError("getUpdates: URLError"), waga.TelegramError("getUpdates: URLError"),
                       [message_update(1, "82,4", bot.clock["now"])]])
    bot.api = api
    with pytest.raises(KeyboardInterrupt):
        waga.run(api, bot)
    assert sleeps == [5, 10] and bot.store.points() == [(TODAY, 82.4)]


def test_whoami_prints_the_id_or_explains(capsys):
    waga.whoami(ScriptedApi([[message_update(1, "hej", datetime(2026, 11, 6, tzinfo=waga.TZ))]]))
    assert capsys.readouterr().out.strip() == str(OWNER)
    waga.whoami(ScriptedApi([waga.TelegramError("getUpdates: Unauthorized")]))
    assert "Unauthorized" in capsys.readouterr().out


def test_chart_headline_shows_the_day_it_is_about():
    points = [(TODAY - timedelta(days=1), 80.0), (TODAY, 91.0)]
    header = (0, 0, 400, 80)
    about_today = picture(waga.render_chart(points, 30, None, None, TODAY, TODAY)).crop(header)
    about_yesterday = picture(waga.render_chart(points, 30, None, None, TODAY - timedelta(days=1), TODAY)).crop(header)
    assert about_today.tobytes() != about_yesterday.tobytes()


def test_correcting_todays_typo_does_not_ask_again(bot):
    bot.store.put(TODAY, 42.8)
    text(bot, "82,4")
    assert bot.store.points() == [(TODAY, 82.4)] and bot.fake.count("sendPhoto") == 1


def test_data_file_is_readable_only_by_its_owner(tmp_path, monkeypatch):
    import os
    import stat

    db = tmp_path / "state" / "waga.db"
    monkeypatch.setenv("TG_TOKEN", "1:x")
    monkeypatch.setenv("TG_OWNER_ID", "100")
    monkeypatch.setenv("WAGA_DB", str(db))
    monkeypatch.setattr(waga, "run", lambda api, bot: None)
    before = os.umask(0o022)
    try:
        waga.main([])
    finally:
        os.umask(before)
    assert stat.S_IMODE(db.stat().st_mode) & 0o077 == 0
