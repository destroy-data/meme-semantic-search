# Checklista ręcznej weryfikacji

Projekt nie ma jeszcze automatycznego test suite. Ta lista definiuje minimum,
które należy przejść na małej, kontrolowanej kolekcji przed pełnym indeksowaniem
albo po zmianie dowolnego etapu pipeline'u.

## Przygotowanie

- [ ] Pracuj na kopii kilkunastu obrazów, nie na właściwej kolekcji. Ustaw w
  testowym TOML osobne `db_path` oraz `image_dir`.
- [ ] W próbce umieść: kilka niezależnych memów, dwie identyczne kopie pliku,
  parę wyraźnie podobnych obrazów, jeden uszkodzony plik z rozszerzeniem obrazu
  oraz co najmniej jeden mem po polsku i po angielsku.
- [ ] Do ręcznej oceny opisów nie używaj trzech „spalonych” próbek wymienionych
  w `AGENTS.md`: `045f3bf5`, `0be81bf3`, `b8bbfce6`.
- [ ] Uruchom `ruff format . && ruff check .`; oba polecenia muszą przejść bez
  zmian i błędów.

## Skanowanie i deduplikacja

```sh
uv run memes.py scan --config config.test.toml
```

- [ ] Liczba `new` odpowiada liczbie różnych zawartości plików; identyczne
  kopie nie tworzą dodatkowych wierszy w `memes`, ale każda ma osobny wpis
  w `meme_files`. Drugi skan zachowuje wszystkie ścieżki i ścieżkę główną.
- [ ] Ustaw `image_dir` relatywnie, a potem sprawdź w SQLite, że każda wartość
  `memes.path` jest ścieżką bezwzględną.
- [ ] Uszkodzony obraz jest zgłoszony jako pominięty, ale nie przerywa skanu.
- [ ] Drugi identyczny skan ma `0 new` i nie tworzy kolejnych rekordów.
- [ ] W SQLite każda grupa podobnych obrazów ma dokładnie jednego
  reprezentanta (`perceptual_duplicate_of IS NULL`), a reszta wskazuje jego
  hash.
- [ ] Zmień tymczasowo `phash_threshold`, uruchom
  `scan --regroup-only` i sprawdź, że zmieniają się tylko grupy, nie opisy ani
  wektory.
- [ ] Usuń główną identyczną kopię i wykonaj skan: inna istniejąca kopia
  zostaje główną, a hash, opisy i embedding pozostają. `--path` akceptuje
  również kopię, która nie jest główna. Po podmianie zawartości pliku jego
  wpis w `meme_files` wskazuje nowy hash, a stary mem korzysta z innej kopii.
- [ ] Usuń wszystkie kopie pliku testowego i uruchom `scan --prune-missing`; jego rekord,
  description, FTS i wektor znikają, a pozostała grupa jest ponownie poprawnie
  zgrupowana.

## Opisy VLM

```sh
uv run memes.py describe --config config.test.toml --limit 10 -v
```

- [ ] Opis powstaje tylko dla reprezentantów grup podobnych obrazów.
- [ ] Każdy zapisany JSON ma wszystkie pola ze `description_schema.json`, a
  `keywords` i `humor_type` spełniają limity oraz enum ze schematu.
- [ ] `transcription` zgadza się z tekstem na obrazku, a `the_clue` wyjaśnia
  konkretny żart, nie ogólnik typu „śmieszny obrazek”.
- [ ] Drugi bieg z tą samą konfiguracją nie wysyła ponownie opisanych memów.
- [ ] `--path plik.jpg -v` świadomie zastępuje tylko opis wybranego pliku dla
  aktywnego `(model, prompt_version, effort_level)`.
- [ ] Zmiana `prompt_version`, modelu lub effortu tworzy wariant opisu obok
  starego, zamiast go kasować.
- [ ] Błąd modelu lub pojedynczego pliku zapisuje `failed_describe` i nie
  zatrzymuje pozostałej kolejki; `--retry-failed` podejmuje go ponownie.
