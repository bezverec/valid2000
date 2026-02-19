# Valid2000

Validátor JPEG 2000 a TIFF pro **NDK** kontrolu (intepretace NDK profilu):

- **JP2**: parsuje výstup z **jpylyzer** + aplikuje pravidla profilu (NDK Archival / Master kopie).
- **TIFF**: spustí **tiffdump ve WSL**, z výpisu vytáhne klíčové tagy a aplikuje pravidla profilu (NDK Master).
- **GUI (Tkinter)**: přepínače, tooltipy, barevný výstup, ukládání konfigurace.
- **Batch režim**: umí validovat **soubor nebo celý adresář** (včetně rekurze a globů).


---

## Obsah repozitáře

- `jp2.py` – JP2 validátor (jpylyzer + pravidla + odvozené hodnoty, tile-parts heuristiky, volitelný scan markerů FF55/TLM).
- `tiff.py` – TIFF validátor (WSL `tiffdump` + pravidla + batch režim).
- `gui.py` – Tkinter GUI pro JP2 i TIFF (spouští `jp2.py` / `tiff.py` přes aktuální Python).

---

## Požadavky

### Společné
- Python **3.10+** (doporučeno 3.13)
- Windows 11 (cilová platforma, omlouvám se :))

### JP2
- `jpylyzer` (na Windows typicky `jpylyzer.exe`)
  - V GUI/CLI lze zadat cestu přes `--jpylyzer-cmd`.

### TIFF
- **WSL** + `tiffdump` dostupné v Linuxu ve WSL
  - Typicky balík `libtiff-tools` (Debian/Ubuntu): `sudo apt install libtiff-tools`
- GUI/CLI standardně převádí cestu `C:\...` → `/mnt/c/...` (lze vypnout).

---

## Instalace (doporučeně venv)

```powershell
cd C:\temp\validator
python -m venv venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
python -m pip install -U pip
```

> Tyto skripty jsou “single-file” a typicky nepotřebují pip balíčky, ale `jp2.py` potřebuje externí `jpylyzer`.

---

## Použití – CLI

### JP2: validace jednoho souboru

```powershell
python .\jp2.py D:\data\img.jp2 --jpylyzer-cmd C:\jpylyzer\jpylyzer.exe
```

Užitečné přepínače (výběr):
- `--dump-map` vypíše “flattened map” klíčů jako JSON (tvorba pravidel)
- `--show-ok` zobrazí i OK pravidla
- `--packetmarkers`, `--mix 2`, `--jp2-format jp2|jph|j2c|jhc` … se jen přeposílají do jpylyzeru
- `--scan-markers` provede byte-scan JP2/j2c a hledá marker **FF55 (TLM)** v payloadu codestreamu
- `--dump-jp2scan` vypíše krátký “JP2SCAN” dump (počty/offsety markerů)

### JP2: batch režim (adresář)

```powershell
python .\jp2.py C:\data\jp2 --jpylyzer-cmd C:\jpylyzer\jpylyzer.exe --recursive
```

Globy (lze opakovat):
```powershell
python .\jp2.py C:\data --recursive --glob "*.jp2" --glob "*.xml"
```

Na konci dostanete `BATCH SUMMARY` + návratový kód: 0 OK, 1 WARN, 2 FAIL/ERROR.

---

### TIFF: validace jednoho TIFFu přes WSL tiffdump

```powershell
python .\tiff.py C:\data\img.tif
```

Nastavení WSL/tiffdump:
```powershell
python .\tiff.py C:\data\img.tif --wsl wsl --tiffdump tiffdump --timeout 30
```

Extra args pro tiffdump (jednoduchý split podle mezer):
```powershell
python .\tiff.py C:\data\img.tif --tiffdump-args "-D"
```

Vypnutí konverze cesty (`C:\...` → `/mnt/c/...`):
```powershell
python .\tiff.py C:\data\img.tif --no-convert-path
```

Batch režim + rekurze:
```powershell
python .\tiff.py C:\data\tiffs --recursive
```

Globy:
```powershell
python .\tiff.py C:\data --glob "*.tif" --glob "*.tiff"
```

Stejné návratové kódy jako JP2 (0/1/2) + `BATCH SUMMARY`.

---

## Použití – GUI (Tkinter)

Spuštění:

```powershell
python .\gui.py
```

- Záložka **JP2** umí validovat soubor *nebo adresář* (rekurze + globs), volby jpylyzeru, `--dump-map`, `--packetmarkers`, a také scan markerů FF55/TLM.
- Záložka **TIFF** umí validovat soubor *nebo adresář* (rekurze + globs), nastavení WSL/tiffdump, timeout atd.
- Barevný výstup: **OK zeleně**, **WARN oranžově**, **FAIL červeně**
- Konfigurace se ukládá do `gui_config.json` vedle skriptu.

