# Fishao AutoFisher

Automatický pomocník a záznamník pro hru **FISHAO - Reborn** na Windows 11 (64bitový Python 3.14). Vedle živého náhledu v okně GUI zobrazuje statistiky, aktuální stav stavového automatu a volby automatického lovu. Číslo verze (`1.3.0`) je zobrazeno pod názvem aplikace.

---

## Rychlý start přes `ApplicationStart`

Veškeré spouštění aplikace je sjednoceno do veřejného vstupního bodu `ApplicationStart` (`application_start.py`), do kterého deleguje i `main.py`. Před otevřením GUI automaticky ověří pomocnou konfiguraci (`config.json`, pokud existuje), operační systém Windows, zapisovatelnost výstupní složky, volné místo na disku, požadované Python knihovny a v režimech `--autofish`, `--xp-farm` a `--agent` i přítomnost šablon `ui_reference/*.png`.

```bat
py -3.14 setup_windows.py
```

Skript `setup_windows.py` vytvoří místní prostředí `.venv`, nainstaluje závislosti z `requirements.txt` a založí zástupce na ploše s ikonkou `assets/Tkinter.ico`.

### Přímé spuštění z příkazové řádky (Windows PowerShell / CMD)

```powershell
$python = '.\.venv\Scripts\python.exe'

# Ověření konfigurace a prostředí před spuštěním GUI:
& $python .\main.py --validate-only --agent
# nebo:
& $python -m application_start --validate-only --agent

# Spuštění základního GUI (Záznamník):
& $python .\main.py

# Spuštění konkrétního režimu přes ApplicationStart:
& $python .\main.py --assist
& $python .\main.py --assist --test-cast
& $python .\main.py --autofish
& $python .\main.py --xp-farm
& $python .\main.py --agent
```

### Požadavky a chybějící podklady při přenosu

- **OS:** Windows 11 (64bitový CPython 3.14).
- **Knihovny:** `requirements.txt` (`opencv-python`, `numpy`, `mss`, `pywin32`, `tkinter`).
- **Povinné šablony (`ui_reference/`):** Režimy `--autofish`, `--xp-farm` a `--agent` vyžadují původní složku `ui_reference/*.png` (61 šablon pro `game_ui.Interface` a `destinations.Navigator`; volitelně také `mission_accept.png`, `mission_claim.png`, `mission_complete.png`, `mission_reward.png`). Skript `setup_windows.py` pro vytvoření zástupce navíc vyžaduje `assets/Tkinter.ico`. V přiloženém předání tyto binární soubory nebyly obsaženy — po rozbalení ZIP archivu je zkopíruj z původní instalace projektu. Pokud v adresáři chybí, `ApplicationStart` skončí srozumitelnou chybou se seznamem chybějících souborů.

---

## Konfigurace, zastavení a protokoly (logy)

- **Konfigurace:** `config.json` (validovaný modulem `config.py`) je zachován jako pomocná/původní konfigurace kontrolovaná při startu `ApplicationStart`. Samotný živý běh `Auto` a `Assist` používá normalizované souřadnice `1264x681` v `game_ui.Interface` a parametry příkazové řádky (`--output-dir`, `--fps 1..120`, `--image-format jpg|png`, `--max-casts 1..500`).
- **Pozastavení a bezpečné zastavení:**
  - **F8** (nebo tlačítko v GUI) relaci spustí či pozastaví (`PAUSED_MANUAL`).
  - **F9** (nebo zavření okna aplikace) bezpečně ukončí pracovní vlákno, uvolní všechny držené klávesy i tlačítka myši (`Controls.release()`) a zavře handlery protokolu.
- **Strukturované logování a panel Události:**
  - **Panel `Události` v GUI (`fishao.events`):** zobrazuje pouze srozumitelný časový přehled významných událostí v češtině (místní čas `HH:MM:SS`), např. otevření a procházení stránek misí, kliknutí na přijetí/odměnu s následným potvrzením výsledku, potvrzení dialogu nové úrovně, chyby včetně důvodu a obnovení lovu. Opakované identické snímky se neopakují.
  - **Technická diagnostika (`fishao` -> `<output-dir>/runtime/logs/fishao.log` a konzole):** je oddělená od uživatelského panelu Události a obsahuje podrobné záznamy přechodů stavového automatu (`ts=<lokální-ISO> | cid=<id_relace> | state=<BotState> | action=<akce> | result=<výsledek> | retry=<pokus> | reason='...'`), OCR výpisy karet misí a ladicí kroky. Časové razítko `ts=` i `%(asctime)s` používá shodný místní čas a filtr `DuplicateLogFilter` potlačuje opakované identické `INFO`/`DEBUG` řádky (bez potlačení přechodů `state=`, `WARNING` či `ERROR`). Logy neobsahují citlivé údaje ani surové snímky obrazovky.