- [ ] Dla backendu `claude-code-cli` sprawdź osobno, że wynik trafia do
  `structured_output`, a trafienie w limit użycia nie jest zapisane jako
  `failed_describe`.

## Embedding i indeksy

```sh
uv run memes.py embed --config config.test.toml
```

- [ ] Każdy opisany reprezentant aktywnego `prompt_version` dostaje po jednym
  wierszu w `memes_vec` i `memes_fts`.
- [ ] W FTS znajdują się transkrypcja, tagi i typ humoru; w tekście embeddingu
  są scena oraz `the_clue`.
- [ ] Drugi bieg nie wywołuje endpointu embeddingów dla już osadzonych memów.
- [ ] Tymczasowo zatrzymaj endpoint embeddingów: błędny mem jest pomijany, a
  po przywróceniu endpointu następny bieg go uzupełnia.
- [ ] Po ręcznym wyczyszczeniu `memes_vec` i `memes_fts` ponowny embed buduje
  oba indeksy z wybranego wariantu opisów.

## Wyszukiwanie CLI

```sh
uv run memes.py search --config config.test.toml "opis albo cytat" -n 10
```

- [ ] Zapytanie po polsku znajduje angielski mem o tym samym znaczeniu i
  odwrotnie.
- [ ] Dokładny cytat lub nietypowe imię zwraca właściwy mem dzięki FTS.
- [ ] Opis sytuacji/joke mechanic (`the_clue`), bez słów widocznych na obrazku,
  zwraca sensowny wynik dzięki wektorom.
- [ ] Wyniki nie zawierają nie-reprezentantów z grup podobnych obrazów.
- [ ] Każda wypisana ścieżka istnieje i otwiera właściwy plik. `--show` nie
  zmienia kolejności ani zestawu wyników.

## GUI

```sh
uv run memes.py gui --config config.test.toml
```

1. [ ] W „Liście” kliknij „Embeduj opisane”. Przycisk działa na całej kolekcji,
   niezależnie od promptu, modelu, filtra i checkboxów. Pomija istniejące
   wektory i duplikaty niebędące reprezentantami. Dla każdego mema wybiera
   najnowszy udany opis ze wszystkich konfiguracji. Po ukończeniu tabela pokazuje nowe „w indeksie”,
   a wyszukiwanie uwzględnia nowe memy bez restartu.
2. [ ] Podczas embedowania widać postęp i liczbę błędów; można przeglądać
   kolekcję i przełączać tryby. Kolejny bieg i usuwanie są zablokowane.
   Błąd endpointu nie zatrzymuje pozostałych memów. Ponowne kliknięcie podejmuje
   tylko brakujące wektory; pusta kolejka i błąd uruchomienia mają jasny komunikat.

- [ ] Na górze, po prawej od pola wyszukiwania, jest przełącznik „Szukaj / Lista”.
  „Lista” pokazuje tabelę po lewej i wspólny podgląd po prawej. Wpisany tekst
  filtruje nazwy i ścieżki bez wywoływania modelu. Powrót do każdego trybu
  zachowuje jego zapytanie, podgląd, zaznaczenie i przewinięcie obu paneli.
- [ ] Między polem wyszukiwania a przełącznikiem jest suwak „FTS / Embeddingi”.
  Domyślne 50% zachowuje dotychczasowe sortowanie. Skrajnie w lewo: tylko FTS
  (działa też bez endpointu embeddingów), skrajnie w prawo: tylko embeddingi.
  Zmiana odświeża wyniki po krótkim opóźnieniu. Suwak znika w „Liście”, a jego
  pozycja i kolejność kontrolek przetrwają wielokrotne przełączanie trybów.
- [ ] Tabela zawiera nazwę, folder, liczbę identycznych kopii (wraz z bieżącym
  plikiem) i stan opisu dla aktywnej konfiguracji; obecność wektora jest osobna.
  Kliknięcie wiersza zmienia podgląd, checkbox tylko zaznaczenie pliku.
