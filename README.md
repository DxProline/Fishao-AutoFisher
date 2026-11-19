# Fishao | automat, záznam a offline rozbor

Windows 11, 64bitový CPython 3.14. Balíček obsahuje záznamník, offline přehrávač, asistenci a automat opakovaného lovu z připraveného místa u vody.

## 1. Záznamník

Rozbal ZIP do samostatné složky a otevři v ní PowerShell. Použít můžeš existující prostředí původního projektu:

```powershell
$python = 'C:\Users\jenda\PycharmProjects\PythonProject6\.venv\Scripts\python.exe'
& $python -m pip install -r requirements.txt
& $python .\main.py
```

1. Otevři hru na jednom monitoru a v aplikaci klikni na **Začít záznam**, případně stiskni **F8** přímo ve hře.
2. Začni několik sekund před ručním nahozením. Odehraj celý pokus a chvíli nech viditelný výsledek. Pro rozbor pomůže úspěšný a neúspěšný pokus, výběr prutu a návnady a oba směry ovládání, pokud je hra podporuje.
3. **F8** pozastaví nebo obnoví relaci. Přepnutí z hry přeruší snímání i sběr herních vstupů. **F9** zastaví snímání a záznamník dopíše už pořízené snímky.
4. Tlačítko **Otevřít složku** ukáže relaci v `recordings/fishao_<čas>/`. V GUI zkontroluj, že hodnota **Vstupy** po hraní roste. Případné selhání registrace Windows uvidíš v panelu **Události**.

PNG snímky mají původní rozlišení klientské plochy. Výchozí cíl je 15 sn./s. Volby `--fps 15` a `--output-dir D:\FishaoZaznamy` nastaví snímkovou frekvenci a umístění. MSS snímá viditelné pixely. Po dobu záznamu ponech okno hry bez překryvů.

| Soubor | Obsah |
| --- | --- |
| `frames.jsonl` | Index uložených PNG, skutečné časy pořízení a rozměry okna. |
| `inputs.jsonl` | Stisk a uvolnění kláves a tlačítek myši během aktivní relace ve hře. Kliknutí mají souřadnice od levého horního rohu klientské plochy a poměry vůči její šířce a výšce. F8 a F9 ovládají relaci. |
| `events.jsonl` | Zahájení snímání, pauzy, čekání na okno, změny rozměru a konec. |
| `session.json` | Stav a souhrn relace včetně počtu uložených snímků a vstupů. |
| `logs/fishao.log` | Protokol programu ve výstupní složce nad jednotlivými relacemi. |

JSONL ukládá jeden objekt JSON na řádek. `t_ns` je počet nanosekund od začátku relace. Volitelný `markers.jsonl` může obsahovat vlastní poznámky ve stejném čase, třeba `{"t_ns":6500000000,"type":"poznámka","label":"tady jsem nahodil"}`.

## 2. Kontaktní list a offline přehrávač

Dosazuj cestu ke konkrétní relaci z tlačítka **Otevřít složku**:

```powershell
$relace = '.\recordings\fishao_2026-11-18_22-45-00-000000'
& $python .\contacts.py $relace
& $python .\replay.py $relace --output .\rozbor.jsonl
```

Kontaktní list otevři z `kontakty/index.html` uvnitř relace. Každý řádek ukazuje pět snímků v rozsahu od 0,4 s před událostí do 0,4 s po ní, s vyznačením kliknutí u prostředního snímku. Ukazuje i mezery, v nichž chybí dostatečně blízký snímek. Opakovaný export ukládej pomocí `--output-dir` do nové složky.

Offline přehrávač analyzuje stabilitu scény, sledované oblasti, jejich posuny, nově objevené oblasti a vizuální kandidáty na vodu. U lišty `YOU HAVE A BITE!` vypíše pozici dvojice modrých šipek, zelený střed a rozhodnutí o držení nebo uvolnění klávesy. Souřadnice jsou poměry 0–1. Volba `--changes-only` zmenší výpis na přechody, změny směru a snímky s událostmi. Herní stavy, ke kterým zatím chybí záběry, se označují `unknown`.

## 3. Živá asistence jedné minihry

```powershell
& $python .\main.py --assist
```

Spusť asistenci přes **F8** a ručně nahoď. Po rozpoznání lišty aplikace drží opačnou šipku podle vychýlení a rychlosti ukazatele. Uprostřed ji uvolní, pokud tam ukazatel zůstává. Jakmile lištu najde, snímá výřez kolem ní s cílem až 90 vzorků za sekundu. Skutečnou rychlost měří GUI. Ztratí-li lištu nebo fokus, uvolní vstup. **F8** je pauza, **F9** konec. Po zmizení lišty zkontroluj výsledek a zavři jej klávesou Esc.

Volitelný režim jednoho zkušebního hodu:

```powershell
& $python .\main.py --assist --test-cast
```

Před startem postav postavu u vody, zobraz kruh dosahu a ověř energii, prut a návnadu. Aplikace zkusí jeden klik, pokud na dvou snímcích najde podobný bod ve vodě uvnitř kruhu. Pro hledání bodu, čekání na záběr a minihru má oddělené limity. Po jednom pokusu se relace ukončí.

## 4. AutoFish

```powershell
& $python .\main.py --autofish
```

1. Postav postavu k vodě, připrav prut a návnadu a zobraz kruh dosahu. Zavři otevřené nabídky a staré výsledky.
2. Spusť automat tlačítkem nebo **F8**. Okno hry ponech v popředí. **F8** pozastaví práci a **F9** ukončí relaci.
3. AutoFish potvrdí hod z obrazu, vyčká na záběr, vyváží minihru a zavře rozpoznaný výsledek přes Esc. Jakmile se herní lišta vrátí do aktivního stavu, hledá další bod ve vodě uvnitř kruhu. Upřednostňuje osvědčená místa z relace a zkusí mírně odlišný bod, když má místo kolem předchozího hodu. Při nulové energii čeká na zvýšení čítače. Vyčerpanou výbavu zkusí doplnit podobně jako předchozí verze.
4. AutoFish běží do ručního vypnutí pomocí **F9**. Volitelný parametr `--max-casts 2` se hodí pro krátký test.

Pohyb postavy k vodě zajišťuje hráč. Hra a automat musí mít stejnou úroveň oprávnění Windows. Rozpoznávání bylo ověřené na skutečných snímcích úlovku, úniku a inventáře. Průchod AutoFish a rychlé změny směru byly testované simulací. Celý automatický průchod ve hře je třeba ověřit prvním hlídaným spuštěním. Probíhající hod může skončit i bez úlovku. AutoFish se řídí stavem hry a při nejasném obraze vyčkává.

Testy balíčku spusť příkazem `& $python -B -m unittest -v`.

