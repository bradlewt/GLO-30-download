# Changelog

## 5.1.0 — 2026-09-30

- Renamed the project to **GLO-30 Download** and the main script to `GLO30_Download.py`.
- Adopted the MIT License for the software repository and clarified that Copernicus DEM data remain subject to separate provider terms.
- Added an explicit true/false product selector for GLO-30 **DGED** and **DTED** profiles; exactly one must be enabled.
- Preserved DGED as the default and retained its historical output/metadata layout for backward compatibility.
- Added DTED Level 2 filename/signature handling and collision-safe DTED output folders.
- Expanded README requirements with the need for CDSE **Copernicus Contributing Missions (CCM)** registration/authorization.
- Removed the obsolete `GLO30_2024_1_TIF` fallback directory from automatic reuse discovery; `Global_GLO30` is now the normal DGED global store.
- Generalized local reuse/audit wording from TIFF-specific to raster-format-aware checks.
- Added offline tests for documented DGED/DTED filename conventions and DTED header recognition.
- Updated repository metadata, data-source notes, migration notes, validation notes, and CI naming.

## 5.0.2 — 2026-09-29

- Changed the workflow to local-first reuse: scan local files once, authenticate only if required files are missing, and avoid protected remote-size probes for adopted local files.
- Paused the progress heartbeat while interactive credentials are entered.
- Reworded console output around `FOUND LOCALLY`, `NOT FOUND LOCALLY`, `DOWNLOADING`, and `DOWNLOAD COMPLETE`.
- Replaced the rejected HEAD size check with a single ranged GET metadata probe for files that truly require downloading.

## 5.0.1 — 2026-09-29

- Reworked catalogue enumeration to use one authoritative `$count` request and ordered date-window pagination rather than repeated expensive count probes.
- Added catalogue-specific progress output and completeness reconciliation.

## 5.0.0

- Introduced count-verified catalogue discovery, ISO3 country selection, deduplicated jobs, resumable downloads, receipts, audits, and a GitHub-oriented package structure.
