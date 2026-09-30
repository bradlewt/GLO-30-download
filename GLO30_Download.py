#!/usr/bin/env python3
r"""GLO-30 Download: retrieve Copernicus DEM GLO-30 2024_1 globally or by ISO3.

Python 3.11+. Install: python -m pip install -r requirements.txt
Edit the USER SETTINGS below, or use --config config.local.toml / --help.
A CDSE account with Copernicus Contributing Missions (CCM) access is required
for protected downloads. Credentials are prompted only when required files are
missing locally. Dry runs and offline audits do not request credentials.
No mosaicking, clipping, resampling, or bare-earth conversion is performed.
"""
from __future__ import annotations

import argparse
import csv
import errno
import getpass
import hashlib
import importlib
import json
import logging
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
import tomllib
import uuid
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import pycountry
import requests
from shapely import STRtree, make_valid
from shapely.affinity import translate
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union

# USER SETTINGS ---------------------------------------------------------------
# ROOT_DIR is the parent of GLO_30, NOT the GLO_30 folder itself.
ROOT_DIR = Path.home() / "GIS_Data"

# Choose exactly ONE Copernicus DEM GLO-30 product profile.
# DGED = GeoTIFF, 32-bit floating point. DTED = DTED Level 2, 16-bit signed.
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
}  # Add any number of ISO 3166-1 alpha-3 codes with True / False values.
WORKERS = 4
COPY_MODE = "copy"  # "hardlink" saves space on the same volume; falls back to copy.
EXISTING_RASTER_DIRS = []  # Optional folders containing this SAME product/release.
BOUNDARY_FILES = {}  # Optional ISO3: path to a WGS84 lon/lat ADM0 GeoJSON.
VERIFY_HASHES = False  # Compare files against locally recorded SHA-256 values.
DEEP_AUDIT = False  # Read all raster blocks; requires optional rasterio package.

# IMPLEMENTATION SETTINGS -----------------------------------------------------
VERSION = "5.1.0"
SCHEMA = 6
RELEASE = "2024_1"
PRODUCT_SPECS = {
    "DGED": {
        "dataset_base": "COP-DEM_GLO-30-DGED",
        "extension": ".tif",
        "description": "DGED GeoTIFF (32-bit floating point)",
    },
    "DTED": {
        "dataset_base": "COP-DEM_GLO-30-DTED",
        "extension": ".dt2",
        "description": "DTED Level 2 (16-bit signed)",
    },
}
_enabled_products = [name for name, enabled in PRODUCTS.items() if enabled]
if len(_enabled_products) != 1 or _enabled_products[0] not in PRODUCT_SPECS:
    raise RuntimeError("Set exactly one PRODUCTS entry to True: DGED or DTED")
PRODUCT_FORMAT = _enabled_products[0]
PRODUCT_SPEC = PRODUCT_SPECS[PRODUCT_FORMAT]
DATASET = f"{PRODUCT_SPEC['dataset_base']}/{RELEASE}"
RASTER_EXTENSION = PRODUCT_SPEC["extension"]
GLOBAL_FOLDER = "Global_GLO30" if PRODUCT_FORMAT == "DGED" else "Global_GLO30_DTED"
CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD_BASE = "https://download.dataspace.copernicus.eu/odata/v1"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
BOUNDARY_API = "https://www.geoboundaries.org/api/current/gbOpen/{iso3}/ADM0/"
FILTER = ("Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'dataset' and "
          f"att/OData.CSC.StringAttribute/Value eq '{DATASET}')")
TIMEOUT = (15, 60)  # Connect timeout, then inactivity timeout; NOT a total deadline.
PAGE_SIZE = 1000
PARTITION_LIMIT = 8000  # Probe at this safe $skip value; split before approaching 10000.
CATALOGUE_WINDOW_DAYS = 180  # Coarse date windows avoid expensive whole-release pagination.
CHUNK_SIZE = 1024 * 1024
HEARTBEAT_SECONDS = 10
MAX_ATTEMPTS = 5
LOG = logging.getLogger("glo30.download")
STOP = threading.Event()


class Cancelled(Exception):
    """Cooperative cancellation, never retried."""


class PermanentError(Exception):
    """Invalid input, permissions, or a non-retryable server response."""


class RetryableError(Exception):
    """Temporary transfer or service failure, optionally with a server retry delay."""
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Settings:
    root: Path
    download_all: bool = False
    countries: list[str] = field(default_factory=list)
    workers: int = WORKERS
    copy_mode: str = COPY_MODE
    reuse_dirs: list[Path] = field(default_factory=list)
    boundary_files: dict[str, Path] = field(default_factory=dict)
    verify_hashes: bool = False
    deep_audit: bool = False
    dry_run: bool = False
    audit_only: bool = False
    refresh_catalogue: bool = False
    refresh_boundaries: bool = False
    max_products: int | None = None

    @property
    def base(self):
        return self.root / "GLO_30"

    @property
    def meta(self):
        # Preserve the historical DGED metadata path for backward compatibility.
        if PRODUCT_FORMAT == "DGED":
            return self.base / "_metadata" / RELEASE
        return self.base / "_metadata" / PRODUCT_FORMAT / RELEASE


def check_stop():
    if STOP.is_set():
        raise Cancelled("Stop requested")


def pause(seconds):
    if STOP.wait(seconds):
        raise Cancelled("Stop requested during retry wait")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def duration(seconds):
    if seconds is None or not math.isfinite(seconds):
        return "--"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}h {minutes:02d}m {seconds:02d}s"


def human_bytes(value):
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:,.2f} {unit}"
        value /= 1024


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=True, allow_nan=False)
        handle.write("\n")
    os.replace(temp, path)


