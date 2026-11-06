# waga-bot

Prywatny bot na Telegramie: wysyłasz wagę, a on odsyła wykres z trendem i paskiem BMI.

Jeden plik (`waga.py`), biblioteka standardowa + Pillow. Odpowiada tylko właścicielowi, tylko na czacie prywatnym.

## Jak się używa

- `82,4` – dzisiejszy pomiar. Działa też `82.4`, `82,4 kg`, `82,4 wczoraj`, `82,4 5.11`, `82,4 2026-11-05`.
- Drugi pomiar tego samego dnia zastępuje pierwszy. Edycja wiadomości też poprawia pomiar.
- Duży skok (literówka?) bot najpierw potwierdza przyciskiem.
- Pod wykresem: zakres (14 d, 30 d, 90 d, rok, całość) i usunięcie pomiaru.
- `/menu` – kafelki. Komendy: `/wzrost 180`, `/cel 78`, `/przypomnienie 8:00` (`off` wyłącza), `/wykres`, `/ostatnie`, `/csv`, `/waga 82,4 5.11`, `/help`.

W niedzielę wieczorem bot wysyła podsumowanie tygodnia z kopią danych w CSV. Odesłanie tego pliku przywraca dane.

Data pomiaru to chwila wysłania wiadomości, więc po przerwie w działaniu laptopa wykres się zgadza (Telegram trzyma wiadomości przez około dobę).

## Prywatność

- Pomiary są w jednym pliku SQLite na maszynie z botem, czytelnym tylko dla użytkownika usługi. Bot nie wysyła ich nikomu poza Tobą i nie zapisuje w logach.
- Telegram widzi to, co wysyłasz i dostajesz: wiadomości, wykresy i kopie CSV.
- Plik nie jest szyfrowany. Ktoś z uprawnieniami administratora albo fizycznym dostępem do dysku może go odczytać.
- Repozytorium zawiera tylko kod: `*.db`, `*.csv` i `*.env` są w `.gitignore`.

BMI to ogólna skala WHO dla dorosłych: nie odróżnia mięśni od tłuszczu. Bot nie śledzi celu, który oznacza niedowagę.

## Instalacja (Ubuntu/Debian, systemd)

1. W [@BotFather](https://t.me/BotFather) utwórz bota (`/newbot`).
2. Skopiuj pliki na maszynę i uruchom instalator:

```bash
scp waga.py deploy/install.sh deploy/waga-bot.service UZYTKOWNIK@MASZYNA:/tmp/
```

```bash
ssh -t UZYTKOWNIK@MASZYNA sudo bash /tmp/install.sh
```

Instalator pyta o token (nie pokazuje go na ekranie), rozpoznaje Twoje ID po wiadomości do bota i uruchamia usługę. Token trafia tylko do `/etc/waga-bot.env` (chmod 600). Aktualizacja to te same dwie komendy: dane i ustawienia zostają.

Usługa ma limit 50% CPU i 200 MB RAM oraz niski priorytet, żeby nie przeszkadzać innym programom na tej maszynie.

Stan usługi:

```bash
systemctl status waga-bot
```

## Rozwój

```bash
pip install -r requirements-dev.txt
```

```bash
python -m ruff check . && python -m pytest -q
```

Testy nie łączą się z Telegramem. Działają na Pythonie 3.10–3.13 i Pillow od 9.0.
