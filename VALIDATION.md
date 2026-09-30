# Validation scope

## Offline automated checks

Version 5.1.0 passes **42 offline tests** in the repository's `unittest` suite and Python compilation with warnings treated as errors.

The tests cover, among other things:

- safe default behavior when nothing is selected;
- ISO3 normalization and validation;
- catalogue enumeration beyond one page;
- exactly one authoritative catalogue count query;
- duplicate catalogue detection and deterministic filename deduplication;
- dateline-crossing boundaries and polygon holes;
- one download job for a tile shared across output targets;
- HTTP 403 fail-fast behavior and Retry-After preservation;
- cooperative cancellation and retained partial files;
- resumed and restarted byte-range behavior;
- local-first adoption without protected network access;
- no credential request when all required files are already local;
- mocked download-to-audit workflow;
- SHA-256 change detection;
- DGED GeoTIFF signature handling;
- documented DGED (`*_DEM.tif`) and DTED (`*_DEM.dt2`) filename conventions;
- DTED `UHL` signature recognition;
- optional Rasterio block-read validation when Rasterio is installed.

Run locally:

```bash
python -W error -m py_compile GLO30_Download.py tests/test_downloader.py
python -m unittest discover -s tests -v
```

## Live-service validation

The DGED workflow has been exercised successfully against CDSE through catalogue enumeration, CCM authentication, local reuse, and protected GLO-30 GeoTIFF downloads on Windows/PowerShell.

This repository does **not** include credentials and CI does not perform authenticated CDSE integration tests.

DTED support is based on the official Copernicus DEM dataset IDs and documented DTED Level 2 product naming/structure. The repository has offline tests for DTED naming and file-header recognition, but no authenticated live DTED integration test is claimed here.

## What validation does not prove

The downloader's audit establishes consistency with its recorded catalogue/manifests/receipts and optional local hashes. It does not independently validate elevation accuracy against ground truth, convert a DSM to a DTM, or certify scientific fitness for a particular analysis.

An adopted unreceipted local file is validated by catalogue-derived filename plus the selected file-format signature; unless deliberately re-downloaded, it is not compared byte-for-byte against a provider-issued checksum.