- [ ] Sprawdź wszystkie pięć filtrów. „Nieopisane” obejmuje brak próby dla
  aktywnej konfiguracji, „Błędy” jej nieudaną próbę, także jeśli inny wariant
  ma poprawny opis. „Duplikaty” zawiera rozwijane grupy co najmniej dwóch
  dostępnych plików, w tym grupy wyłącznie identycznych kopii. Liczebność grupy
  dotyczy wszystkich dostępnych plików, nawet po filtrowaniu ścieżki.
- [ ] Filtr, rozwinięte grupy i checkboxy przetrwają przejście do „Szukaj”
  i powrót. Brak dopasowań daje komunikat i usuwa niepasujący podgląd.
- [ ] Bez działającego Ollama lista, filtry i podgląd nadal działają.
- [ ] Po zmianach kolekcji przez CLI uruchom GUI ponownie, by odczytać nowe dane.
- [ ] Wpisanie zapytania uruchamia wyszukiwanie po krótkiej pauzie, a Enter
  wykonuje je od razu.
- [ ] Kliknięcie oraz strzałki zmieniają zaznaczenie, podgląd obrazu i opis
  tego samego wyniku.
- [ ] Page Down przełącza podgląd o 10 wyników dalej, a Page Up o 10 wstecz,
  także przy fokusie w polu zapytania. Lista przewija się do wybranego wyniku;
  skoki zatrzymują się na pierwszym i ostatnim wyniku.
- [ ] Jeśli mem ma wiele wariantów opisu, Tab oraz lista wariantów przełączają
  opis bez zmiany obrazu. Rozwinięta lista pozostaje otwarta przez kilka sekund
  bez klikania; można przesunąć mysz do innej pozycji i wybrać ją. Fokus wraca
  do zapytania dopiero po wyborze.
- [ ] Podmień zawartość pliku pod tą samą ścieżką bez skanowania. Wynik ze
  starym hashem nie może pojawić się na liście; status powinien zgłosić liczbę
  ukrytych nieaktualnych rekordów.
- [ ] Sprawdź podstawowe akcje: kopiowanie obrazka/ścieżki, otwieranie w
  przeglądarce, czyszczenie Esc oraz zmianę liczby wyników.
- [ ] Po błędzie wczytania, kopiowania lub otwierania jednego mema przejście
  do innego usuwa poprzedni komunikat. Jeśli kolejny mem też powoduje błąd,
  pasek statusu pokazuje nowy komunikat.
- [ ] Pasek statusu z „F1: skróty” jest widoczny bez przewijania, także przy
  długim opisie, wielu wynikach, długim komunikacie statusu, zmianie rozmiaru
  okna i podglądzie na całą szerokość. Przewijają się tylko panele zawartości.

### Podgląd duplikatów i identycznych kopii — dostępne w GUI

- [ ] Przyciski „Grupa duplikatów”, „Identyczne kopie” i „Wróć do wyniku”
  widać tylko, gdy grupa zawiera więcej niż jeden dostępny plik (podobny mem
  lub identyczną kopię). Dla pojedynczego mema i grupy z samymi nieaktualnymi
  dodatkowymi ścieżkami cały zestaw przycisków jest ukryty.
- [ ] Pod obrazem w wynikach i widokach duplikatów widać ścieżkę względem
  `image_dir`, także dla plików w podkatalogach. Plik poza `image_dir` ma
  czytelną ścieżkę względną z `..`.
- [ ] Z wybranego mema można otworzyć jego grupę, podejrzeć każdego członka
  i rozpoznać reprezentanta. Mem bez duplikatów ma jasny komunikat.
- [ ] Widoki grup obejmują także nie-reprezentantów oraz memy bez opisów
  i embeddingów; normalne wyszukiwanie nadal pokazuje tylko reprezentantów.
