# Security policy

## Credentials

`GLO30_Download.py` requests CDSE credentials interactively only when protected downloads are required. Passwords and OAuth access tokens are kept in memory and are not intentionally written to manifests, receipts, configuration files, or normal logs.

Do not hard-code credentials in the script. Do not commit credential files, `.env` files, tokens, or local `config.local.toml` files containing sensitive information.

## Repository hygiene

The supplied `.gitignore` excludes common secret/config patterns, downloaded DEM rasters, logs, partial files, caches, and editor-specific files. Review `git status` before every push.

## Reporting a vulnerability

If you publish this repository, configure GitHub's private vulnerability reporting or provide a maintainer security contact before inviting public security reports.

## Access authorization

This project does not bypass CDSE permissions. Persistent HTTP 403 responses are treated as authorization failures. Users must obtain valid Copernicus Contributing Missions access through CDSE.