def load_json(path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while data := handle.read(4 * CHUNK_SIZE):
            check_stop()
            digest.update(data)
    return digest.hexdigest()


def raster_header_ok(path, product_format=None):
    """Lightweight signature check for the selected GLO-30 raster format."""
    product_format = product_format or PRODUCT_FORMAT
    try:
        with path.open("rb") as handle:
            header = handle.read(4)
    except OSError:
        return False
    if product_format == "DGED":
        return header in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
    if product_format == "DTED":
        return header[:3] == b"UHL"
    return False


def tiff_header_ok(path):
    """Backward-compatible helper retained for callers/tests that expect TIFF validation."""
    return raster_header_ok(path, "DGED")


def dem_filename_for(tile, product_format=None):
    product_format = product_format or PRODUCT_FORMAT
    extension = PRODUCT_SPECS[product_format]["extension"]
    return tile + "_DEM" + extension


def grid_info(grid_id):
    match = re.fullmatch(r"([NS])(\d{1,2})_([EW])(\d{1,3})", grid_id.upper())
    if not match:
        raise PermanentError(f"Invalid gridId: {grid_id!r}")
    ns, lat, ew, lon = match.groups()
    lat, lon = int(lat), int(lon)
    if lat > 90 or lon > 180:
        raise PermanentError(f"Out-of-range gridId: {grid_id}")
    y, x = lat * (1 if ns == "N" else -1), lon * (1 if ew == "E" else -1)
    x = -180 if x == 180 else x
    if not -90 <= y < 90:
        raise PermanentError(f"Invalid southwest tile latitude: {grid_id}")
    tile = f"Copernicus_DSM_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00"
    return tile, box(x, y, x + 1, y + 1)


def normalize_product(item):
    attributes = item.get("Attributes", [])
    values = lambda name: [str(a["Value"]) for a in attributes if a.get("Name") == name]
    if DATASET not in values("dataset"):
        raise PermanentError("Catalogue response lacks the requested release attribute; refusing to omit it.")
    grids = values("gridId")
    if len(set(grids)) != 1:
        raise PermanentError(f"Missing or ambiguous gridId for {item.get('Id')}")
    tile, _ = grid_info(grids[0])
    product_id = str(uuid.UUID(item["Id"]))
    name = str(item["Name"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise PermanentError(f"Unsafe or unexpected product name for {product_id}")
    return {
        "product_id": product_id, "product_name": name, "grid_id": grids[0].upper(),
        "tile_name": tile, "dem_filename": dem_filename_for(tile), "dataset": DATASET,
        "publication_date": item.get("PublicationDate", ""),
        "modification_date": item.get("ModificationDate", ""),
        "s3_path": item.get("S3Path", ""), "online": item.get("Online", True),
    }


def deduplicate(rows):
    by_id, by_file = {}, defaultdict(list)
    for row in rows:
        previous = by_id.get(row["product_id"])
        if previous and previous != row:
            raise PermanentError("Conflicting metadata for the same product ID")
        by_id[row["product_id"]] = row
    for row in by_id.values():
        by_file[row["dem_filename"].casefold()].append(row)
    chosen, decisions = [], []
    for candidates in by_file.values():
        # Deterministic choice only among products in the exact requested release.
        ordered = sorted(candidates, key=lambda r: (r.get("online", True),
                         r.get("modification_date", ""), r.get("publication_date", ""), r["product_id"]),
                         reverse=True)
        chosen.append(ordered[0])
        for row in ordered:
            if len(ordered) > 1:
                decisions.append({**row, "selected": row is ordered[0], "alternatives": len(ordered)})
    return sorted(chosen, key=lambda r: r["dem_filename"]), decisions


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.download_started = None
        self.stage = "startup"
        self.total = 0
        self.counts = Counter()
        self.network_bytes = 0
        self.tile_seconds = 0.0
        self.active = {}
        self.catalogue_expected = 0
        self.catalogue_verified = 0
        self.catalogue_scanning = 0
        self.catalogue_requests = 0
        self.catalogue_interval = ""
        self.local_checked = 0
        self.local_found = 0
        self.local_missing = 0
        self.heartbeat_paused = False

    def stage_set(self, stage, message=None):
        with self.lock:
            self.stage = stage
        LOG.info("STAGE %s", message or stage)

    def pause_heartbeat(self, paused=True):
        with self.lock:
            self.heartbeat_paused = paused

    def local_scan_begin(self, total):
        with self.lock:
            self.total = total
            self.local_checked = 0
            self.local_found = 0
            self.local_missing = 0

    def local_scan_update(self, *, found=False, missing=False):
        with self.lock:
            self.local_checked += 1
            if found:
                self.local_found += 1
            if missing:
                self.local_missing += 1

    def catalogue_begin(self, expected):
        with self.lock:
            self.catalogue_expected = expected
            self.catalogue_verified = 0
            self.catalogue_scanning = 0
            self.catalogue_requests = 0
            self.catalogue_interval = ""

    def catalogue_scan(self, interval, scanned, *, request=False):
        with self.lock:
            self.catalogue_interval = interval
            self.catalogue_scanning = scanned
            if request:
                self.catalogue_requests += 1

    def catalogue_accept(self, count):
        with self.lock:
            self.catalogue_verified += count
            self.catalogue_scanning = 0

    def tile_stage(self, name, stage):
        with self.lock:
            self.active[name] = (stage, time.monotonic())

    def add_bytes(self, count):
        with self.lock:
            self.network_bytes += count

    def finished(self, name, status, elapsed):
        with self.lock:
            self.active.pop(name, None)
            self.counts[status] += 1
            if status == "downloaded":
                self.tile_seconds += elapsed
        if status in ("found_local", "reused", "skipped"):
            LOG.info("FOUND LOCALLY %s | no download needed | local processing=%s", name, duration(elapsed))
        elif status == "downloaded":
            LOG.info("DOWNLOAD COMPLETE %s | elapsed=%s", name, duration(elapsed))
        elif status == "cancelled":
            LOG.warning("CANCELLED %s | elapsed=%s", name, duration(elapsed))
        elif status == "failed":
            LOG.error("FAILED %s | elapsed=%s", name, duration(elapsed))
        else:
            LOG.info("%s %s | elapsed=%s", status.upper(), name, duration(elapsed))

    def heartbeat(self):
        with self.lock:
            if self.heartbeat_paused:
                return
            elapsed = time.monotonic() - self.started
            if self.stage == "catalogue":
                if self.catalogue_expected:
                    LOG.info("catalogue | verified %s/%s (%.2f%%) | scanning %s in %s | requests %s | elapsed %s",
                             self.catalogue_verified, self.catalogue_expected,
                             100 * self.catalogue_verified / max(1, self.catalogue_expected),
                             self.catalogue_scanning, self.catalogue_interval or "range setup",
                             self.catalogue_requests, duration(elapsed))
                else:
                    LOG.info("catalogue | establishing authoritative count/range | elapsed %s", duration(elapsed))
                return
            if self.stage == "local files":
                LOG.info("local files | checked %s/%s | found locally %s | not found locally %s | elapsed %s",
                         self.local_checked, self.total, self.local_found, self.local_missing, duration(elapsed))
                return
            if self.stage == "authentication":
                return
            transfer_elapsed = time.monotonic() - (self.download_started or self.started)
            counts, active = self.counts.copy(), list(self.active.items())
            local_count = sum(counts[k] for k in ("found_local", "reused", "skipped"))
            successful = counts["downloaded"] + local_count
            processed = sum(counts.values())
            timed = counts["downloaded"]
            remaining = max(0, self.total - processed)
            eta = transfer_elapsed / timed * remaining if timed and not STOP.is_set() and not counts["failed"] else None
            avg = self.tile_seconds / counts["downloaded"] if counts["downloaded"] else None
            speed = self.network_bytes / max(transfer_elapsed, 0.001)
            LOG.info("%s | complete %s/%s (%.2f%%) | found locally %s | downloaded %s | failed %s "
                     "cancelled %s | elapsed %s | mean download/tile %s | ETA~ %s | %s/s",
                     self.stage, successful, self.total, 100 * successful / max(1, self.total),
                     local_count, counts["downloaded"], counts["failed"], counts["cancelled"],
                     duration(elapsed), duration(avg), duration(eta), human_bytes(speed))
        for name, (stage, started) in active[:4]:
            LOG.info("  ACTIVE %s | %s | stage elapsed %s", name, stage, duration(time.monotonic() - started))
        if len(active) > 4:
            LOG.info("  ... %s more active tiles", len(active) - 4)


def retry_delay(response, attempt):
    value = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return min(5 * 2 ** (attempt - 1), 120)


class HTTP:
    """Thread-local sessions, closed responses, explicit transient retries."""
    def __init__(self, tokens=None):
        self.tokens = tokens
        self.local = threading.local()
        self.sessions = []
        self.lock = threading.Lock()

    def session(self):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({"User-Agent": f"GLO30-Download/{VERSION}",
                                               "Accept-Encoding": "identity"})
            with self.lock:
                self.sessions.append(self.local.session)
        return self.local.session

    def get(self, url, *, method="GET", auth=False, label="HTTP", attempts=MAX_ATTEMPTS,
            allowed=(), **kwargs):
        for attempt in range(1, attempts + 1):
            check_stop()
            headers = dict(kwargs.pop("headers", {})) if attempt == 1 else headers.copy()
            token = self.tokens.get() if auth else None
            if auth:
                headers["Authorization"] = "Bearer " + token
            started = time.monotonic()
            try:
                response = self.session().request(method, url, headers=headers, timeout=TIMEOUT,
                                                  allow_redirects=True, **kwargs)
                LOG.debug("HTTP %s | %s | %.2fs | attempt %s", response.status_code, label,
                          time.monotonic() - started, attempt)
                if response.status_code == 401 and auth:
                    response.close()
                    headers["Authorization"] = "Bearer " + self.tokens.get(rejected=token)
                    check_stop()
                    response = self.session().request(method, url, headers=headers, timeout=TIMEOUT,
                                                      allow_redirects=True, **kwargs)
                    LOG.debug("HTTP %s | %s | after token refresh", response.status_code, label)
                if response.status_code in allowed or response.status_code < 400:
                    return response
                code = response.status_code
                delay = retry_delay(response, attempt)
                response.close()
                if code not in (408, 429, 500, 502, 503, 504):
                    if auth and code in (401, 403):
                        STOP.set()
                    raise PermanentError(f"HTTP {code}: {label}. " + (
                        "Access denied; check CCM authorization/account policy." if code == 403 else "Not retried."))
                error = f"HTTP {code}: {label}"
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = f"{type(exc).__name__}: {label}"
                delay = retry_delay(None, attempt)
            if attempt == attempts:
                raise RetryableError(error, retry_after=delay)
            LOG.warning("RETRY %s | wait %.1fs", error, delay)
            pause(delay)
        raise RetryableError(label)

    def json(self, url, **kwargs):
        with self.get(url, **kwargs) as response:
            try:
                return response.json()
            except ValueError as exc:
                raise PermanentError("Expected JSON metadata, received a different response") from exc

    def close(self):
        for session in self.sessions:
            session.close()


class Tokens:
    """One refresh operation at a time. Secrets exist only in process memory."""
    def __init__(self):
        self.lock = threading.Lock()
        self.username = self.password = self.access = self.refresh = None
        self.expires = 0.0

    def get(self, rejected=None):
        with self.lock:
            check_stop()
            if self.access and self.access != rejected and time.monotonic() < self.expires:
                return self.access
            if self.username is None:
                env_username = os.environ.get("CDSE_USERNAME")
                env_password = os.environ.get("CDSE_PASSWORD")
                if env_username and env_password:
                    LOG.info("CDSE LOGIN | using credentials from environment variables")
                    self.username, self.password = env_username, env_password
                else:
                    print("\nCDSE login is required only for tiles that are missing locally.", flush=True)
                    self.username = env_username or input("CDSE username/email: ").strip()
                    self.password = env_password or getpass.getpass("CDSE password: ")
            common = {"client_id": "cdse-public"}
            data = {**common, "grant_type": "refresh_token", "refresh_token": self.refresh} if self.refresh else {
                **common, "grant_type": "password", "username": self.username, "password": self.password}
            for fallback in range(2):
                check_stop()
                LOG.info("CDSE LOGIN | %s", "refreshing session" if data["grant_type"] == "refresh_token" else "signing in")
                with requests.post(TOKEN_URL, data=data, timeout=TIMEOUT) as response:
                    if response.status_code in (400, 401) and data["grant_type"] == "refresh_token":
                        data = {**common, "grant_type": "password", "username": self.username, "password": self.password}
                        continue
                    if response.status_code != 200:
                        raise PermanentError(f"CDSE login failed (HTTP {response.status_code}); check credentials/account access. "
                                             "No credentials or tokens have been logged.")
                    payload = response.json()
                self.access = payload["access_token"]
                self.refresh = payload.get("refresh_token")
                self.expires = time.monotonic() + max(1, int(payload.get("expires_in", 600)) - 60)
                LOG.info("CDSE LOGIN | successful")
                return self.access
            raise PermanentError("CDSE authentication did not succeed")


# CATALOGUE: ONE COUNT, ORDERED PAGES, ADAPTIVE DATE PARTITIONS ----------------
def date_value(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def odata_date(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fetch_catalogue(http, progress=None):
    # CDSE documents that $count is comparatively expensive. Use it exactly once
    # as the release-wide completeness target, then enumerate ordinary ordered
    # pages inside bounded time windows.
    first_params = {"$filter": FILTER, "$orderby": "ContentDate/Start asc",
                    "$top": 1, "$count": "true", "$expand": "Attributes"}
    first = http.json(CATALOGUE_URL, params=first_params, label="catalogue authoritative count")
    try:
        expected = int(first["@odata.count"])
        first_item = first["value"][0]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise PermanentError("Catalogue count/range response was incomplete") from exc
    if expected <= 0:
        raise PermanentError("The release query returned no products")
    start = date_value(first_item["ContentDate"]["Start"])
    if progress:
        progress.catalogue_begin(expected)

    last_params = {"$filter": FILTER, "$orderby": "ContentDate/Start desc",
                   "$top": 1, "$expand": "Attributes"}
    last = http.json(CATALOGUE_URL, params=last_params, label="catalogue range end")
    try:
        end = date_value(last["value"][0]["ContentDate"]["Start"]) + timedelta(milliseconds=1)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise PermanentError("Catalogue range-end response was incomplete") from exc
    if end <= start:
        end = start + timedelta(milliseconds=1)

    LOG.info("CATALOGUE advertised count=%s; one count only, then ordered date-window pages", expected)
    LOG.info("CATALOGUE ContentDate range %s to %s", odata_date(start), odata_date(end))
    rows, partitions = [], []

    def interval_label(lo, hi):
        return f"{odata_date(lo)}..{odata_date(hi)}"

    def request_page(lo, hi, skip, top, label, scanned=0):
        extra = f" and ContentDate/Start ge {odata_date(lo)} and ContentDate/Start lt {odata_date(hi)}"
        params = {"$filter": FILTER + extra, "$orderby": "ContentDate/Start asc",
                  "$expand": "Attributes", "$top": top, "$skip": skip}
        if progress:
            progress.catalogue_scan(interval_label(lo, hi), scanned, request=True)
        page = http.json(CATALOGUE_URL, params=params, label=label)
        return page.get("value", [])

    def fetch_interval(lo, hi, depth=0):
        check_stop()
        if hi <= lo:
            return
        label = interval_label(lo, hi)
        local = []
        skip = 0
        while skip < PARTITION_LIMIT:
            check_stop()
            top = min(PAGE_SIZE, PARTITION_LIMIT - skip)
            batch = request_page(lo, hi, skip, top, f"catalogue page offset {skip}", len(local))
            if not batch:
                break
            local.extend(normalize_product(item) for item in batch)
            if progress:
                progress.catalogue_scan(label, len(local))
            if len(batch) < top:
                break
            skip += len(batch)

        # If the safe local paging budget was filled exactly, make one tiny
        # existence probe at $skip=PARTITION_LIMIT. A hit means the interval is
        # too dense, so divide the time range rather than walking toward 10000.
        if len(local) == PARTITION_LIMIT:
            overflow = request_page(lo, hi, PARTITION_LIMIT, 1,
                                    f"catalogue partition-limit probe {PARTITION_LIMIT}", len(local))
            if overflow:
                span_ms = int((hi - lo).total_seconds() * 1000)
                if span_ms < 2 or depth > 40:
                    raise PermanentError(
                        "More products share a millisecond-scale ContentDate interval than the safe paging limit; "
                        "cannot prove complete enumeration without exceeding CDSE paging limits")
                mid = lo + timedelta(milliseconds=span_ms // 2)
                if mid <= lo or mid >= hi:
                    raise PermanentError("Could not subdivide a dense catalogue time interval")
                LOG.info("CATALOGUE splitting dense interval %s after probing >%s records", label, PARTITION_LIMIT)
                if progress:
                    progress.catalogue_scan(label, 0)
                fetch_interval(lo, mid, depth + 1)
                fetch_interval(mid, hi, depth + 1)
                return

        ids = [row["product_id"] for row in local]
        if len(ids) != len(set(ids)):
            raise PermanentError("Catalogue page duplicates detected inside an ordered partition")
        rows.extend(local)
        partitions.append({"start": odata_date(lo), "end_exclusive": odata_date(hi), "count": len(local)})
        if progress:
            progress.catalogue_accept(len(local))
        LOG.info("CATALOGUE accepted %s | %s products | verified %s/%s",
                 label, len(local), len(rows), expected)

    cursor = start
    window = timedelta(days=CATALOGUE_WINDOW_DAYS)
    while cursor < end:
        check_stop()
        next_cursor = min(end, cursor + window)
        fetch_interval(cursor, next_cursor)
        cursor = next_cursor

    distinct = {row["product_id"] for row in rows}
    if len(rows) != expected or len(distinct) != expected:
        raise PermanentError(
            f"Release inventory did not reconcile with the one authoritative count: "
            f"retrieved {len(rows)} rows / {len(distinct)} distinct IDs, expected {expected}. "
            "Rerun --refresh-catalogue; no incomplete inventory was saved.")
    return {"schema": SCHEMA, "dataset": DATASET, "complete": True, "retrieved_utc": now(),
            "catalogue_count": expected, "count_queries": 1, "partitions": partitions, "rows": rows}


def catalogue(settings, http, progress=None):
    path = settings.meta / "catalogue_verified.json"
    saved = load_json(path)
    valid = (isinstance(saved, dict) and saved.get("schema") == SCHEMA and saved.get("dataset") == DATASET
             and saved.get("complete") is True and len(saved.get("rows", [])) == saved.get("catalogue_count")
             and len({r["product_id"] for r in saved.get("rows", [])}) == saved.get("catalogue_count"))
    if settings.audit_only and (not valid or settings.refresh_catalogue):
        raise PermanentError("Offline audit requires a verified catalogue cache; first run --dry-run")
    if not valid or settings.refresh_catalogue:
        LOG.info("Creating a NEW verified inventory; old CSV inventories are not treated as complete")
        saved = fetch_catalogue(http, progress)
        atomic_json(path, saved)
    else:
        LOG.info("Using count-verified catalogue cache from %s", saved["retrieved_utc"])
        if progress:
            progress.catalogue_begin(saved["catalogue_count"])
            progress.catalogue_accept(saved["catalogue_count"])
    unique, decisions = deduplicate(saved["rows"])
    write_csv(settings.meta / "duplicate_product_choices.csv", decisions,
              ["dem_filename", "product_id", "modification_date", "selected", "alternatives"])
    LOG.info("CATALOGUE %s product IDs -> %s unique raster files; %s alternate records",
             saved["catalogue_count"], len(unique), saved["catalogue_count"] - len(unique))
    return unique, saved


# COUNTRY GEOMETRY: FULL ADM0, INCLUDING DISCONNECTED PARTS ---------------------
def polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def unwrap_ring(coordinates):
    result = []
    for coordinate in coordinates:
        x, y = float(coordinate[0]), float(coordinate[1])
        if not (-180.000001 <= x <= 180.000001 and -90.000001 <= y <= 90.000001):
            raise PermanentError("Boundary must use WGS84 longitude/latitude coordinates")
        if result:
            while x - result[-1][0] > 180:
                x -= 360
            while x - result[-1][0] < -180:
                x += 360
        result.append((x, y))
    if result and abs(result[-1][0] - result[0][0]) > 0.000001:
        raise PermanentError("Boundary winds around a pole; supply an antimeridian-cut WGS84 GeoJSON")
    return result


def normalize_geometry(geojson):
    kind = geojson.get("type")
    if kind == "FeatureCollection":
        inputs = [feature["geometry"] for feature in geojson["features"] if feature.get("geometry")]
    elif kind == "Feature":
        inputs = [geojson["geometry"]]
    else:
        inputs = [geojson]
    pieces = []
    world = box(-180, -90, 180, 90)
    for item in inputs:
        for polygon in polygon_parts(shape(item)):
            shell = unwrap_ring(polygon.exterior.coords)
            center = sum(x for x, _ in shell) / len(shell)
            holes = []
            for interior in polygon.interiors:
                ring = unwrap_ring(interior.coords)
                shift = 360 * round((center - sum(x for x, _ in ring) / len(ring)) / 360)
                holes.append([(x + shift, y) for x, y in ring])
            unwrapped = make_valid(Polygon(shell, holes))
            lo, _, hi, _ = unwrapped.bounds
            for k in range(math.floor((-180 - hi) / 360), math.ceil((180 - lo) / 360) + 1):
                clipped = translate(unwrapped, xoff=360 * k).intersection(world)
                pieces.extend(p for p in polygon_parts(clipped) if p.area > 0)
    if not pieces:
        raise PermanentError("Boundary has no usable polygonal area")
    return make_valid(unary_union(pieces))


def boundary(iso3, settings, http):
    directory = settings.meta / "boundaries"
    record = directory / f"{iso3}.json"
    local = settings.boundary_files.get(iso3)
    if local:
        document = load_json(local)
        if document is None:
            raise PermanentError(f"Boundary file not found: {local}")
        meta = {"boundaryISO": iso3, "source": str(local), "license": "user-supplied; verify independently"}
        bundle = {"metadata": meta, "geojson": document, "retrieved_utc": now()}
    elif record.exists() and not settings.refresh_boundaries:
        bundle = load_json(record)
    else:
        if settings.audit_only:
            raise PermanentError(f"Offline boundary cache missing for {iso3}; run --dry-run first")
        url = BOUNDARY_API.format(iso3=iso3)
        meta = http.json(url, label=f"{iso3} boundary metadata")
        if isinstance(meta, list) and len(meta) == 1:
            meta = meta[0]
        if not isinstance(meta, dict) or meta.get("boundaryISO") != iso3 or meta.get("boundaryType") != "ADM0":
            raise PermanentError(f"No matching gbOpen ADM0 geometry for {iso3}; set BOUNDARY_FILES")
        download = meta["gjDownloadURL"]
        # The media endpoint resolves Git LFS-backed GeoJSONs in the source repository.
        match = re.match(r"https://github.com/([^/]+)/([^/]+)/raw/(.+)", download)
        if match:
            download = f"https://media.githubusercontent.com/media/{match[1]}/{match[2]}/{match[3]}"
        if urlsplit(download).scheme != "https":
            raise PermanentError("Refusing a non-HTTPS boundary download URL")
        document = http.json(download, label=f"{iso3} full boundary GeoJSON")
        bundle = {"metadata": meta, "geojson": document, "retrieved_utc": now(), "download_url": download}
    geometry = normalize_geometry(bundle["geojson"])
    bundle["geometry_sha256"] = hashlib.sha256(json.dumps(bundle["geojson"], sort_keys=True).encode()).hexdigest()
    if not settings.audit_only:
        atomic_json(record, bundle)
    return geometry, {k: v for k, v in bundle.items() if k != "geojson"}


def target_name(iso3=None):
    """Return a collision-safe output folder name for the selected product profile."""
    if iso3 is None:
        return GLOBAL_FOLDER
    return iso3 if PRODUCT_FORMAT == "DGED" else f"{iso3}_DTED"


def make_plan(rows, settings, http):
    selected, provenance = {}, {}
    if settings.download_all:
        selected[target_name()] = rows
    if settings.countries:
        cells = [grid_info(r["grid_id"])[1] for r in rows]
        tree = STRtree(cells)
        for iso3 in settings.countries:
            geometry, metadata = boundary(iso3, settings, http)
            indices = set()
            for piece in polygon_parts(geometry):
                indices.update(int(i) for i in tree.query(piece, predicate="intersects"))
            target = target_name(iso3)
            selected[target] = [rows[i] for i in sorted(indices)]
            provenance[iso3] = metadata
            if not selected[target]:
                raise PermanentError(f"No DEM tiles intersect {iso3}; inspect boundary and catalogue, not a successful download")
            LOG.info("SELECTION %s: %s whole intersecting DEM tiles", iso3, len(indices))
    jobs = {}
    for target, products in selected.items():
        (settings.base / target).mkdir(parents=True, exist_ok=True)
        write_csv(settings.meta / "manifests" / f"{target}.csv", products,
                  ["dem_filename", "grid_id", "product_id", "product_name", "dataset"])
        for row in products:
            job = jobs.setdefault(row["dem_filename"], {"product": row, "targets": []})
            job["targets"].append(settings.base / target)
    atomic_json(settings.meta / "selection.json", {"dataset": DATASET, "timestamp": now(),
                "country_boundaries": provenance, "targets": {k: len(v) for k, v in selected.items()},
                "unique_jobs": len(jobs), "rule": "whole 1-degree grid cells intersecting ADM0, including edge touches"})
    return selected, [jobs[name] for name in sorted(jobs)]


# FILE VERIFICATION AND DOWNLOAD ---------------------------------------------
def dem_url(product):
    nodes = (product["product_name"], product["tile_name"], "DEM", product["dem_filename"])
    return (f"{DOWNLOAD_BASE}/Products({product['product_id']})"
            + "".join(f"/Nodes({quote(node, safe='._-')})" for node in nodes) + "/$value")


def receipt_path(settings, name):
    return settings.meta / "receipts" / (name + ".json")


def file_state(path):
    stat = path.stat()
    return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def valid_file(path, product, receipt, verify_hashes=False):
    if not path.is_file() or not raster_header_ok(path):
        return False
    if not receipt or receipt.get("product_id") != product["product_id"] or receipt.get("dataset") != DATASET:
        return False
    if path.stat().st_size != receipt.get("size_bytes"):
        return False
    if receipt.get("files", {}).get(str(path.resolve())) != file_state(path):
        return False
    return not verify_hashes or sha256_file(path) == receipt.get("sha256")


def remote_info(http, product):
    url, name = dem_url(product), product["dem_filename"]
    # CDSE's node download endpoint commonly rejects HEAD. A one-byte ranged GET
    # obtains the authoritative object length without producing a noisy 405 first.
    with http.get(url, auth=True, stream=True, headers={"Range": "bytes=0-0"}, label=f"remote info {name}") as response:
        content_range = re.fullmatch(r"bytes \d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
        if response.status_code == 206 and not content_range:
            raise PermanentError(f"Size probe lacks a valid Content-Range for {name}")
        size = int(content_range[1]) if content_range else int(response.headers.get("Content-Length", 0))
        if size <= 0:
            raise PermanentError(f"Server did not supply a usable byte count for {name}")
        return {"size_bytes": size, "etag": response.headers.get("ETag", ""),
                "last_modified": response.headers.get("Last-Modified", "")}


def response_layout(response, current, total):
    encoding = response.headers.get("Content-Encoding", "identity").lower()
    if encoding not in ("", "identity"):
        raise PermanentError("Encoded raster response cannot be safely byte-range resumed")
    if response.status_code == 206:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
        if not match or (int(match[1]), int(match[3])) != (current, total):
            raise PermanentError("Server returned an inconsistent Content-Range")
        return "ab" if current else "wb", current
    if response.status_code == 200:
        length = response.headers.get("Content-Length")
        if length and int(length) != total:
            raise PermanentError("Source size changed; refusing to merge different object versions")
        return "wb", 0  # Server ignored Range: replace the partial, never append.
    raise PermanentError(f"Unexpected transfer status {response.status_code}")


def download_file(http, product, settings, info, progress):
    name = product["dem_filename"]
    part = settings.meta / "partials" / (name + ".part")
    tag = part.with_name(part.name + ".json")
    part.parent.mkdir(parents=True, exist_ok=True)
    identity = {"product_id": product["product_id"], "dataset": DATASET, **info}
    if part.exists() and load_json(tag) != identity:
        LOG.warning("Discarding mismatched partial for %s", name)
        part.unlink()
    atomic_json(tag, identity)
    total = info["size_bytes"]
    if total <= 0:
        raise PermanentError(f"Invalid remote file size for {name}")
    LOG.info("DOWNLOADING %s | %s", name, human_bytes(total))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        check_stop()
        current = part.stat().st_size if part.exists() else 0
        if current > total or (current == total and not raster_header_ok(part)):
            part.unlink()
            current = 0
        if current == total:
            return part
        if shutil.disk_usage(settings.base).free < total - current + 16 * CHUNK_SIZE:
            STOP.set()
            raise PermanentError("Insufficient free disk space for the next tile")
        headers = {"Range": f"bytes={current}-"} if current else {}
        if current and info.get("etag") and not info["etag"].startswith("W/"):
            headers["If-Range"] = info["etag"]
        progress.tile_stage(name, f"request {attempt}; resume {human_bytes(current)}")
        try:
            with http.get(dem_url(product), auth=True, stream=True, headers=headers, attempts=1,
                          allowed=(416,), label=f"download {name}") as response:
                if response.status_code == 416:
                    # A complete validated partial was handled above. Restart once on a stale range.
                    if part.exists():
                        part.unlink()
                    raise RetryableError("Range rejected; restarting this tile from byte zero")
                mode, current = response_layout(response, current, total)
                progress.tile_stage(name, f"receiving {human_bytes(total)}")
                with part.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        check_stop()
                        if chunk:
                            handle.write(chunk)
                            current += len(chunk)
                            progress.add_bytes(len(chunk))
                            if current > total:
                                raise PermanentError("Received more bytes than expected")
                    handle.flush()
                    os.fsync(handle.fileno())
            if current != total:
                raise RetryableError(f"Short transfer: {current} bytes; expected {total}")
            if not raster_header_ok(part):
                raise PermanentError("Response does not match the selected raster format; refusing to publish it")
            return part
        except (RetryableError, requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise RetryableError(f"Transfer failed for {name}: {type(exc).__name__}") from exc
            delay = getattr(exc, "retry_after", None)
            if delay is None:
                delay = min(5 * 2 ** (attempt - 1), 120)
            progress.tile_stage(name, f"retry wait {delay}s")
            LOG.warning("TRANSFER RETRY %s: %s; wait %ss", name, type(exc).__name__, delay)
            pause(delay)
    raise RetryableError(name)


def place_file(source, destination, settings):
    check_stop()
    if source.resolve() == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".placing")
    if temp.exists():
        temp.unlink()
    linked = False
    if settings.copy_mode == "hardlink":
        try:
            os.link(source, temp)
            linked = True
        except OSError:
            LOG.info("Hardlink unavailable; copying %s", destination.name)
    if not linked:
        if shutil.disk_usage(destination.parent).free < source.stat().st_size + 16 * CHUNK_SIZE:
            STOP.set()
            raise PermanentError("Insufficient free disk space to place a tile copy")
        with source.open("rb") as src, temp.open("wb") as dst:
            while chunk := src.read(4 * CHUNK_SIZE):
                check_stop()
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
    check_stop()
    if destination.exists():
        quarantine = settings.meta / "quarantine" / f"{time.time_ns()}_{destination.parent.name}_{destination.name}"
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(destination, quarantine)
    os.replace(temp, destination)


def reuse_folders(settings):
    folders = [settings.base / GLOBAL_FOLDER]
    country_pattern = r"[A-Z]{3}" if PRODUCT_FORMAT == "DGED" else r"[A-Z]{3}_DTED"
    folders += [p for p in settings.base.iterdir() if p.is_dir() and re.fullmatch(country_pattern, p.name)]
    folders += settings.reuse_dirs
    return list(dict.fromkeys(p.resolve() for p in folders if p.is_dir()))


def local_source_ok(path, product, receipt, verify_hashes=False):
    """Accept a local source without contacting CDSE.

    The filename is already tied to an exact product ID by the verified catalogue.
    A conflicting receipt is therefore rejected, while an unreceipted legacy raster can
    be adopted from a folder the user has explicitly designated as the same product and
    release. The selected DGED/DTED file signature is always checked locally.
    """
    if not path.is_file() or not raster_header_ok(path):
        return False
    if receipt:
        if receipt.get("product_id") != product["product_id"] or receipt.get("dataset") != DATASET:
            return False
        recorded_size = receipt.get("size_bytes")
        if recorded_size is not None and path.stat().st_size != recorded_size:
            return False
        if verify_hashes and receipt.get("sha256") and sha256_file(path) != receipt["sha256"]:
            return False
    return True


def scan_local_jobs(jobs, settings, progress):
    """Index relevant local folders once, then split jobs into local and missing."""
    progress.local_scan_begin(len(jobs))
    required = {job["product"]["dem_filename"].casefold() for job in jobs}
    folders = []
    for job in jobs:
        folders.extend(job["targets"])
    folders.extend(reuse_folders(settings))
    folders = list(dict.fromkeys(Path(folder).resolve() for folder in folders if Path(folder).is_dir()))
    index = defaultdict(list)
    for folder in folders:
        check_stop()
        try:
            entries = folder.iterdir()
            for path in entries:
                if path.is_file() and path.name.casefold() in required and path.suffix.lower() == RASTER_EXTENSION:
                    index[path.name.casefold()].append(path)
        except OSError as exc:
            LOG.warning("LOCAL SCAN | could not read %s: %s", folder, exc)

    local, missing = [], []
    for job in jobs:
        check_stop()
        product = job["product"]
        name = product["dem_filename"]
        receipt = load_json(receipt_path(settings, name))
        candidates = index.get(name.casefold(), [])
        source = next((p for p in candidates if local_source_ok(p, product, receipt, settings.verify_hashes)), None)
        if source is None:
            progress.local_scan_update(missing=True)
            missing.append(job)
        else:
            progress.local_scan_update(found=True)
            local.append((job, source, receipt))
    LOG.info("LOCAL CHECK COMPLETE | required %s | found locally %s | not found locally %s",
             len(jobs), len(local), len(missing))
    if missing:
        LOG.info("DOWNLOAD PLAN | only the %s missing %s will contact the protected CDSE download service",
                 len(missing), "tile" if len(missing) == 1 else "tiles")
    else:
        LOG.info("DOWNLOAD PLAN | every required tile was found locally; CDSE login is not needed")
    return local, missing


def adopt_local_job(job, source, receipt, settings, progress):
    """Place an already-local raster into required targets and create/update its receipt."""
    product, targets = job["product"], job["targets"]
    name = product["dem_filename"]
    started = time.monotonic()
    status, error = "found_local", ""
    try:
        check_stop()
        destinations = [folder / name for folder in targets]
        progress.tile_stage(name, f"found locally in {source.parent.name}")
        if not receipt or receipt.get("product_id") != product["product_id"] or receipt.get("dataset") != DATASET:
            receipt = {
                "product_id": product["product_id"],
                "dataset": DATASET,
                "size_bytes": source.stat().st_size,
                "etag": "",
                "last_modified": "",
                "recorded_utc": now(),
                "verification": "local-catalogue-filename-and-raster-header; remote object not queried",
            }
            if settings.verify_hashes:
                progress.tile_stage(name, "hashing local file")
                receipt["sha256"] = sha256_file(source)
        else:
            receipt = dict(receipt)
            receipt.setdefault("size_bytes", source.stat().st_size)
            receipt.setdefault("recorded_utc", now())
            receipt.setdefault("verification", "local-catalogue-filename-and-raster-header; remote object not queried")
            if settings.verify_hashes and not receipt.get("sha256"):
                progress.tile_stage(name, "hashing local file")
                receipt["sha256"] = sha256_file(source)
        for destination in destinations:
            check_stop()
            if destination.resolve() != source.resolve():
                current_ok = destination.is_file() and local_source_ok(destination, product, receipt, settings.verify_hashes)
                if not current_ok:
                    progress.tile_stage(name, f"copying local file to {destination.parent.name}")
                    place_file(source, destination, settings)
        states = dict(receipt.get("files", {}))
        states[str(source.resolve())] = file_state(source)
        for destination in destinations:
            if destination.is_file():
                states[str(destination.resolve())] = file_state(destination)
        receipt["files"] = states
        atomic_json(receipt_path(settings, name), receipt)
        if not all(valid_file(destination, product, receipt, settings.verify_hashes) for destination in destinations):
            raise PermanentError("Local file placement did not pass the final local verification")
    except Cancelled:
        status, error = "cancelled", "Stop requested"
    except Exception as exc:
        status = "failed"
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            STOP.set()
        error = str(exc) if isinstance(exc, (PermanentError, RetryableError)) else f"{type(exc).__name__}: {str(exc)[:250]}"
        LOG.error("LOCAL FILE ERROR %s | %s", name, error)
    elapsed = time.monotonic() - started
    progress.finished(name, status, elapsed)
    return {"dem_filename": name, "product_id": product["product_id"], "status": status,
            "targets": ";".join(p.name for p in targets), "elapsed_seconds": round(elapsed, 3), "error": error}


def process_local_jobs(local_jobs, settings, progress):
    results = []
    for job, source, receipt in local_jobs:
        check_stop()
        results.append(adopt_local_job(job, source, receipt, settings, progress))
        if results[-1]["status"] in ("failed", "cancelled") and STOP.is_set():
            break
    return results


def run_job(job, settings, http, progress, reuse):
    product, targets = job["product"], job["targets"]
    name = product["dem_filename"]
    started = time.monotonic()
    status, error = "failed", ""
    try:
        check_stop()
        progress.tile_stage(name, "checking local copies")
        receipt = load_json(receipt_path(settings, name))
        destinations = [folder / name for folder in targets]
        candidates = list(dict.fromkeys(destinations + [folder / name for folder in reuse]))
        good = [p for p in candidates if valid_file(p, product, receipt, settings.verify_hashes)]
        if all(p in good for p in destinations):
            status = "skipped"
        else:
            source = good[0] if good else None
            source_is_partial = False
            if source is None:
                LOG.info("NOT FOUND LOCALLY %s | download required", name)
                progress.tile_stage(name, "checking remote file information")
                info = remote_info(http, product)
                # Existing files need a remote-size check, not merely non-empty-file detection.
                # Adoption establishes a LOCAL checksum baseline, not an official source checksum.
                source = next((p for p in candidates if p.is_file() and raster_header_ok(p)
                               and (not receipt or receipt.get("product_id") == product["product_id"])
                               and p.stat().st_size == info["size_bytes"]
                               and (not receipt or sha256_file(p) == receipt.get("sha256"))), None)
                status = "reused" if source else "downloaded"
                if source is None:
                    source = download_file(http, product, settings, info, progress)
                    source_is_partial = True
                progress.tile_stage(name, "recording local SHA-256")
                receipt = {**info, "product_id": product["product_id"], "dataset": DATASET,
                           "sha256": sha256_file(source), "recorded_utc": now(),
                           "verification": "http-size-and-raster-header" if source_is_partial else "adopted-size-and-raster-header"}
            else:
                status = "reused"
            for destination in destinations:
                check_stop()
                if destination not in good and destination.resolve() != source.resolve():
                    progress.tile_stage(name, f"placing in {destination.parent.name}")
                    place_file(source, destination, settings)
            states = dict(receipt.get("files", {}))
            for path in good + destinations + ([] if source_is_partial else [source]):
                states[str(path.resolve())] = file_state(path)
            receipt["files"] = states
            atomic_json(receipt_path(settings, name), receipt)
            if source_is_partial:
                source.unlink(missing_ok=True)
                source.with_name(source.name + ".json").unlink(missing_ok=True)
        for destination in destinations:
            if valid_file(destination, product, receipt, False):
                destination.with_name(destination.name + ".placing").unlink(missing_ok=True)
    except Cancelled:
        status, error = "cancelled", "Stop requested; partial file retained"
    except Exception as exc:
        status = "failed"
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            STOP.set()
        error = str(exc) if isinstance(exc, (PermanentError, RetryableError)) else f"{type(exc).__name__}: {str(exc)[:250]}"
        LOG.error("FAILED %s | %s", name, error)
    elapsed = time.monotonic() - started
    progress.finished(name, status, elapsed)
    return {"dem_filename": name, "product_id": product["product_id"], "status": status,
            "targets": ";".join(p.name for p in targets), "elapsed_seconds": round(elapsed, 3), "error": error}


def run_downloads(jobs, settings, http, progress, run_dir, initial_results=None):
    reuse = reuse_folders(settings)
    results, pending, index = list(initial_results or []), {}, 0
    limited = jobs
    progress.download_started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="glo30")
    path = run_dir / "tile_results.csv"
    try:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["dem_filename", "product_id", "status", "targets", "elapsed_seconds", "error"])
            writer.writeheader()
            for result in results:
                writer.writerow(result)
            handle.flush()
            while pending or (index < len(limited) and not STOP.is_set()):
                while not STOP.is_set() and len(pending) < settings.workers and index < len(limited):
                    job = limited[index]
                    pending[pool.submit(run_job, job, settings, http, progress, reuse)] = job
                    index += 1
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    result = future.result()
                    results.append(result)
                    writer.writerow(result)
                    handle.flush()
    finally:
        # No context-manager trap: the signal handler sets STOP before shutdown waits.
        pool.shutdown(wait=True, cancel_futures=True)
    return results


# AUDIT: PRESENCE IS NOT THE SAME AS PIXEL OR SOURCE-IDENTITY VERIFICATION --------
def deep_raster_check(path):
    rasterio = importlib.import_module("rasterio")
    with rasterio.open(path) as dataset:
        if dataset.count != 1 or dataset.width <= 0 or dataset.height <= 0 or dataset.crs is None:
            raise ValueError("Invalid elevation raster metadata")
        for _, window in dataset.block_windows(1):
            check_stop()
            dataset.read(1, window=window)


def audit(selected, settings, run_dir):
    summaries = {}
    for target, products in selected.items():
        check_stop()
        folder = settings.base / target
        expected = {p["dem_filename"].casefold(): p for p in products}
        actual = {p.name.casefold(): p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() == RASTER_EXTENSION}
        missing, extra = sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))
        details, counts = [], Counter()
        for key, product in expected.items():
            check_stop()
            path = actual.get(key)
            status, error = "OK", ""
            receipt = load_json(receipt_path(settings, product["dem_filename"]))
            if path is None:
                status = "MISSING"
            elif not raster_header_ok(path):
                status = "INVALID_RASTER_HEADER"
            elif not receipt or receipt.get("product_id") != product["product_id"] or receipt.get("dataset") != DATASET:
                status = "UNVERIFIED"
            elif path.stat().st_size != receipt["size_bytes"]:
                status = "SIZE_MISMATCH"
            elif receipt.get("files", {}).get(str(path.resolve())) != file_state(path):
                status = "UNVERIFIED_OR_CHANGED"
            elif settings.verify_hashes and sha256_file(path) != receipt.get("sha256"):
                status = "HASH_MISMATCH"
            if status == "OK" and settings.deep_audit:
                try:
                    deep_raster_check(path)
                except Cancelled:
                    raise
                except Exception as exc:
                    status, error = "RASTER_READ_ERROR", str(exc)[:250]
            counts[status] += 1
            details.append({"filename": product["dem_filename"], "status": status,
                            "bytes": path.stat().st_size if path else 0, "error": error})
        details += [{"filename": actual[key].name, "status": "UNEXPECTED", "bytes": actual[key].stat().st_size} for key in extra]
        placing = [p.name for p in folder.glob("*.placing")]
        passed = counts["OK"] == len(expected) and not extra and not placing
        summaries[target] = {"expected_unique": len(expected), "actual_rasters": len(actual),
                             "missing": len(missing), "unexpected": len(extra), "checks": dict(counts),
                             "placement_fragments": placing, "filename_match": not missing and not extra,
                             "passed": passed, "hashes_checked": settings.verify_hashes,
                             "all_raster_blocks_read": settings.deep_audit}
        write_csv(run_dir / f"{target}_audit.csv", details, ["filename", "status", "bytes", "error"])
        LOG.info("AUDIT %s | expected=%s actual=%s missing=%s extra=%s verified=%s | %s",
                 target, len(expected), len(actual), len(missing), len(extra), counts["OK"], "PASS" if passed else "NOT COMPLETE/VERIFIED")
    atomic_json(run_dir / "audit_summary.json", {"dataset": DATASET, "time": now(), "targets": summaries,
                "test_limited": settings.max_products is not None, "dry_run": settings.dry_run,
                "scope": "current verified catalogue snapshot and recorded country boundaries"})
    return all(summary["passed"] for summary in summaries.values())


# COMMAND LINE, PROCESS LOCK, AND ENTRY POINT ----------------------------------
def configuration(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="Optional TOML configuration")
    parser.add_argument("--root", type=Path, help="Parent directory of GLO_30")
    parser.add_argument("--download-all", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--iso3", nargs="+", help="Overrides the enabled-country list; e.g. THA NER")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--copy-mode", choices=("copy", "hardlink"))
    parser.add_argument("--reuse-dir", action="append", type=Path, default=[])
    parser.add_argument("--verify-hashes", action="store_true", default=None)
    parser.add_argument("--deep-audit", action="store_true", default=None)
    parser.add_argument("--max-products", type=int, help="TEST ONLY: limit unique jobs, not the full audit expectation")
    parser.add_argument("--refresh-catalogue", "--refresh", action="store_true")
    parser.add_argument("--refresh-boundaries", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Build manifests and audit local files; no DEM downloads")
    mode.add_argument("--audit-only", action="store_true", help="Offline audit; requires cached catalogue and boundaries")
    parser.add_argument("--list-countries", action="store_true", help="Print recognized ISO3 codes; no network requests")
    args = parser.parse_args(argv)
    if args.list_countries:
        for country in sorted(pycountry.countries, key=lambda c: c.alpha_3):
            print(country.alpha_3, country.name)
        parser.exit()
    data = {}
    if args.config:
        with args.config.open("rb") as handle:
            data = tomllib.load(handle)
    allowed = {"root_dir", "download_all", "countries", "workers", "copy_mode", "existing_raster_dirs",
               "existing_tif_dirs", "boundary_files", "verify_hashes", "deep_audit"}
    if set(data) - allowed:
        parser.error("Unknown config keys: " + ", ".join(sorted(set(data) - allowed)))
    flags = data.get("countries", COUNTRIES)
    if not isinstance(flags, dict) or any(type(v) is not bool for v in flags.values()):
        parser.error("countries must be an ISO3-to-boolean mapping")
    countries = args.iso3 if args.iso3 is not None else [k for k, v in flags.items() if v]
    countries = sorted({part.strip().upper() for code in countries for part in code.split(",") if part.strip()})
    unknown = [code for code in countries if pycountry.countries.get(alpha_3=code) is None]
    if unknown:
        parser.error("Unrecognized ISO3 codes: " + ", ".join(unknown))
    option = lambda name, default: getattr(args, name) if getattr(args, name) is not None else data.get(name, default)
    root = (args.root or Path(data.get("root_dir", ROOT_DIR))).expanduser()
    if os.name != "nt" and re.match(r"^[A-Za-z]:", str(root)):
        parser.error("On this operating system specify a native --root path")
    settings = Settings(root=root.resolve(), download_all=option("download_all", DOWNLOAD_ALL), countries=countries,
                        workers=option("workers", WORKERS), copy_mode=option("copy_mode", COPY_MODE),
                        reuse_dirs=[Path(p).expanduser().resolve() for p in
                                    data.get("existing_raster_dirs", data.get("existing_tif_dirs", EXISTING_RASTER_DIRS))] + args.reuse_dir,
                        boundary_files={k.upper(): Path(v).expanduser().resolve() for k, v in data.get("boundary_files", BOUNDARY_FILES).items()},
                        verify_hashes=option("verify_hashes", VERIFY_HASHES), deep_audit=option("deep_audit", DEEP_AUDIT),
                        dry_run=args.dry_run, audit_only=args.audit_only, refresh_catalogue=args.refresh_catalogue,
                        refresh_boundaries=args.refresh_boundaries, max_products=args.max_products)
    if type(settings.download_all) is not bool or type(settings.verify_hashes) is not bool or type(settings.deep_audit) is not bool:
        parser.error("download_all, verify_hashes, and deep_audit must be booleans")
    if type(settings.workers) is not int or settings.workers < 1:
        parser.error("workers must be a positive integer")
    if settings.copy_mode not in ("copy", "hardlink"):
        parser.error("copy_mode must be copy or hardlink")
    if settings.max_products is not None and settings.max_products < 1:
        parser.error("max-products must be positive")
    if not settings.download_all and not settings.countries:
        parser.error("Nothing selected. Enable ISO3 flags, use --iso3 THA NER, or explicitly use --download-all")
    if settings.deep_audit:
        try:
            importlib.import_module("rasterio")
        except ImportError:
            parser.error("Deep audit requires: python -m pip install -r requirements-validation.txt")
    return settings


@contextmanager
def run_lock(base):
    path = base / "_metadata" / ".run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PermanentError("Another downloader holds this output-root lock; stop it before starting a second run") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    settings = configuration(argv)
    STOP.clear()
    run_dir = settings.base / "logs" / (datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}")
    run_dir.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    file_handler = logging.FileHandler(run_dir / "download.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    for handler in (console_handler, file_handler):
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        LOG.addHandler(handler)
    interrupted = threading.Event()
    signal_count = 0

    def handle_interrupt(signum, frame):
        nonlocal signal_count
        signal_count += 1
        if signal_count > 1:
            os.write(2, b"\nSecond interrupt: forcing exit; unfinished partials remain for next run.\n")
            os._exit(130)
        interrupted.set()
        STOP.set()
        os.write(2, b"\nStop requested. Retry waits wake now; active I/O must unwind. Press Ctrl+C again to force exit.\n")

    old_handler = signal.signal(signal.SIGINT, handle_interrupt)
    progress, heartbeat_stop, http = Progress(), threading.Event(), HTTP(Tokens())
    def heartbeat():
        while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
            progress.heartbeat()
    thread = threading.Thread(target=heartbeat, daemon=True, name="glo30-heartbeat")
    thread.start()
    outcome, code = "failed", 1
    try:
        with run_lock(settings.base):
            LOG.info("GLO-30 Download %s | %s | %s", VERSION, DATASET, PRODUCT_SPEC["description"])
            LOG.info("OUTPUT %s | PID %s | workers %s", settings.base, os.getpid(), settings.workers)
            progress.stage_set("catalogue", "catalogue: building/reading the verified 2024_1 inventory")
            products, snapshot = catalogue(settings, http, progress)
            progress.stage_set("country selection and manifests", "selection: deciding which tiles are required")
            selected, jobs = make_plan(products, settings, http)
            LOG.info("PLAN %s unique jobs across %s output folders", len(jobs), len(selected))
            if settings.max_products:
                LOG.warning("TEST LIMIT %s: audit still compares the FULL selected manifests", settings.max_products)
            if not settings.dry_run and not settings.audit_only:
                limited_jobs = jobs[:settings.max_products] if settings.max_products else jobs
                progress.stage_set("local files", "local files: checking what is already on this computer")
                local_jobs, missing_jobs = scan_local_jobs(limited_jobs, settings, progress)
                local_results = process_local_jobs(local_jobs, settings, progress)
                if STOP.is_set():
                    raise Cancelled("User stop or local file error; consult tile_results.csv")
                if missing_jobs:
                    progress.stage_set("authentication", "login: CDSE credentials are needed for missing tiles")
                    progress.pause_heartbeat(True)
                    try:
                        LOG.info("LOGIN REQUIRED | %s %s %s not found locally", len(missing_jobs),
                                 "tile" if len(missing_jobs) == 1 else "tiles",
                                 "was" if len(missing_jobs) == 1 else "were")
                        http.tokens.get()  # Prompt on the main thread, never in a download worker.
                    finally:
                        progress.pause_heartbeat(False)
                    progress.stage_set("download", "download: retrieving only tiles that were not found locally")
                    results = run_downloads(missing_jobs, settings, http, progress, run_dir, local_results)
                else:
                    results = run_downloads([], settings, http, progress, run_dir, local_results)
                if STOP.is_set():
                    raise Cancelled("User stop or fatal access denial; consult tile_results.csv")
            else:
                results = []
            progress.stage_set("audit", "audit: checking final completeness")
            passed = audit(selected, settings, run_dir)
            if settings.dry_run:
                outcome, code = "planned_no_dem_download", 0
            elif settings.max_products and not settings.audit_only:
                code = 1 if any(r["status"] == "failed" for r in results) else 0
                outcome = "test_only_not_a_complete_download"
            else:
                outcome, code = ("complete", 0) if passed else ("audit_incomplete_or_unverified", 1)
            LOG.info("FINISH %s | report folder %s", outcome, run_dir)
    except Cancelled:
        outcome, code = ("interrupted", 130) if interrupted.is_set() else ("stopped_after_fatal_error", 1)
        LOG.warning("%s; partials retained. No complete-download claim is made.", outcome)
    except Exception as exc:
        LOG.error("STOPPED %s: %s", type(exc).__name__, str(exc)[:400])
    finally:
        heartbeat_stop.set()
        thread.join(timeout=2)
        http.close()
        atomic_json(run_dir / "run_info.json", {"script_version": VERSION, "dataset": DATASET,
                    "product_format": PRODUCT_FORMAT, "status": outcome,
                    "exit_code": code, "finished_utc": now(), "root": str(settings.root),
                    "download_all": settings.download_all, "countries": settings.countries,
                    "workers": settings.workers, "test_limit": settings.max_products,
                    "elapsed_seconds": round(time.monotonic() - progress.started, 3)})
        signal.signal(signal.SIGINT, old_handler)
        for handler in list(LOG.handlers):
            handler.close()
            LOG.removeHandler(handler)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