- [ ] „Identyczne kopie” pokazuje wszystkie dostępne zeskanowane ścieżki
  o tym samym hashu, także kopie niebędące główną. Mają wspólne warianty opisów.
- [ ] Po kliknięciu ścieżki podgląd, kopiowanie i otwieranie dotyczą tej kopii.
  Lista grupy/kopii jest w głównym oknie. „Wróć” przywraca podgląd, z którego
  otwarto grupę, także w „Liście”; Esc zamyka listę. Przełączenie trybu
  zachowuje otwartą grupę lub listę identycznych kopii.
- [ ] Nieaktualne ścieżki są ukrywane z komunikatem o ich liczbie. Baza bez
  `meme_files` nadal pozwala przeglądać znane grupy podobnych obrazów.

### Pozostałe rozszerzenie GUI — po implementacji

Usuwanie, przenoszenie, zmiana nazw i indeksowanie z GUI są odłożone.
Checkboxy obecnie tylko przechowują zaznaczenie.
Usuwanie sprawdzaj wyłącznie na kopii kolekcji przygotowanej do weryfikacji.

- [ ] W wynikach i grupach można wybrać jeden lub wiele memów do usunięcia.
  Z dysku znikają wyłącznie wskazane pliki, a ich wpisy w `meme_files`
  znikają z bazy. Rekord mema, opisy, FTS i wektor znikają dopiero po
  usunięciu ostatniej identycznej kopii. Lista i podgląd odświeżają się po operacji.
- [ ] Usunięcie reprezentanta pozostawia poprawne grupy z nowym reprezentantem;
  opisy i embeddingi ocalałych memów nie są kasowane ani generowane od nowa.
  Usunięcie całej grupy usuwa ją z widoku, a pozostawienie jednego członka
  sprawia, że przestaje być pokazywana jako grupa duplikatów.
- [ ] Przy błędzie usuwania pliku GUI wskazuje, którego pliku dotyczy błąd,
  zachowuje jego rekordy i poprawnie pokazuje pozostałe udane usunięcia.
- [ ] Grupy obejmują wszystkie zeskanowane identyczne kopie z `meme_files`,
  także grupę złożoną wyłącznie z identycznych plików. Liczebność oznacza
  liczbę fizycznych plików. Usunięcie jednej kopii zachowuje opisy i indeksy
  wspólnego hasha, dopóki istnieje inna kopia.

## Pełny przebieg i wznawianie

```sh
uv run memes.py index --config config.test.toml --limit 10
```

- [ ] Pierwszy bieg wykonuje kolejno scan, describe i embed.
- [ ] Przerwij `describe` po ukończeniu jednego obrazu, uruchom `index`
  ponownie i sprawdź, że już zapisane opisy oraz embeddingi nie są robione od
  nowa.
- [ ] Na koniec wykonaj kilka zapisanych przykładowych zapytań z `AGENTS.md` i
  oceń wyniki ręcznie, zanim uruchomisz indeksowanie całej kolekcji.

## Weryfikacja implementacji Lista (2026-10-09)

Na tymczasowej kolekcji sprawdzono programowo wszystkie filtry, liczebności
fizycznych kopii, brakujące/podmienione ścieżki i bazę bez `meme_files`.
Test z rzeczywistym oknem Dear PyGui i renderowaniem klatek przeszedł tworzenie
tabeli, callbacki podglądu i checkboxów, przełączanie trybów z osobnymi
zapytaniami, powrót z identycznych kopii, pusty wynik oraz widoczność F1.
Nie zastępuje to ręcznej oceny układu, klikania i przewijania dużej kolekcji.

## Izolacja Claude i OpenCode

1. Claude: uruchom rzeczywiste CLI z lokalnym endpointem Anthropic i fikcyjnym
   logowaniem. W żądaniu mają być obrazy base64 i wyłącznie `StructuredOutput`.
   Nie może być Read, Bash, narzędzi sieciowych/MCP ani ścieżek źródłowych.
   Sprawdź odczyt ostatniego JSONL `result`, `structured_output` i usage.
