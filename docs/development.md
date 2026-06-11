# Development

This page is for contributors working from a source checkout.

## Editable Install

Create and activate your Python environment, then install the package with development dependencies:

```powershell
python -m pip install -e ".[dev]"
python -m barprint configure --local
python -m barprint doctor
```

Use repo-local config while developing so local paths stay out of user config:

```powershell
python -m barprint configure --local
```

Generated config and output files are intentionally ignored by git.

## Tests

Run the normal test suite:

```powershell
python -m pytest --basetemp tmp\pytest-release -p no:cacheprovider
```

Run a focused CLI and config pass:

```powershell
python -m pytest tests\test_cli.py tests\test_config.py --basetemp tmp\pytest-readme-rewrite -p no:cacheprovider
```

Run the optional Blender integration test:

```powershell
python -m pytest tests\test_integration_blender.py --basetemp tmp\pytest-release-integration -p no:cacheprovider
```

The integration test reads `BLENDER_EXE`, `TEST_S3O_PATH`, and `S3O_IMPORTER_PATH`, or the `blender`, `test_s3o_path`, and `s3o_importer` values from local config. If `test_s3o_path` is omitted, it derives a representative model from configured BAR data using `scale_reference_unit`, falling back to `armcom`.

## Export Audit

The optional all-unit audit is expensive. Enable it only when validating release readiness or broad pipeline changes:

```powershell
$env:BARPRINT_EXPORT_AUDIT = "1"
python -m pytest tests\test_export_audit.py --basetemp tmp\barprint-release-full-audit -p no:cacheprovider
```

Run a focused subset:

```powershell
$env:BARPRINT_EXPORT_AUDIT = "1"
$env:BARPRINT_EXPORT_AUDIT_UNITS = "armcom,corak"
python -m pytest tests\test_export_audit.py --basetemp tmp\barprint-release-focused-audit -p no:cacheprovider
```

The audit exports discovered units through the automatic pose-profile, opaque print-source STL path, validates final STL bounds and topology, then writes metrics and debug-stage artifacts for failures.

## Build and Packaging

Build source and wheel distributions:

```powershell
python -m build --sdist --wheel --outdir tmp\dist-release
```

For README or docs-only packaging checks, use a dedicated output folder:

```powershell
python -m build --sdist --wheel --outdir tmp\dist-readme-rewrite
```

The sdist should include:

```text
README.md
docs/*.md
docs/images/*.png
barprint/**
tests/**
LICENSE
pyproject.toml
barprint.config.example.json
```

## Release Checklist

Before tagging or publishing a release:

```powershell
python -m pytest --basetemp tmp\pytest-release -p no:cacheprovider
python -m pytest tests\test_integration_blender.py --basetemp tmp\pytest-release-integration -p no:cacheprovider
$env:BARPRINT_EXPORT_AUDIT = "1"
python -m pytest tests\test_export_audit.py --basetemp tmp\barprint-release-full-audit -p no:cacheprovider
python -m build --sdist --wheel --outdir tmp\dist-release
```

Keep generated audit and build artifacts under `tmp\`; they are intentionally ignored by git.

## Contribution Guidance

Keep changes scoped to the behavior being changed. Prefer the existing CLI, config, Blender pipeline, and test patterns over new abstractions.

Add tests when a change affects CLI behavior, config discovery, unit discovery, pose selection, geometry processing, output naming, or packaging.

Do not commit BAR assets, extracted rapid package files, generated STL/3MF/GLB output, debug-stage folders, local config files, virtual environments, build output, or audit output.

Use BAR assets only in ways allowed by their licenses and terms. This repository does not grant rights to BAR game assets, models, textures, names, trademarks, or generated derivatives.
