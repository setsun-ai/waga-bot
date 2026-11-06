# weight-bot

Prywatny bot na Telegramie: wysyłasz wagę, a on odsyła wykres z trendem i paskiem BMI.

Jeden plik (`weight_bot.py`), biblioteka standardowa + Pillow. Odpowiada tylko właścicielowi, tylko na czacie prywatnym.

## Jak się używa

- `82,4` – dzisiejszy pomiar. Działa też `82.4`, `82,4 kg`, `82,4 wczoraj`, `82,4 5.11`, `82,4 2026-11-05`.
- `/menu` → **⚖️ Dodaj pomiar** – bez pisania daty: Dziś, Wczoraj, Przedwczoraj albo kalendarz (• oznacza dzień z pomiarem), a wagę ustawiasz przyciskami `−1`, `−0,1`, `+0,1`, `+1` albo wpisujesz liczbę. Wybrany dzień czeka na liczbę 10 minut, potem wpisana liczba znowu oznacza dziś.
- **📋 Ostatnie** – lista pomiarów; stuknięcie otwiera poprawianie i usuwanie.
- Drugi pomiar tego samego dnia zastępuje pierwszy. Edycja wiadomości też poprawia pomiar.
- Duży skok (literówka?) bot najpierw potwierdza przyciskiem.
- Pod wykresem: zakres (14 d, 30 d, 90 d, rok, całość) i usunięcie pomiaru.
- `/menu` – wszystko jako kafelki, otwierają się w miejscu z ↩️ Menu. Komendy dalej działają: `/wzrost 180`, `/cel 78`, `/przypomnienie 8:00` (`off` wyłącza), `/wykres`, `/ostatnie`, `/csv`, `/waga`, `/help`.

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
2. Pobierz kod na maszynę i uruchom instalator:

```bash
git clone https://github.com/setsun-ai/weight-bot.git
```

```bash
sudo bash weight-bot/deploy/install.sh
```

Instalator pyta o token (nie pokazuje go na ekranie), rozpoznaje Twoje ID po wiadomości do bota i uruchamia usługę. Token trafia tylko do `/etc/weight-bot.env` (chmod 600). Dane i ustawienia zostają przy każdej aktualizacji.

## Aktualizacja

W bocie: `/menu` → **🔄 Aktualizacja** albo `/update`. Bot pobiera najnowsze wydanie z GitHuba, uruchamia jego selftest i dopiero wtedy się przełącza; gdy selftest nie przejdzie, działa dalej stara wersja. **⏪ Wróć do poprzedniej** albo `/rollback` cofa o krok. O nowej wersji bot pisze sam, raz. Pobrany plik nie ma osobnego podpisu: bot ufa temu, co jest w wydaniu tego repozytorium.

Ręcznie, także gdy nowa wersja nie chce wystartować: `git -C weight-bot pull` i ten sam instalator.

Jeden raz, do wersji 1.2.0, trzeba to zrobić ręcznie: wcześniejsze wersje nie mają przycisku.

Usługa ma limit 50% CPU i 200 MB RAM oraz niski priorytet, żeby nie przeszkadzać innym programom na tej maszynie.

Stan usługi:

```bash
systemctl status weight-bot
```

## Użycie AI

Kod i dokumentację napisano z pomocą AI (Claude Code). Założenia, wybór rozwiązań i weryfikacja należą do autora.

## Rozwój

```bash
pip install -r requirements-dev.txt
```

```bash
python -m ruff check . && python -m pytest -q
```

Testy nie łączą się z Telegramem. Działają na Pythonie 3.10–3.13 i Pillow od 9.0.

## Licencja

MIT.