2. OpenCode V2: lokalny provider OpenAI-compatible, izolowane dane testowe.
   Żądanie ma zawierać `tools: []`, obrazy i schemat; instrukcje systemowe mają
   zawierać dokładny prompt. `cwd` i `PWD` wskazują ten sam katalog tymczasowy.
   Ustawienia repozytorium, globalne AGENTS.md, MCP, skills i zewnętrzne pluginy
   nie mogą trafiać do żądania. CLI zachowuje własną obsługę logowania i sesji.
   Sprawdź też, że OPENCODE_CONFIG_DIR i XDG_CONFIG_HOME są tymczasowe, a
   OPENCODE_CONFIG oraz OPENCODE_CLI_CONFIG_CONTENT nie są dziedziczone.
   Monitor inotify na prawdziwym ~/.config/opencode nie może zgłosić otwarć
   ani odczytów podczas działania klienta i serwera.
3. Sprawdź statyczny obraz, kolejność i timestampy klatek oraz pliki ze spacjami.
   OpenCode skaluje tylko obrazy statyczne do 1440; Python przekazuje Claude
   natywny PNG, ale jego CLI może dodatkowo zmniejszać obrazy.
4. W testowej bazie sprawdź zapis/pominięcie opisu, osobny effort, błędny JSON
   i retry. OpenCode: błąd procesu, limit konta, brak wyniku lub próba narzędzia
   mają zatrzymać kolejkę bez rekordu. Timeout i błędny opis są per-image failures.
   Claude: typowane limity nadal wywołują oczekiwanie bez zapisu błędu obrazu.
5. OpenCode: poprawny pełny JSON przy exit 0 bez zdarzeń `step_finish` też jest
   sukcesem (ścieżka catch-up V2). Usage pozostaje wtedy NULL; przy natywnych
   tokenach zachowaj cały obiekt i licz tylko jawne `total`.
6. Sprawdź sprzątanie plików po sukcesie/błędzie, timeout/Ctrl+C oraz brak
   regresji Codexa po wydzieleniu wspólnej walidacji i przygotowania PNG.

Weryfikacja 2026-10-25: `/tmp/check_meme_new_clis.py` uruchomił Claude 2.1.294
i OpenCode 2.1.3 przeciw lokalnemu serwerowi. Obydwa zwróciły poprawny opis;
sprawdzono rzeczywiste listy narzędzi (`StructuredOutput` / pusta), dwa obrazy,
rozdzielczość i brak ścieżek źródłowych. OpenCode zwrócił tekst bez step_finish
i bez usage; adapter poprawnie go obsłużył. Nie wywoływano usług modeli ani nie
zmieniano memes.db. `/tmp/check_meme_cli_regressions.py` przeszedł 6 testów
adapterów i tymczasowej bazy (obrazy/klatki, schema, typed quota Claude,
resume/skip/retry, izolacja, błędy, usage, sprzątanie). Dotychczasowy
`/tmp/check_meme_codex_timeout.py` także przeszedł. Formatowanie i lint przeszły.
Rzeczywiste logowanie i dostępność modelu nadal wymagają próby z kontem użytkownika.

Weryfikacja katalogu 2026-10-25: `/tmp/check_meme_opencode_model.py` sprawdził
`opencode/deepseek-v4.1-flash-free` z dokładnym środowiskiem izolowanego adaptera.
Monitor inotify nie odnotował otwarć ani odczytów ~/.config/opencode podczas
działania klienta i serwera. Do białej listy modułów dodano opencode.models.dev;
bez niego wyłączony był także wbudowany katalog modeli. Nie wywołano modelu
ani nie zmieniono bazy. Konfiguracja OpenCode używa dostępnego darmowego
DeepSeeka bez niezweryfikowanego wariantu effort.
Pełny test adaptera z lokalnym providerem także nie odnotował otwarć/odczytów
tego katalogu; zachował `tools: []`, obrazy i poprawny opis. Z włączonym katalogiem
modeli uzyskano też step_start/step_finish oraz natywne usage bez jawnego total.