### Co znamená „učení“ agenta (bez mezi-relační paměti)
- **Mezi jednotlivými spuštěními se agent neučí:** aplikace neukládá na disk žádné zkušenosti, váhy ani naučené strategie pro příští spuštění. Při startu na to panel Události výslovně upozorňuje.
- **V rámci jednoho spuštění** využívá pouze krátkodobou paměť a stavové heuristiky:
  - `Cycle.score` (`+2` za úlovek, `-1` za útěk) a `Cycle.preferred()` v `bot.py` upřednostňují ověřeně úspěšná místa nahození z aktuálního běhu;
  - `Cycle.rejected` dočasně vyřadí až 6 bodů, kde se nahození nepotvrdilo;
  - `Progression` (`progression.py`) na základě statistik aktuálního běhu (`casts`, `caught`, `escaped`) rozhoduje o návnadě, vlasci, přesunu na jinou oblast nebo Trout Farm.

---

## Oprava spolehlivosti a známá omezení OS Windows

### Uzamčení počítače (`Win + L`), spořič obrazovky a běh v překrytém okně
- **Prevence spořiče obrazovky a uspání při AFK (`Bránit spořiči a uspání obrazovky`):** Během aktivního lovu (pokud není ručně pozastaven nebo vypnut) volá automat Win32 API `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)`, aby Windows po 15–20 minutách bez fyzické aktivity nespustil spořič obrazovky, nevypnul monitor ani PC nezamkl z nečinnosti. Při pauze (**F8**) nebo zastavení (**F9**) se nastavení okamžitě vrátí na `ES_CONTINUOUS`.
- **Co když se PC přesto zamkne (`Win + L` nebo vynucený zámek systému):**
  - Na uzamčené pracovní stanici Windows (`WinSta0\Winlogon`) operační systém z bezpečnostních důvodů blokuje `SendInput`, `GetCursorPos` i běžné snímání popředí (`mss`). Aplikace toto omezení neobchází nelegálními zásahy, ale v režimu `Auto` (`continuous=True`) automaticky vyzkouší záložní čtení okna na pozadí (`WindowCapture.grab_locked_background` přes `BitBlt` / `PrintWindow`) a zprávy oknu (`BackgroundControls` přes `PostMessage`).
  - Pokud Windows a klient `FISHAO - Reborn` při spořiči nebo zámku stále vykreslují obraz okna (`PrintWindow`) a přijímají `PostMessage`, **agent pokračuje v lovu na pozadí bez přerušení i bez vypnutí**.
  - Pokud Windows na zamčené ploše z bezpečnostních důvodů či kvůli GPU vrací pouze prázdné/černé snímky (`not image.any()`), aplikace bezpečně uvolní klávesy, vyčká ve stavu **`PAUSED_LOCKED`** (bez vyčerpání rozpočtu chyb a **bez ukončení agenta**) a po probuzení či odemčení lov automaticky obnoví.
  - Výjimky `SendInput`, `GetCursorPos`, `GetForegroundWindow` a `IsIconic` v `controls.py` jsou zachyceny tak, aby neukončily vlákno asistenta ani při přepnutí plochy.
- **Režim překrytého okna (`Lovit v překrytém okně`):** Snímá okno přes `BitBlt` / `PrintWindow` a posílá zprávy `PostMessage` bez nutnosti popředí. Závisí na tom, zda Windows a spuštěný klient `FISHAO - Reborn` zprávy na pozadí přijímají. Při minimalizaci okna hra neposkytuje obraz a automat bezpečně vyčká (`RECOVERING`).

