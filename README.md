# GLO-30 Download

A reproducible Python downloader for the **Copernicus DEM GLO-30** from the Copernicus Data Space Ecosystem (CDSE). It can download the full global collection or whole 1-degree tiles intersecting one or more ISO3 countries, reuses files already present locally, resumes interrupted transfers, and audits the final output against a count-verified CDSE catalogue snapshot.

The default product is **COP-DEM_GLO-30-DGED/2024_1** (GeoTIFF). The script also includes an explicit DGED/DTED product selector.

## What this tool does

- queries the official CDSE OData catalogue for the exact selected GLO-30 product/release;
- verifies catalogue completeness against one authoritative server count without walking past CDSE's deep-pagination limit;
- selects either the full global collection or whole tiles intersecting ISO3 country boundaries;
- scans local files first and skips protected download requests for files already present;
- prompts for CDSE credentials only when files are actually missing;
- downloads with bounded retries, resumable partial files, and cooperative Ctrl+C handling;
- deduplicates catalogue records, filenames, and cross-country download jobs;
- writes manifests, receipts, logs, and audit reports for reproducibility;
- optionally verifies SHA-256 hashes and performs a deeper raster-read audit with Rasterio.

It does **not** mosaic, clip, resample, hydrologically condition, or convert the Copernicus DEM from a DSM to a bare-earth DTM.

## Requirements

### 1. Python

Python **3.11 or newer** is recommended.

Install the required packages:

```bash
python -m pip install -r requirements.txt
```

Optional deep raster validation:

```bash
python -m pip install -r requirements-validation.txt
```

### 2. Copernicus Data Space Ecosystem account

You need a CDSE account for protected downloads:

- https://dataspace.copernicus.eu/

### 3. Copernicus Contributing Missions (CCM) access

**GLO-30 access requires CCM registration/authorization.** Registering for a normal CDSE account is not always sufficient by itself.

Current CDSE guidance requires users who access Copernicus DEM GLO-30 to be registered for Copernicus Contributing Missions data. In the CDSE identity/profile interface, enable the option indicating that you are interested in accessing **Copernicus Contributing Missions data** and complete any authorization steps requested for your account category.

Official references:

- CCM documentation: https://documentation.dataspace.copernicus.eu/Data/Others/CCM.html
- Copernicus DEM collection: https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM
- CDSE announcement on GLO-30 CCM access: https://dataspace.copernicus.eu/news/2026-8-25-copernicus-dem-30m-view-service-update

If CDSE returns **HTTP 403**, the script stops promptly. A 403 generally means the authenticated account does not currently have permission for the requested protected product; the script cannot grant CCM access itself.

### 4. Storage and network access

A full global download is large. Make sure the destination volume has substantial free disk space and a stable network connection before enabling `DOWNLOAD_ALL`.

## Product selection

Near the top of `GLO30_Download.py`, enable exactly one product profile:

```python
PRODUCTS = {
    "DGED": True,
    "DTED": False,
}
```

Exactly **one** entry must be `True`.

| Setting | CDSE dataset | Main DEM file | Numeric representation |
|---|---|---|---|
| `DGED` | `COP-DEM_GLO-30-DGED/2024_1` | GeoTIFF (`*_DEM.tif`) | 32-bit floating point |
| `DTED` | `COP-DEM_GLO-30-DTED/2024_1` | DTED Level 2 (`*_DEM.dt2`) | 16-bit signed |

DGED is the default and is the profile used for the primary live workflow validation. DTED support follows the documented Copernicus DEM DTED package naming/structure and has offline tests for naming and file signatures; see `VALIDATION.md`.

The downloader retrieves the **DEM height raster only**, not the additional DGED quality layers such as EDM, FLM, HEM, or WBM.

## Quick start

Edit the user settings near the top of `GLO30_Download.py`:

```python
ROOT_DIR = Path.home() / "GIS_Data"

PRODUCTS = {
    "DGED": True,
    "DTED": False,
}

DOWNLOAD_ALL = False
COUNTRIES = {
    "THA": False,
    "NER": False,
    "NGA": False,
    "GHA": False,
    "SEN": False,
}
```

For example, to download Thailand and Niger:

```python
COUNTRIES = {
    "THA": True,
    "NER": True,
}
```

Or use the command line without editing the country dictionary:

```bash
python GLO30_Download.py --root D:\GIS_Data --iso3 THA NER
```

For a full global download:

```bash
python GLO30_Download.py --root D:\GIS_Data --download-all
```

Always consider a dry run first:

```bash
python GLO30_Download.py --root D:\GIS_Data --iso3 THA NER --dry-run
```

## Local-first behavior

Before asking for credentials, the script scans the relevant local output/reuse folders once and separates the plan into:

```text
FOUND LOCALLY      -> no protected CDSE download request
NOT FOUND LOCALLY  -> authenticate, then download
```

Typical console output is intentionally human-readable:

```text
LOCAL CHECK COMPLETE | required 125 | found locally 120 | not found locally 5
DOWNLOAD PLAN | only the 5 missing tiles will contact the protected CDSE download service
LOGIN REQUIRED | 5 tiles were not found locally
CDSE username/email:
CDSE password:
CDSE LOGIN | successful
NOT FOUND LOCALLY ... | download required
DOWNLOADING ...
DOWNLOAD COMPLETE ...
```

The progress heartbeat is paused while credentials are being entered.

## Output layout

With DGED selected:

```text
<ROOT_DIR>/
└── GLO_30/
    ├── Global_GLO30/
    ├── THA/
    ├── NER/
    ├── _metadata/
    │   └── 2024_1/
    │       ├── catalogue_verified.json
    │       ├── manifests/
    │       ├── boundaries/
    │       ├── receipts/
    │       ├── partials/
    │       └── quarantine/
    └── logs/
```

With DTED selected, product-specific folders avoid collisions with DGED:

```text
GLO_30/
├── Global_GLO30_DTED/
├── THA_DTED/
└── _metadata/
    └── DTED/
        └── 2024_1/
```

## Country selection

Country mode uses whole Copernicus 1-degree cells that intersect the selected ADM0 geometry. It does **not** clip the DEM to the national boundary.

By default, ADM0 boundaries are retrieved from the geoBoundaries `gbOpen` API and cached. You can override a country with your own WGS84 lon/lat GeoJSON:

```python
BOUNDARY_FILES = {
    "THA": Path(r"D:\Boundaries\THA_ADM0.geojson"),
}
```

A shared border tile can appear in more than one country folder while being downloaded only once.

## Configuration file

Copy `config.example.toml` to `config.local.toml` and edit it. Keep local configuration files out of Git.

```bash
python GLO30_Download.py --config config.local.toml --dry-run
```

The DGED/DTED product selector intentionally stays in the clearly visible `PRODUCTS` block near the top of the script, because product format changes filename and output-layout assumptions.

## Useful commands

List recognized ISO3 country codes:

```bash
python GLO30_Download.py --list-countries
```

Audit an existing global DGED collection without network access:

```bash
python GLO30_Download.py --download-all --audit-only
```

Audit selected countries:

```bash
python GLO30_Download.py --iso3 THA NER --audit-only
```

Refresh the CDSE catalogue and country boundaries:

```bash
python GLO30_Download.py --iso3 THA NER --refresh-catalogue --refresh-boundaries --dry-run
```

Test with only a small number of jobs:

```bash
python GLO30_Download.py --download-all --max-products 5
```

`--max-products` is explicitly a test mode. It does not make a complete-download claim.

## Catalogue completeness

CDSE documents a maximum OData `$top` of 1,000 and a deep `$skip` limit of 10,000, and notes that `$count` can be expensive. This downloader therefore:

1. requests the exact release-wide count once;
2. obtains the catalogue date range without another count;
3. enumerates ordered pages inside bounded date windows;
4. subdivides a window before approaching the provider's deep-pagination ceiling;
5. refuses to save a complete inventory unless retrieved rows and distinct product IDs both equal the authoritative count.

There is no hard-coded global tile count.

## Existing files and integrity

For local-first reuse, an existing file must have the exact catalogue-derived filename and a valid signature for the selected product format. If a matching local receipt exists, product ID, dataset, recorded size, and file state are also checked.

For an unreceipted legacy file, local adoption avoids a protected server-size probe. That is efficient, but it is not a provider-issued checksum comparison. Use `--verify-hashes`, `--deep-audit`, or deliberately re-download questionable files when stronger verification is needed.

`COPY_MODE = "copy"` creates independent copies when one source tile belongs in several output folders. `COPY_MODE = "hardlink"` can avoid duplicate storage on compatible same-volume filesystems, but hardlinked files share the same bytes and should be treated as read-only source data.

## Authentication and credential handling

Credentials are requested interactively only if protected downloads are needed. Passwords and access tokens are not written to configuration files, manifests, receipts, or normal logs.

Do not hard-code credentials into the script and do not commit local credential material to GitHub.

## Data licensing and attribution

The **software in this repository** is licensed under the MIT License. That license does **not** replace or modify the license/terms that apply to Copernicus DEM data downloaded from CDSE.

CDSE publishes separate Copernicus DEM licensing and attribution obligations. See `DATA_SOURCES.md` and the official collection page before redistributing or publishing derived products.

For research use, CDSE lists the Copernicus DEM DOI:

- https://doi.org/10.5270/ESA-c5d3d65

## Tests

Run the offline test suite:

```bash
python -m unittest discover -s tests -v
```

Compile with warnings treated as errors:

```bash
python -W error -m py_compile GLO30_Download.py tests/test_downloader.py
```

The tests do not require CDSE credentials or live network access. See `VALIDATION.md` for the validation scope and limitations.

## License

Code: **MIT License**. See `LICENSE`.

Copernicus DEM data: separate provider terms apply. See `DATA_SOURCES.md`.
