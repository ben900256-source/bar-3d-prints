# barprint

`barprint` turns Beyond All Reason unit models into local 3D-print files. It finds BAR units, imports their S3O models through Blender, repairs common game-mesh issues, scales the model, and writes STL or 3MF output that you can open in slicer software.

The tool runs on your computer. It does not upload BAR assets or generated models.

## Examples

These sample renders are untextured captures of final STL output from `barprint`.

| CORAK bot | CORPYRO heavy bot | CORMSHIP naval unit |
| --- | --- | --- |
| ![CORAK sample STL render](docs/images/corak-sample.png) | ![CORPYRO sample STL render](docs/images/corpyro-sample.png) | ![CORMSHIP sample STL render](docs/images/cormship-sample.png) |

## Quickstart

Requirements: Windows PowerShell, Python 3.10 or newer, Blender, and an installed Beyond All Reason data directory.

```powershell
git clone https://github.com/ben900256-source/bar-3d-prints.git
cd bar-3d-prints
python -m pip install .
barprint configure --user
barprint list-units --faction cortex
barprint export --unit corak --out .\out\corak
```

`configure --user` looks for Blender and BAR data, then saves the paths for later commands. If the BAR data path is not found automatically, it asks you to paste the path.

`list-units --faction cortex` prints a table like this:

```text
                                             Cortex (1)
+-------+-------+-----------------+------+------+--------------------------+
| Code  | Name  | Description     | Kind | Type | Source                   |
|-------+-------+-----------------+------+------+--------------------------|
| corak | corak | Units/CORAK.s3o | unit |      | objects3d/Units/CORAK.s3o |
+-------+-------+-----------------+------+------+--------------------------+
```

Use the `Code` column with `barprint export`. In the example above, the code is `corak`.

After the export, expect these files:

```text
out/corak/corak.stl
out/corak/corak_manifest.json
out/corak/corak_print_source.glb
```

Open `corak.stl` in your slicer. The manifest records the source model, scale, pose profile, mesh repair steps, and Blender version used for the export. The GLB is the normalized print source used by the final STL step.

## More Docs

- [Usage](docs/usage.md): install details, BAR data paths, configuration modes, setup checks, listing units, exporting, and common failures.
- [Advanced](docs/advanced.md): poses, variants, scaling, bases, STL/3MF options, debug-stage output, manifests, explicit S3O paths, and printability tuning.
- [Development](docs/development.md): editable installs, tests, export audits, release checks, and contribution guidance.

## Troubleshooting

Run this first when setup or export fails:

```powershell
barprint doctor
```

Common fixes are installing Blender, running `barprint configure --user` again, checking the BAR data path, or using a unit code from `barprint list-units`.

## License

This repository's code and documentation are source-available under the PolyForm Noncommercial License 1.0.0. Commercial use requires separate permission from the copyright holders.

This repository does not grant rights to Beyond All Reason game assets, models, textures, names, trademarks, or generated derivatives. Use BAR assets only in ways allowed by their licenses and terms.
