# Usage

This page covers the normal `barprint` workflow in more detail than the README. The examples are Windows and PowerShell first because BAR and Blender installs are usually discovered from Windows paths.

## Requirements

- Python 3.10 or newer.
- Blender installed separately from <https://www.blender.org/download/>.
- Beyond All Reason installed locally.

`barprint` has no npm dependency. Normal exports run Blender in the background.

## Install

From a source checkout:

```powershell
python -m pip install .
```

From a release wheel:

```powershell
python -m pip install .\dist\barprint-0.1.0-py3-none-any.whl
```

After installing, configure paths once:

```powershell
barprint configure --user
```

## BAR Data

The BAR data directory is commonly under one of these locations:

```text
C:\Users\<user>\AppData\Local\Programs\Beyond-All-Reason\data
C:\Program Files\Beyond-All-Reason\data
```

Unpacked game data is often here:

```text
C:\Users\<user>\AppData\Local\Programs\Beyond-All-Reason\data\games\BAR.sdd
```

The Windows launcher may instead store game content as rapid `.sdp` packages under `data\packages` with compressed files under `data\pool`. `barprint` can read that installed rapid layout for unit discovery and extracts individual S3O files only when needed for export.

Unit Lua files live under `BAR.sdd\units\**\*.lua` and usually contain an `objectname`, for example `objectname = "Units/CORAK.s3o"`. The model resolves to `BAR.sdd\objects3d\Units\CORAK.s3o`.

## Configuration Modes

Use a user config for regular installed use:

```powershell
barprint configure --user
```

Use a repo-local config while working from a checkout:

```powershell
python -m barprint configure --local
```

Use a portable workspace when you want config, importer, and cache files under one folder:

```powershell
python -m barprint configure --portable C:\tools\barprint-portable
$env:BARPRINT_PORTABLE_HOME = "C:\tools\barprint-portable"
```

The `$env:BARPRINT_PORTABLE_HOME` assignment is session-local in PowerShell. For later sessions, set it again, set it persistently, run commands from the portable folder, or pass the config file explicitly:

```powershell
barprint list-units --config C:\tools\barprint-portable\barprint.portable.json
```

Config discovery order is:

```text
--config
BARPRINT_CONFIG
.\barprint.local.json
active portable config
per-user config
```

Command-line arguments override config values.

You can also copy `barprint.config.example.json` to `barprint.local.json` and fill in paths manually:

```json
{
  "bar_root": "C:/Users/<you>/AppData/Local/Programs/Beyond-All-Reason/data/games/BAR.sdd",
  "blender": "C:/Program Files/Blender Foundation/Blender <version>/blender.exe",
  "s3o_importer": "C:/path/to/s3o_import.py",
  "scale_reference_unit": "armcom",
  "scale_reference_height_mm": 45,
  "test_s3o_path": "C:/path/to/model.s3o"
}
```

## S3O Importer

Blender does not import Spring/BAR `.s3o` files by default. `configure` installs the FluidPlay `s3o-Blender-plugins-2022` importer automatically when no importer is configured or discovered.

The install destination follows the config scope:

- `--local`: repo-local `vendor\`.
- `--user`: per-user data.
- `--portable`: the portable folder.

You can also place the importer at one of these repo-local paths:

```text
vendor\s3o-Blender-plugins-2022\s3o_import.py
vendor\s3o_import.py
barprint\vendor\s3o_import.py
```

For a custom importer path, pass it on the command line or save it in config:

```powershell
barprint export --unit corak --s3o-importer C:\path\to\s3o_import.py --out .\out\corak
```

## Check Setup

Run `doctor` when setup fails or before reporting an issue:

```powershell
barprint doctor
```

Machine-readable status is available for scripts:

```powershell
barprint doctor --json
```

`doctor` checks BAR data discovery, Blender discovery, the S3O importer, the cache directory, and debug viewer availability.

## List Units

List every discovered unit:

```powershell
barprint list-units
```

List one faction:

```powershell
barprint list-units --faction cortex
```

Useful filters:

```powershell
barprint list-units --faction armada
barprint list-units --faction scavs
barprint list-units --faction raptors
barprint list-units --kind building
barprint list-units --type bot
barprint list-units --type experimental
```

Useful grouping:

```powershell
barprint list-units --by-faction
barprint list-units --group-by kind
barprint list-units --group-by type
barprint list-units --group-by factory --kind unit
```

The output includes a `Code` column. Use that value with `barprint export`.

For one unit's metadata:

```powershell
barprint info --unit corak
```

## Export

Export a single STL from a configured BAR install:

```powershell
barprint export --unit corak --out .\out\corak
```

The output directory contains:

```text
out/corak/corak.stl
```

By default, `barprint` leaves only the final STL or 3MF. To also keep the manifest JSON and normalized print-source GLB:

```powershell
barprint export --unit corak --out .\out\corak --export-support-files
```

That adds:

```text
out/corak/corak_manifest.json
out/corak/corak_print_source.glb
```

If `--out` is omitted, output defaults to:

```text
out/<unit>/<unit>.stl
```

If you omit both `--unit` and `--s3o` in an interactive terminal, `export` prompts for a faction and unit, then exports the selected model. The selector shows the same names and descriptions as `list-units`.

Open the finished file manually in your slicer, or ask `barprint` to open it with the operating system default app:

```powershell
barprint export --unit corak --out .\out\corak --open
```

## Common Setup Failures

`Blender not found`: Install Blender, set `BLENDER_EXE`, or pass the path:

```powershell
barprint export --unit corak --blender "C:\Program Files\Blender Foundation\Blender <version>\blender.exe"
```

`S3O importer missing`: Run `barprint configure --user` again, or pass `--s3o-importer` with a compatible importer `.py` file or add-on `.zip`.

`Unit not found`: Run `barprint list-units --faction cortex` and use a value from the `Code` column, such as `corak`.

`Could not find BAR.sdd`: Pass `--bar-root` with your BAR data path, then save it with `barprint configure --user`.

`S3O import operator not registered`: The importer loaded but did not register the Blender import operator. Try a different importer version or Blender version.

`Model imported but no meshes found`: The importer may not support that file, or the import failed without a clear Blender error.
