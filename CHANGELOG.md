# Changelog

## 1.3.0 (2026-11)

- **Wartości przy punktach wykresu.** Do 16 pomiarów (np. widok 14 dni) każdy ma podpisaną wagę; na dłuższym wykresie tylko dzień, którego dotyczy wykres (na czerwono), najniższy i najwyższy. Podpisy nie nachodzą na siebie ani na punkty i nie wychodzą poza wykres.

## 1.2.0 (2026-11)

- **🔄 Aktualizacja w menu** (także `/update` i `/rollback`): bot pokazuje swoją wersję i najnowszą, pobiera nową, sprawdza ją selftestem, uruchamia się ponownie i daje znać. Gdy selftest nie przejdzie, działa dalej stara wersja. Przycisk **⏪ Wróć do poprzedniej** cofa o krok.
- Raz dziennie bot sprawdza, czy jest nowa wersja, i pisze o każdej raz, z przyciskiem instalacji.
- Instalator kasuje pobraną kopię, więc ręczna instalacja zawsze naprawia bota, gdyby nowa wersja nie chciała wystartować.

Aktualizacja do tej wersji wymaga jeszcze raz ręcznego `git pull` i instalatora; następne już z przycisku.

## 1.1.0 (2026-11)

- Menu z kafelków ze wszystkimi akcjami, otwierane w miejscu, z ↩️ Menu.
- **⚖️ Dodaj pomiar** bez pisania daty: Dziś, Wczoraj, Przedwczoraj albo kalendarz, a waga przyciskami lub wpisana.
- **📋 Ostatnie** jako przyciski: stuknięcie otwiera poprawianie i usuwanie.

## 1.0.0 (2026-11)

- Pierwsza wersja: waga wysłana na czat wraca jako wykres ze średnią z 7 dni i paskiem BMI, zakresy, cel, przypomnienie, kopia CSV.
