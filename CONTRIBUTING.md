# Contributing

Contributions are welcome after the repository has been published.

## Development setup

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-validation.txt
```

## Before opening a pull request

Run:

```bash
python -W error -m py_compile GLO30_Download.py tests/test_downloader.py
python -m unittest discover -s tests -v
```

Keep tests offline: they should not require CDSE credentials, live network access, or actual DEM downloads.

## Design principles

Changes should preserve:

- exact dataset/release filtering;
- count-reconciled catalogue discovery;
- local-first reuse;
- no credential storage;
- bounded retries and responsive interruption;
- reproducible manifests/receipts/audits;
- explicit failure rather than silent omission when coverage, permissions, or metadata are ambiguous.

If adding another Copernicus DEM product profile, keep product-specific output/metadata separated so unlike formats/releases cannot be mixed accidentally.

## Data and licensing

Do not add downloaded Copernicus DEM rasters to the repository. The repository's MIT License applies to the software, not to third-party source data.