---

## Profily a pravidla

### Vestavěné profily
- `jp2.py` používá vestavěný profil “NDK Master/Archival JP2 (core rules)”.
- `tiff.py` používá vestavěný TIFF profil “NDK Master”.

### Vlastní profil (JSON)
Oba validátory umí načíst `--profile profil.json` (v GUI volba „Profil JSON“).

Struktura profilu:
- `name`: volitelný název profilu
- `descriptions`: mapování `key → popisek` (použije se ve výpisu)
- `rules`: seznam pravidel `{id, key, assert, expected, level, message, when?}`

Tip: nejdřív použijte `--dump-map`, abyste viděli dostupné klíče.

---

## Vzorový JSON profil – JP2

Uložte např. jako `profile_jp2_custom.json`:

```json
{
  "name": "Custom JP2 profile (example)",
  "descriptions": {
    "jpylyzer.file.properties.contiguousCodestreamBox.cod.transformation": "Transformace (COD)",
    "jpylyzer.file.properties.contiguousCodestreamBox.cod.order": "Progression order (COD)",
    "jpylyzer.file.properties.contiguousCodestreamBox.cod.levels": "Počet dekompozičních úrovní (COD)",
    "jpylyzer.file.properties.contiguousCodestreamBox.siz.xTsiz": "Dlaždice – tile width (SIZ)",
    "jpylyzer.file.properties.contiguousCodestreamBox.siz.yTsiz": "Dlaždice – tile height (SIZ)",
    "derived.tlm_present": "TLM (Tile Length Markers) přítomno",
    "derived.icc_present": "ICC profil přítomen",
    "derived.tparts_org_inferred": "Tile-part organization (inferováno)"
  },
  "rules": [
    {
      "id": "lossless_transformation",
      "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.transformation",
      "assert": "equals",
      "expected": "5-3 reversible",
      "level": "error",
      "message": "Vyžadujeme bezeztrátovou transformaci 5-3 reversible."
    },
    {
      "id": "progression_order_rpcl",
      "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.order",
      "assert": "equals",
      "expected": "RPCL",
      "level": "error",
      "message": "Progression order musí být RPCL."
    },
    {
      "id": "decomposition_levels",
      "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.levels",
      "assert": "in",
      "expected": [5, 6],
      "level": "error",
      "message": "Dekompoziční úrovně: 5 nebo 6."
    },
    {
      "id": "tiling_4096",
      "key": "jpylyzer.file.properties.contiguousCodestreamBox.siz.xTsiz",
      "assert": "equals",
      "expected": 4096,
      "level": "error",
      "message": "Tile width má být 4096."
    },
    {
      "id": "tlm_present",
      "key": "derived.tlm_present",
      "assert": "equals",
      "expected": true,
      "level": "error",
      "message": "TLM marker (FF55) / tlm element musí být přítomen."
    },
    {
      "id": "icc_present",
      "key": "derived.icc_present",
      "assert": "equals",
      "expected": true,
      "level": "error",
      "message": "ICC profil musí být přítomen (v JP2 headeru)."
    },
    {
      "id": "tileparts_org_warn",
      "key": "derived.tparts_org_inferred",
      "assert": "in",
      "expected": ["R", "unknown"],
      "level": "warn",
      "message": "ORGtparts=R je doporučení/heuristika – pokud inferujeme T, dáme WARN."
    },
    {
      "id": "marker_scan_only_if_enabled",
      "key": "derived.tlm_marker_ff55_present",
      "assert": "equals",
      "expected": true,
      "level": "warn",
      "message": "Pokud používáte --scan-markers, chceme vidět FF55.",
      "when": { "key": "derived.jp2scan_enabled", "equals": true }
    }
  ]
}
```

Poznámky:
- `level`: `"error"` → při nesplnění je `FAIL`, `"warn"` → při nesplnění je `WARN`.
- `when` je volitelná podmínka (typicky pro pravidla závislá na tom, zda je zapnutý marker-scan).
- `derived.*` jsou odvozené hodnoty počítané skriptem (ne přímo z jpylyzer).

---

## Vzorový JSON profil – TIFF

Uložte např. jako `profile_tiff_custom.json`:

```json
{
  "name": "Custom TIFF profile (example)",
  "descriptions": {
    "tiff.ImageWidth": "Šířka",
    "tiff.ImageLength": "Výška",
    "tiff.BitsPerSample": "BitsPerSample",
    "tiff.SamplesPerPixel": "SamplesPerPixel",
    "tiff.PhotometricInterpretation": "PhotometricInterpretation",
    "tiff.Compression": "Compression",
    "tiff.XResolution": "XResolution",
    "tiff.YResolution": "YResolution",
    "tiff.ResolutionUnit": "ResolutionUnit",
    "tiff.ICCProfile.present": "ICC profil přítomen",
    "tiff.ICCProfile.datatype_ok": "ICC datatype OK"
  },
  "rules": [
    {
      "id": "rgb",
      "key": "tiff.PhotometricInterpretation",
      "assert": "in",
      "expected": [2, "RGB"],
      "level": "error",
      "message": "Musí být RGB (PhotometricInterpretation=2)."
    },
    {
      "id": "samples_3",
      "key": "tiff.SamplesPerPixel",
      "assert": "equals",
      "expected": 3,
      "level": "error",
      "message": "SamplesPerPixel musí být 3 (RGB)."
    },
    {
      "id": "bps_8_8_8",
      "key": "tiff.BitsPerSample",
      "assert": "equals",
      "expected": [8, 8, 8],
      "level": "error",
      "message": "BitsPerSample musí být [8,8,8]."
    },
    {
      "id": "no_compression",
      "key": "tiff.Compression",
      "assert": "equals",
      "expected": 1,
      "level": "error",
      "message": "Compression musí být 1 (bez komprese)."
    },
    {
      "id": "dpi_300_plus_x",
      "key": "derived.dpi_x",
      "assert": "range",
      "expected": { "min": 300 },
      "level": "error",
      "message": "DPI X musí být >= 300."
    },
    {
      "id": "dpi_300_plus_y",
      "key": "derived.dpi_y",
      "assert": "range",
      "expected": { "min": 300 },
      "level": "error",
      "message": "DPI Y musí být >= 300."
    },
    {
      "id": "icc_present",
      "key": "tiff.ICCProfile.present",
      "assert": "equals",
      "expected": true,
      "level": "error",
      "message": "ICC profil musí být přítomen."
    },
    {
      "id": "icc_datatype_ok",
      "key": "tiff.ICCProfile.datatype_ok",
      "assert": "equals",
      "expected": true,
      "level": "error",
      "message": "ICC musí mít správný data type (kontrola dle parseru)."
    }
  ]
}
```

Poznámky:
- TIFF klíče závisí na tom, jak `tiff.py` mapuje výstup `tiffdump`.
- `derived.dpi_x` / `derived.dpi_y` jsou odvozené hodnoty (přepočet z XResolution/YResolution + ResolutionUnit).

---

## Interpretace výsledků

- `SUMMARY: OK=… WARN=… FAIL=…` – souhrn pravidel pro jeden soubor
- `BATCH SUMMARY: files=… OK=… WARN=… FAIL=… ERROR=…` – souhrn pro dávku
- Návratové kódy:
  - `0` – vše OK
  - `1` – pouze WARN (bez FAIL/ERROR)
  - `2` – FAIL nebo ERROR (včetně chyb spuštění externích nástrojů)

---

## Troubleshooting

### TIFF: “Failed to translate Z:\…”
Typicky problém s cestou do WSL. V GUI nechte zapnuté **„Převést Win cestu → /mnt/…“**, nebo v CLI nepoužívejte `--no-convert-path`.

### TIFF: `tiffdump` není ve WSL
Nainstalujte ve WSL:
```bash
sudo apt update
sudo apt install libtiff-tools
```

### JP2: jpylyzer nenalezen
Zadejte `--jpylyzer-cmd C:\...\jpylyzer.exe` (nebo vyberte v GUI).

### TLM / FF55
Jpylyzer umí ukázat `<tlm/>` element, ale někdy je užitečné potvrdit i marker-scanem:
- zapněte `--scan-markers` (a případně `--dump-jp2scan`)

---

## Licence

GPLv3.

---

## Poznámky k NDK

- JP2 pravidla jsou nastavená podle mé vlastní interpretace NDK Archival / Master kopie (5-3 reversible, no quantization, RPCL, 4096 tiling atd.).
- ICC je kontrolováno jako **FAIL**, pokud v datech není přítomné.
- ORGtparts R heuristika je **heuristika** (odvozená z pořadí SOT/tpsot/isot) – není to “oficiální” pole v JP2, ale praktická interpretace. V profilu NDK řazení tile partů podle rozlišení není zřejmě specificky vyžadováno, ale vyskytuje se jako přepínač ve vzorových příkazech pro Kakadu kodek.
- Při vyjasnění sporných parametrů *hardcodnutou* interpretaci opravím.

## Screenshot
<img width="1182" height="851" alt="valid2000" src="https://github.com/user-attachments/assets/24882f7b-b9fe-4297-b1be-9a2515f46c04" />

