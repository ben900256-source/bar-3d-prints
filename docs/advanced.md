# Advanced Usage

This page covers options that are useful after the basic `barprint export --unit corak --out .\out\corak` workflow is working.

## Poses

BAR models are piece hierarchies rather than skinned characters. Pose profiles rotate named mesh or empty pieces around their existing pivots. Piece matching is case-insensitive and prefers exact name, then prefix, then substring.

When `--pose-profile` is omitted, `barprint export --unit ...` chooses a built-in profile from BAR metadata and S3O piece names. Current automatic archetypes cover small bots, large or experimental bots, tick and flea scouts, vehicles, turrets, buildings, and raptors.

Export all poses defined by the selected profile:

```powershell
barprint export --unit corak --pose all --out .\out\corak
```

The standard biped and tick-style pose set is:

```text
neutral
aim_left
aim_right
stride_left
stride_right
brace
advance
```

Vehicle, turret, building, and naval profiles may use smaller pose sets.

Generate random poses from the selected profile:

```powershell
barprint export --unit corak --random-poses 5 --out .\out\corak
```

Force a profile explicitly:

```powershell
barprint export `
  --unit armflea `
  --pose-profile .\barprint\profiles\tick.json `
  --pose neutral `
  --out .\out\armflea
```

## Variants

Some pose profiles define print variants. For example, the commander profile can export a standard variant or a decorated variant with optional pieces repositioned.

```powershell
barprint export --unit armcom --variant decorated --out .\out\armcom
barprint export --unit armcom --variant all --out .\out\armcom
```

Variant output adds a suffix when needed, such as:

```text
out/armcom/armcom_decorated.stl
```

## Scaling

Profiles default to game-relative scale. The Armada commander (`armcom`) is exported at 45 mm tall, and other units use the same source-to-mm ratio.

Override the reference unit:

```powershell
barprint export `
  --unit corak `
  --scale-mode game-relative `
  --scale-reference-unit armcom `
  --scale-reference-height-mm 45 `
  --out .\out\corak
```

Export one model at an absolute target height:

```powershell
barprint export --unit corak --scale-mm 40 --out .\out\corak
```

Set defaults in `barprint.local.json`:

```json
{
  "scale_reference_unit": "armcom",
  "scale_reference_height_mm": 45
}
```

Use `scale_reference_unit: "tallest"` if you want the older tallest-unit reference behavior.

## Bases

Profiles default to no round base. Add one when wanted:

```powershell
barprint export --unit corak --base --out .\out\corak
```

Tune the base size:

```powershell
barprint export `
  --unit corak `
  --base `
  --base-diameter-mm 32 `
  --base-height-mm 2.4 `
  --out .\out\corak
```

Disable a profile base explicitly:

```powershell
barprint export --unit corak --no-base --out .\out\corak
```

## Output Formats

STL is the default:

```powershell
barprint export --unit corak --format stl --out .\out\corak
```

3MF is also supported:

```powershell
barprint export --unit corak --format 3mf --out .\out\corak
```

With multiple poses or variants, output names include the pose or variant:

```text
out/corak/corak_neutral.stl
out/corak/corak_aim_left.stl
out/corak/corak_stride_right.stl
```

Keep a raw pre-repair STL snapshot:

```powershell
barprint export --unit corak --keep-raw --out .\out\corak
```

## Debug Stages

Use `--debug-stages` when you need to inspect how the pipeline changed a model:

```powershell
barprint export --unit corak --debug-stages --out .\out\corak
```

For STL exports, this writes a debug folder beside the output with stage renders, GLB/STL snapshots, a `stage_report.json`, and a standalone debug viewer HTML file.

Open the debug viewer through a local HTTP server:

```powershell
barprint view .\out\corak\corak_debug
barprint view .\out\corak\corak_debug\corak_debug_viewer.html
```

`barprint view` serves the containing folder so GLB/STL assets load correctly in browsers that block `file://` model fetches. The viewer is self-contained HTML with an embedded WebGL STL/GLB renderer. It does not require npm or a CDN.

If you exported multiple poses with debug stages, `barprint` also writes a multi-row viewer next to the output base.

## Manifests and JSON

Every export writes a manifest next to the model:

```text
out/corak/corak_manifest.json
```

The manifest records the source S3O, pose, pose profile name, archetype, scale, mesh closure, thin feature thickening, base settings, warnings, and Blender version.

Check setup as JSON:

```powershell
barprint doctor --json
```

Inspect a unit or direct S3O path:

```powershell
barprint inspect --unit corak
barprint inspect --s3o .\BAR.sdd\objects3d\Units\CORAK.s3o
```

Inspect imported Blender pieces as JSON:

```powershell
barprint inspect `
  --s3o .\BAR.sdd\objects3d\Units\CORAK.s3o `
  --with-pieces `
  --s3o-importer .\vendor\s3o-Blender-plugins-2022\s3o_import.py
```

## Explicit S3O Paths

Use `--s3o` when you want to bypass unit discovery:

```powershell
barprint export `
  --s3o ".\BAR.sdd\objects3d\Units\CORAK.s3o" `
  --pose-profile .\barprint\profiles\bot_small.json `
  --pose neutral `
  --out .\out\corak.stl `
  --s3o-importer .\vendor\s3o-Blender-plugins-2022\s3o_import.py
```

You can also combine `--s3o` with absolute scale when no BAR metadata is available:

```powershell
barprint export --s3o .\model.s3o --scale-mm 45 --out .\out\model
```

## Printability Tuning

BAR models are game meshes. They may contain very thin barrels, floating pieces, holes, overlapping surfaces, or details that do not survive FDM printing.

`barprint` imports and poses the S3O, forces print-source materials to opaque beige, writes a normalized `*_print_source.glb`, reloads that GLB, welds and caps mesh boundary loops, rebuilds some closed residual non-manifold pieces as convex hulls, thickens thin features, optionally adds a base, and exports the final STL or 3MF.

Tune thin feature handling:

```powershell
barprint export `
  --unit corak `
  --min-feature-mm 0.8 `
  --thin-feature-max-inflate-mm 0.4 `
  --out .\out\corak
```

Disable thin feature expansion:

```powershell
barprint export --unit corak --no-thin-features --out .\out\corak
```

For difficult models, use `--debug-stages` and compare the normalized GLB, post-thickening STL, and final STL before changing print settings.