### Nepřetržitý AFK grind, timeouty a automatické potvrzování dialogů (`OK` / `Collect` / `Claim`)
- **Nepřetržitý AFK provoz (`continuous=True`):** `Auto` (`AutoFish`, `XP Farm`, `Agent`) se při přechodných výpadcích nebo opakovaných limitech (`Phase.CAST`, `Phase.WAIT` při ztrátě HUD, `Phase.RESULT`, `Phase.CLOSE`, `Phase.GEAR`, `Phase.REFILL`, `UNKNOWN_SCREEN` i opakovaných chybách mapy v `Navigator._map`) sám bezpečně zotaví (`_afk_cooldown_recovery`, pauza 5 s, uvolnění vstupů, `Escape`, reset čítače chyb a návrat k `Phase.READY`) a **pokračuje v lovu bez zastavení**, dokud jej ručně neukončíš (**F9** / tlačítko *Zastavit*, případně explicitní limit `--max-casts`). Pokud dojde k neočekávané výjimce v pracovním vlákně s `continuous=True`, `Assist.run()` smyčku rovněž restartuje.
- **Okamžité potvrzení modálních dialogů (`OK` / `Collect` / `Claim` / `Accept`):** Před každým dalším krokem ve smyčce volá `Auto._modal_dialog()`:
  - Rozpozná dialog nové úrovně (`ui.level_ok`) se zeleným `OK` a tolerancí odlesků.
  - Rozpozná také samostatná modální okna misí, odměn a hlášení (včetně obrazovek typu *„WOW, YOU'RE FAST!“* s oranžovým tlačítkem `Collect`, `Claim` i zelenými `OK`/`Accept` tlačítky uvnitř světlé karty v `Interface.mission_popup_action` / `find_action_button`) i bez dodatečných PNG šablon.
  - Potvrzuje okamžitě přes `LevelDialogController` (2 po sobě jdoucí snímky, prodleva mezi kliky, záložní `Escape`, bez zastavení smyčky lovu). Úspěšné zavření dialogu zapíše do Událostí až poté, co okno skutečně zmizí.
- **Mise (`Activities` + `MissionTracker`):** Detekuje nabídnuté mise, potvrzuje přijetí (`accept`), sleduje podmínky a průběh, vyzvedává odměny (`claim`/`collect`/`reward`) jak na kartě mise (zelené i oranžové tlačítko), tak ve vyskakovacích oknech, a stav `COMPLETED` i zprávu v Událostech zapíše teprve po ověření výsledku. Chvilkový 1-snímkový přechod zavírání okna nezpůsobí předčasné selhání stavu.

---

## Režimy aplikace

### 1. Záznamník (`python main.py`)
1. Otevři hru na jednom monitoru a v aplikaci klikni na **Začít záznam** (nebo stiskni **F8**).
2. Hraj ručně; **F8** pozastaví nebo obnoví relaci, **F9** ji ukončí a dopíše soubory do `recordings/fishao_<čas>/` (`frames.jsonl`, `inputs.jsonl`, `events.jsonl`, `session.json`).

### 2. Kontaktní list a offline přehrávač
```powershell
$relace = '.\recordings\fishao_2026-11-18_22-45-00-000000'
& $python .\contacts.py $relace
& $python .\replay.py $relace --output .\rozbor.jsonl
```
Kontaktní list otevři z `kontakty/index.html` uvnitř složky relace.

### 3. Živá asistence jedné minihry (`--assist` / `--assist --test-cast`)
Spusť asistenci přes **F8** a nahoď. Po rozpoznání lišty minihry `YOU HAVE A BITE!` vyvažuje ukazatel šipkami až do 120 Hz. Režim `--test-cast` provede jeden zkušební hod v kruhu dosahu a skončí.

### 4. AutoFish, XP Farm a Agent (`--autofish`, `--xp-farm`, `--agent`)
- Postav postavu k vodě, připrav prut a návnadu a zavři cizí okna ve hře.
- **AutoFish** opakovaně hledá vodu v kruhu dosahu, nahazuje, vyvažuje minihru a zavírá výsledek.
- **XP Farm** při nulové energii čeká na dobití a na placené Trout Farm umí využít ověřenou nabídku energie (s rezervou bankovek).
- **Agent** zapíná **Mise, postup a vybavení**, kontroluje karty misí (přijetí / sledování / vyzvednutí), doplňuje prut/návnadu z inventáře či obchodu, při nízkém výkonu mění oblast, a při volbě **Účastnit se turnajů** se může hlásit do herních turnajů.
- Na kartě **Lovná místa** lze vybrat Laketown, vlastní uložený břeh i bezplatný chléb u Jamieho v pekárně.

---

## Řešení potíží a testování

- **Chybí `ui_reference/*.png`:** Ověř kompletnost instalační složky pomocí `python main.py --validate-only --agent`.
- **Hra na pozadí nereaguje na kliknutí:** Vypni volbu *Lovit v překrytém okně* a ponech `FISHAO - Reborn` v popředí se stejnými oprávněními Windows jako má automat.
- **Automat se zastavil s `ERROR_STOPPED`:** Zkontroluj `recordings/runtime/logs/fishao.log` podle posledního záznamu `cid=` a `reason=`.

### Spuštění testů (na Windows se všemi závislostmi)
```powershell
& $python -B -m unittest -v
```
Podrobný přehled spuštěných vs. nespuštěných testů je v souboru [`TEST_REPORT.md`](./TEST_REPORT.md).
