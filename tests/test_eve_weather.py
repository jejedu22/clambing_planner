from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.models import Outing
from app.services import notifications, weather
from .conftest import client_for, csrf, enable_mailjet, recipients


def payload(start: date, rains):
    n = len(rains)
    return {"daily": {
        "time": [(start + timedelta(days=i)).isoformat() for i in range(n)],
        "weather_code": [63 if r >= 1 else 1 for r in rains], "temperature_2m_max": [12] * n,
        "temperature_2m_min": [4] * n, "precipitation_sum": rains, "precipitation_probability_max": [10] * n,
        "wind_speed_10m_max": [10] * n}}


def test_each_day_knows_its_eve():
    days = weather.parse_daily(payload(date(2026, 6, 1), [8.0, 0.0, 0.0]))
    assert days[0]["eve"] is None                                         # premier jour : pas de veille connue
    assert days[1]["eve"]["date"] == date(2026, 6, 1) and days[1]["eve"]["rain"] == 8.0
    assert days[2]["eve"]["rain"] == 0.0 and "eve" not in days[2]["eve"]   # pas d'imbrication


def fake_forecast(monkeypatch, rains_before_and_on):
    """La sortie a lieu dans 3 jours ; on contrôle la pluie de la veille et du jour."""
    start = date.today()
    days = weather.parse_daily(payload(start, [0.0, 0.0] + rains_before_and_on))
    monkeypatch.setattr(weather, "get_forecast", lambda lat, lon: days)
    return start + timedelta(days=3)


def create(c, day, title="Sortie"):
    c.post("/outings/new", data={"title": title, "site_id": 1, "day": day.isoformat(), "csrf": csrf(c, "/outings/new")})


def test_outing_page_shows_eve_and_day(world, db, monkeypatch):
    day = fake_forecast(monkeypatch, [6.0, 0.0])
    a = client_for("admin_a@test.org"); create(a, day)
    page = a.get(f"/outings/{db.query(Outing).one().id}").text
    assert "Veille" in page and "Jour de la sortie" in page and "6.0 mm" in page


def test_agenda_flags_wet_eve(world, db, monkeypatch):
    day = fake_forecast(monkeypatch, [6.0, 0.0])
    a = client_for("admin_a@test.org"); create(a, day)
    page = a.get("/").text
    assert "Veille :" in page and "6.0 mm" in page and "rocher peut-être humide" in page and 'eve-line wet' in page


def test_agenda_dry_eve_is_quiet(world, db, monkeypatch):
    day = fake_forecast(monkeypatch, [0.0, 0.0])
    a = client_for("admin_a@test.org"); create(a, day)
    page = a.get("/").text
    assert "Veille :" in page and "0.0 mm" in page and "rocher peut-être humide" not in page


def test_reminder_mentions_eve(world, db, outbox, monkeypatch):
    enable_mailjet(db, world["a"].id)
    tomorrow = date.today() + timedelta(days=1)
    days = weather.parse_daily(payload(date.today() - timedelta(days=1), [0.0, 4.5, 0.0]))   # veille = aujourd'hui
    monkeypatch.setattr(weather, "get_forecast", lambda lat, lon: days)
    a = client_for("admin_a@test.org"); create(a, tomorrow)
    m = client_for("member_a@test.org")
    m.post(f"/outings/{db.query(Outing).one().id}/respond", data={"status": "yes", "csrf": csrf(m)})
    outbox.clear()
    noon = datetime.now(notifications.ZoneInfo("Europe/Paris")).replace(hour=12)
    monkeypatch.setattr(notifications, "datetime", SimpleNamespace(now=lambda tz: noon))
    assert notifications.send_reminders() == 1 and recipients(outbox) == ["member_a@test.org"]
    text = outbox[0]["messages"][0]["TextPart"]
    assert "Veille : " in text and "4.5 mm de pluie" in text and "rocher peut-être humide" in text