Weryfikacja błędu darmowej usługi 2026-10-25: metadane sesji potwierdzają
udaną odpowiedź natywnego build na DeepSeek V4.1 Flash Free / max po nieudanym
żądaniu adaptera na ten sam model/effort. Trzy minimalne próby bez obrazów i
bez zmian w memes.db odtworzyły provider.free-limit: custom agent, build oraz
build z natywnymi modułami agent/identity — zawsze z zakazem narzędzi.
To pasuje do upstream #51241/#54744; sama nazwa błędu nie dowodzi zużytej puli.
Nie włączano narzędzi ani dostępu do ~/.config/opencode w celu obejścia odmowy.

## Backend codex-cli

Profile: sprawdź `config.deepseek.toml`. Dziedziczenie model/effort musi być
widoczne również dla embed, a jawne ustawienia i CLI overrides mają pierwszeństwo.
Nie importuj pozostałych ustawień użytkownika, szczególnie uprawnień i helpers.
Token/header nie może pojawić się w argv ani komunikacie timeoutu. Testowy profil
z shell_tool=true i danger-full-access nadal ma uruchomić adapter bez narzędzi.

Weryfikacja profilu 2026-10-25: `/tmp/check_meme_codex_profile.py` przeszedł
4 testy wyboru providera, overrides, odrzucania helpers i przekazywania sekretów
przez środowisko. `/tmp/check_meme_codex_profile_wire.py` sprawdził rzeczywiste
CLI, lokalny endpoint, autoryzację fikcyjnym tokenem, model/effort, tools: [],
obraz i schema. Próba `--limit 1 -v` z rzeczywistym OpenRouterem opisała i zapisała
obraz vertical/1636857810162.jpg (5 sekund, 4898 tokenów, effort high). Kolejne
uruchomienia tej konfiguracji powinny pomijać już zapisany wariant.
Katalog `codex.deepseek-models.json` przeszedł parser Codexa 0.164.0 i ogłasza
none/low/high/max zgodnie z publicznymi metadanymi OpenRoutera. Instrukcje
domyślne przechwycono lokalnie z nieznanego wcześniej modelu, aby ich nie zmieniać.

Kontrola ograniczeń narzędzi: uruchom adapter z rzeczywistym CLI i lokalnym
endpointem testowym (fikcyjne logowanie, osobny `CODEX_HOME`). Żądanie do
`/v1/responses` ma zawierać `tools: []`, obraz/klatki i schemat. Syntetyczna
odpowiedź ma przejść przez adapter do JSON-a opisu. Sprawdź model z konfiguracji
oraz model spoza katalogu. Samo sprawdzenie argumentów procesu nie wystarcza.

- [ ] Ustaw `backend = "codex-cli"`, dostępny model wizyjny i prompt w osobnym
  TOML (`prompt_version = "vcodex1-master"`); `base_url` i `api_key` nie są
  wymagane. Użyj testowej bazy.
- [ ] `model_instructions_file` wskazuje plik z dokładną treścią wybranego promptu.
  Stdin zawiera wyłącznie polecenie dla mema i informacje o klatkach; stały prompt
  nie jest w nim powtarzany. Zmiana `prompt_version` nadal tworzy osobny wariant.
- [ ] Nagłówek `-v` pokazuje `[N tokens]` jako sumę input/output z `turn.completed`;
  cached input nie jest doliczane drugi raz. Przy braku usage licznik jest pomijany.
- [ ] `describe --limit 1 -v` zapisuje surowy JSON; drugi przebieg go pomija.
  `--model`, `--effort`, `--path` i `--retry-failed` działają jak wcześniej.
- [ ] Sprawdź PNG/JPEG oraz animacje GIF/WebP/APNG, także ścieżkę ze spacjami.
  Wszystkie wybrane klatki trafiają do modelu w kolejności chronologicznej.
- [ ] Błąd procesu, limit konta i brak końcowej odpowiedzi zatrzymują
  przebieg bez rekordu dla bieżącego obrazu. Ponowne uruchomienie go podejmuje.
- [ ] Timeout zapisuje `failed_describe` z komunikatem o przekroczeniu czasu
  i przechodzi do następnego obrazu. `--retry-failed` ponawia tę próbę.
- [ ] Niepoprawny końcowy JSON, powtórzone keywords, zły humor_type lub uszkodzony
  obraz dają `failed_describe`; pozostałe obrazy są przetwarzane dalej.
- [ ] Pierwszy Ctrl+C kończy i zapisuje bieżący opis; drugi przerywa proces.
  Timeout usuwa też procesy potomne. Pliki tymczasowe są sprzątane.
- [ ] Backend Claude nadal odczytuje `structured_output` i rozpoznaje swój limit
  użycia po polach typowanych, mimo wspólnej funkcji uruchamiania procesu.

Weryfikacja 2026-10-09: skrypt `/tmp/check_codex_backend.py` przeszedł testy
adaptera z symulowaną odpowiedzią CLI (argumenty, GIF/PNG, schemat, zachowanie
surowego JSON, walidacja i sprzątanie), tymczasowej bazy (błąd bez wpisu,
wznowienie/pominięcie, wariant effort i błędny JSON), regresji odpowiedzi/limitu
Claude oraz rzeczywistego subprocessu (stdin/stdout/timeout). Przerwanie procesu
sprawdzono z mockiem. Nie wykonano opisu przez usługę Codex; logowanie,
dostępność wybranego modelu i pełny przebieg sieciowy wymagają próby `--limit 1`.

Weryfikacja przejścia na instructions file: adapter Codexa z symulowaną
odpowiedzią dostał dokładną treść `vcodex1-master.txt` w pliku UTF-8 wskazanym
przez `model_instructions_file`. Sprawdzono statyczny obraz i animację:
stdin zawierał tylko zadanie oraz timestampy klatek, a plik instrukcji sam prompt.
Sprawdzono skalowanie obrazu statycznego, sprzątanie plików i transporty pozostałych
backendów. Formatowanie i lint przeszły. Nie wykonano wywołania usługi Codex.

## Wspólne klatki animacji

- [ ] Animacja z cięciami: jedna klatka ze środka każdego ujęcia (`scene > 0.15`).
- [ ] Bez cięć: klatka ze środka i ostatnia; ten sam indeks klatki tylko raz.
- [ ] Zmienne czasy klatek w GIF/WebP/APNG i APNG z osobnym podglądem statycznym.
- [ ] Klatki zachowują natywną rozdzielczość; statyczne obrazy nadal są skalowane
  na ścieżkach HTTP/Codex. Pliki tymczasowe znikają po żądaniu/błędzie.
- [ ] Brak `ffmpeg` lub błąd ekstrakcji zapisuje błąd tylko danego obrazu;
  kolejny obraz statyczny nadal jest opisywany.

Weryfikacja 2026-10-09: `/tmp/check_meme_animation_frames.py` użył prawdziwego
FFmpeg do GIF/WebP/APNG (cięcia, brak cięć, zmienne czasy, natywna rozdzielczość,
APNG z osobnym podglądem, ścieżki ze spacjami). Transporty HTTP/Claude/Codex
sprawdzono z symulowanymi odpowiedziami modeli i tymczasową bazą, także kolejność,
timestampy, zapis JSON, sprzątanie oraz kontynuację po braku FFmpeg.
Ekstrakcja przeszła też na trzech GIF-ach z rzeczywistej kolekcji (20, 24 i 51
klatek źródłowych). Nie wykonano płatnych wywołań modeli ani zmian w `memes.db`.
