"""Offline tests. No CDSE credentials, network, or real DEM downloads are used."""
import contextlib
import hashlib
import io
import json
import os
import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import GLO30_Download as d


def product(number=1, grid="N00_E000"):
    tile, _ = d.grid_info(grid)
    return {"product_id": f"00000000-0000-0000-0000-{number:012d}", "product_name": f"DEMO_{number}.DEM",
            "grid_id": grid, "tile_name": tile, "dem_filename": d.dem_filename_for(tile), "dataset": d.DATASET,
            "publication_date": "2024-01-01T00:00:00Z", "modification_date": "2024-01-01T00:00:00Z", "online": True}


def raw_product(number, day):
    return {"Id": product(number)["product_id"], "Name": f"DEMO_{number}.DEM",
            "ContentDate": {"Start": f"2011-01-{day:02d}T00:00:00.000Z"},
            "Attributes": [{"Name": "dataset", "Value": d.DATASET},
                           {"Name": "gridId", "Value": f"N00_E{number:03d}"}]}


class Response:
    def __init__(self, status=200, headers=None, content=b"", payload=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = content
        self.payload = payload
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.content


class CatalogueHTTP:
    def __init__(self, items, duplicate=False):
        self.items = items
        self.calls = []
        self.duplicate = duplicate

    def json(self, url, params, label):
        self.calls.append(params.copy())
        items = self.items
        match = re.search(r"ContentDate/Start ge (\S+) and ContentDate/Start lt (\S+)", params["$filter"])
        if match:
            lo, hi = map(d.date_value, match.groups())
            items = [p for p in items if lo <= d.date_value(p["ContentDate"]["Start"]) < hi]
        items = sorted(items, key=lambda p: (p["ContentDate"]["Start"], p["Id"]),
                       reverse=params["$orderby"].endswith("desc"))
        skip = params.get("$skip", 0)
        value = items[skip:skip + params["$top"]]
        if self.duplicate and skip and value:
            value = [items[0]] * len(value)
        return {"@odata.count": len(items), "value": value}


class Tests(unittest.TestCase):
    def setUp(self):
        d.STOP.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.s = d.Settings(self.root, download_all=True)
        self.s.base.mkdir()

    def tearDown(self):
        d.STOP.clear()
        self.temp.cleanup()

    def test_config_no_selection_is_safe(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            d.configuration(["--root", str(self.root)])
        self.assertEqual(result.exception.code, 2)

    def test_config_many_iso3_and_case_dedup(self):
        settings = d.configuration(["--root", str(self.root), "--iso3", "tha,ner", "THA", "--workers", "16"])
        self.assertEqual(settings.countries, ["NER", "THA"])
        self.assertFalse(settings.download_all)
        self.assertEqual(settings.workers, 16)

    def test_toml_flags(self):
        path = self.root / "config.toml"
        path.write_text('download_all = false\n[countries]\nTHA = true\nNER = false\n')
        settings = d.configuration(["--root", str(self.root), "--config", str(path)])
        self.assertEqual(settings.countries, ["THA"])

    def test_unknown_iso3_fails(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            d.configuration(["--root", str(self.root), "--iso3", "XYZ"])

    def test_grid_south_west(self):
        tile, geom = d.grid_info("S01_W002")
        self.assertEqual(geom.bounds, (-2.0, -1.0, -1.0, 0.0))
        self.assertEqual(tile, "Copernicus_DSM_10_S01_00_W002_00")

    def test_invalid_grid(self):
        for grid in ("ABC", "N91_E001", "N01_E999", "N90_E001"):
            with self.assertRaises(d.PermanentError):
                d.grid_info(grid)

    def test_dedup_repeated_id_and_filename(self):
        a, b = product(1), product(2)
        b["modification_date"] = "2025-01-01T00:00:00Z"
        rows, decisions = d.deduplicate([a, a.copy(), b])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_id"], b["product_id"])
        self.assertEqual(len(decisions), 2)

    def test_conflicting_id_rejected(self):
        a = product()
        b = {**a, "grid_id": "N00_E001"}
        with self.assertRaises(d.PermanentError):
            d.deduplicate([a, b])

    def test_missing_grid_is_not_silently_omitted(self):
        raw = raw_product(1, 1)
        raw["Attributes"] = raw["Attributes"][:1]
        with self.assertRaises(d.PermanentError):
            d.normalize_product(raw)

    def test_partitioned_enumeration_beyond_one_page(self):
        http = CatalogueHTTP([raw_product(i, i) for i in range(1, 8)])
        with patch.object(d, "PARTITION_LIMIT", 3), patch.object(d, "PAGE_SIZE", 2):
            value = d.fetch_catalogue(http)
        self.assertEqual(len(value["rows"]), 7)
        self.assertEqual(value["catalogue_count"], 7)
        self.assertGreater(len(value["partitions"]), 1)
        self.assertTrue(all(p.get("$skip", 0) <= 3 for p in http.calls))
        self.assertEqual(sum(1 for p in http.calls if p.get("$count") == "true"), 1)
        self.assertTrue(all("$orderby" in p for p in http.calls))

    def test_catalogue_uses_only_one_count_query(self):
        http = CatalogueHTTP([raw_product(i, i) for i in range(1, 5)])
        d.fetch_catalogue(http)
        count_calls = [p for p in http.calls if p.get("$count") == "true"]
        self.assertEqual(len(count_calls), 1)
        self.assertFalse(any("$count" in p for p in http.calls[1:]))

    def test_pagination_duplicates_rejected(self):
        http = CatalogueHTTP([raw_product(i, i) for i in range(1, 5)], duplicate=True)
        with patch.object(d, "PAGE_SIZE", 2), self.assertRaises(d.PermanentError):
            d.fetch_catalogue(http)

    def test_dateline_not_world_spanning(self):
        geometry = d.normalize_geometry({"type": "Polygon", "coordinates": [
            [[179, 0], [-179, 0], [-179, 1], [179, 1], [179, 0]]]})
        self.assertAlmostEqual(geometry.area, 2)
        self.assertFalse(geometry.intersects(d.box(0, 0, 1, 1)))
        self.assertTrue(geometry.intersects(d.box(-180, 0, -179, 1)))

    def test_country_holes_preserved(self):
        geometry = d.normalize_geometry({"type": "Polygon", "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]]})
        self.assertAlmostEqual(geometry.area, 12)
        self.assertFalse(geometry.intersects(d.box(1.5, 1.5, 2.5, 2.5)))

    def test_plan_downloads_shared_tile_once(self):
        self.s.countries = ["THA", "NER"]
        geometry = d.box(0.2, 0.2, 0.8, 0.8)
        with patch.object(d, "boundary", return_value=(geometry, {"test": True})):
            selection, jobs = d.make_plan([product()], self.s, None)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(jobs[0]["targets"]), 3)
        self.assertEqual(set(selection), {"Global_GLO30", "THA", "NER"})

    def test_ignored_range_restarts_instead_of_appending(self):
        self.assertEqual(d.response_layout(Response(200, {"Content-Length": "100"}), 50, 100), ("wb", 0))

    def test_valid_range_appends(self):
        self.assertEqual(d.response_layout(Response(206, {"Content-Range": "bytes 50-99/100"}), 50, 100), ("ab", 50))

    def test_wrong_range_rejected(self):
        with self.assertRaises(d.PermanentError):
            d.response_layout(Response(206, {"Content-Range": "bytes 0-49/100"}), 50, 100)

    def test_http_403_stops_without_retry(self):
        http = d.HTTP(Mock())
        http.tokens.get.return_value = "fake-token"
        session = Mock()
        session.request.return_value = Response(403)
        with patch.object(http, "session", return_value=session), self.assertRaises(d.PermanentError):
            http.get("https://example.invalid", auth=True)
        self.assertEqual(session.request.call_count, 1)
        self.assertTrue(d.STOP.is_set())
        self.assertTrue(session.request.return_value.closed)

    def test_retry_after_preserved(self):
        http = d.HTTP()
        session = Mock()
        session.request.return_value = Response(429, {"Retry-After": "73"})
        with patch.object(http, "session", return_value=session), self.assertRaises(d.RetryableError) as failure:
            http.get("https://example.invalid", attempts=1)
        self.assertEqual(failure.exception.retry_after, 73)

    def test_interruptible_wait(self):
        d.STOP.set()
        with self.assertRaises(d.Cancelled):
            d.pause(120)

    def test_tiff_header_rejects_error_html(self):
        path = self.root / "bad.tif"
        path.write_bytes(b"<html>error</html>")
        self.assertFalse(d.tiff_header_ok(path))

    def test_documented_product_filename_conventions(self):
        tile = "Copernicus_DSM_10_N00_00_E010_00"
        self.assertEqual(d.dem_filename_for(tile, "DGED"), tile + "_DEM.tif")
        self.assertEqual(d.dem_filename_for(tile, "DTED"), tile + "_DEM.dt2")

    def test_dted_signature(self):
        path = self.root / "sample.dt2"
        path.write_bytes(b"UHL1synthetic")
        self.assertTrue(d.raster_header_ok(path, "DTED"))
        self.assertFalse(d.raster_header_ok(path, "DGED"))

    def test_module_can_start_with_dted_selected(self):
        source = Path(d.__file__).read_text(encoding="utf-8")
        source = source.replace('"DGED": True,\n    "DTED": False,', '"DGED": False,\n    "DTED": True,', 1)
        module_path = self.root / "dted_mode.py"
        module_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("dted_mode_test", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        self.assertEqual(module.PRODUCT_FORMAT, "DTED")
        self.assertEqual(module.DATASET, "COP-DEM_GLO-30-DTED/2024_1")
        self.assertEqual(module.GLOBAL_FOLDER, "Global_GLO30_DTED")
        self.assertEqual(module.target_name("THA"), "THA_DTED")
        tile, _ = module.grid_info("N00_E010")
        self.assertEqual(module.dem_filename_for(tile), tile + "_DEM.dt2")

    def test_hash_detects_same_size_change(self):
        p = product()
        path = self.root / p["dem_filename"]
        path.write_bytes(b"II*\x00original")
        receipt = {"dataset": d.DATASET, "product_id": p["product_id"], "size_bytes": path.stat().st_size,
                   "sha256": d.sha256_file(path), "files": {str(path.resolve()): d.file_state(path)}}
        self.assertTrue(d.valid_file(path, p, receipt, True))
        state = path.stat()
        path.write_bytes(b"II*\x00modified")
        os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns))
        self.assertTrue(d.valid_file(path, p, receipt, False))
        self.assertFalse(d.valid_file(path, p, receipt, True))

    def test_complete_partial_resumed_without_http(self):
        p = product()
        data = b"II*\x00sample"
        info = {"size_bytes": len(data), "etag": "", "last_modified": ""}
        part = self.s.meta / "partials" / (p["dem_filename"] + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(data)
        d.atomic_json(part.with_name(part.name + ".json"), {"product_id": p["product_id"], "dataset": d.DATASET, **info})
        http = Mock()
        self.assertEqual(d.download_file(http, p, self.s, info, d.Progress()), part)
        http.get.assert_not_called()

    def test_mock_download_places_in_two_folders_and_audits(self):
        p = product()
        payload = b"II*\x00" + b"demo-data" * 30
        http = Mock()
        http.get.return_value = Response(200, {"Content-Length": str(len(payload))}, payload)
        targets = [self.s.base / "THA", self.s.base / "NER"]
        job = {"product": p, "targets": targets}
        with patch.object(d, "remote_info", return_value={"size_bytes": len(payload), "etag": "", "last_modified": ""}):
            result = d.run_job(job, self.s, http, d.Progress(), [])
        self.assertEqual(result["status"], "downloaded")
        for folder in targets:
            self.assertEqual((folder / p["dem_filename"]).read_bytes(), payload)
        self.assertEqual(http.get.call_count, 1)
        self.assertTrue(d.audit({"THA": [p], "NER": [p]}, self.s, self.root / "reports"))
        (targets[1] / p["dem_filename"]).unlink()
        self.assertFalse(d.audit({"THA": [p], "NER": [p]}, self.s, self.root / "reports2"))

    def test_audit_does_not_confuse_existence_with_verification(self):
        p = product()
        directory = self.s.base / "Global_GLO30"
        directory.mkdir()
        (directory / p["dem_filename"]).write_bytes(b"II*\x00some-data")
        self.assertFalse(d.audit({"Global_GLO30": [p]}, self.s, self.root / "reports"))
        result = d.load_json(self.root / "reports" / "audit_summary.json")["targets"]["Global_GLO30"]
        self.assertTrue(result["filename_match"])
        self.assertEqual(result["checks"]["UNVERIFIED"], 1)

    def test_legacy_adoption_no_download(self):
        p = product()
        source = self.s.base / d.GLOBAL_FOLDER
        source.mkdir(exist_ok=True)
        payload = b"II*\x00legacy-data"
        (source / p["dem_filename"]).write_bytes(payload)
        http = Mock()
        with patch.object(d, "remote_info", return_value={"size_bytes": len(payload), "etag": "", "last_modified": ""}):
            result = d.run_job({"product": p, "targets": [self.s.base / "THA"]}, self.s, http, d.Progress(), [source])
        self.assertEqual(result["status"], "reused")
        http.get.assert_not_called()
        self.assertTrue((source / p["dem_filename"]).exists())

    def test_stale_receipt_is_not_adopted_as_another_product(self):
        p, old = product(), product(2)
        folder = self.s.base / "Global_GLO30"
        folder.mkdir()
        data = b"II*\x00old-data"
        (folder / p["dem_filename"]).write_bytes(data)
        d.atomic_json(d.receipt_path(self.s, p["dem_filename"]), {"product_id": old["product_id"], "dataset": d.DATASET,
                      "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        http = Mock()
        http.get.return_value = Response(200, {"Content-Length": str(len(data))}, b"II*\x00new-data")
        with patch.object(d, "remote_info", return_value={"size_bytes": len(data), "etag": "", "last_modified": ""}):
            result = d.run_job({"product": p, "targets": [folder]}, self.s, http, d.Progress(), [])
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual((folder / p["dem_filename"]).read_bytes(), b"II*\x00new-data")

    def test_no_credentials_in_serialized_receipt(self):
        p = product()
        data = {"dataset": d.DATASET, "product_id": p["product_id"], "size_bytes": 100, "sha256": "abc"}
        path = d.receipt_path(self.s, p["dem_filename"])
        d.atomic_json(path, data)
        self.assertNotIn("password", path.read_text())
        self.assertNotIn("access_token", path.read_text())


    def make_cache(self, products):
        d.atomic_json(self.s.meta / "catalogue_verified.json", {
            "schema": d.SCHEMA, "dataset": d.DATASET, "complete": True,
            "retrieved_utc": d.now(), "catalogue_count": len(products), "rows": products})

    def test_main_dry_run_is_offline_with_cached_global_inventory(self):
        self.make_cache([product()])
        with patch.object(d.HTTP, "get", side_effect=AssertionError("Unexpected network")):
            with contextlib.redirect_stdout(io.StringIO()):
                result = d.main(["--root", str(self.root), "--download-all", "--dry-run"])
        self.assertEqual(result, 0)
        reports = list((self.s.base / "logs").glob("*/run_info.json"))
        self.assertEqual(d.load_json(reports[0])["status"], "planned_no_dem_download")

    def test_main_full_mock_download_then_offline_audit(self):
        p = product()
        self.make_cache([p])
        data = b"II*\x00".decode("unicode_escape").encode("latin1") + b"synthetic" * 100
        session = Mock()
        session.request.side_effect = lambda method, *args, **kw: Response(
            200, {"Content-Length": str(len(data))}, content=data if method == "GET" else b"")
        with patch.object(d.HTTP, "session", return_value=session), patch.object(d.Tokens, "get", return_value="synthetic-token"):
            with contextlib.redirect_stdout(io.StringIO()):
                code = d.main(["--root", str(self.root), "--download-all"])
        self.assertEqual(code, 0)
        self.assertEqual((self.s.base / "Global_GLO30" / p["dem_filename"]).read_bytes(), data)
        with patch.object(d.HTTP, "get", side_effect=AssertionError("Unexpected network")):
            with contextlib.redirect_stdout(io.StringIO()):
                code = d.main(["--root", str(self.root), "--download-all", "--audit-only"])
        self.assertEqual(code, 0)

    def test_remote_info_uses_single_range_probe(self):
        http = Mock()
        http.get.return_value = Response(206, {"Content-Range": "bytes 0-0/500"})
        result = d.remote_info(http, product())
        self.assertEqual(result["size_bytes"], 500)
        self.assertEqual(http.get.call_count, 1)

    def test_local_scan_adopts_existing_raster_without_network(self):
        p = product()
        source = self.s.base / d.GLOBAL_FOLDER
        source.mkdir(exist_ok=True)
        payload = b"II*\x00legacy-data"
        (source / p["dem_filename"]).write_bytes(payload)
        target = self.s.base / "THA"
        target.mkdir()
        progress = d.Progress()
        jobs = [{"product": p, "targets": [target]}]
        local, missing = d.scan_local_jobs(jobs, self.s, progress)
        self.assertEqual(len(local), 1)
        self.assertEqual(missing, [])
        results = d.process_local_jobs(local, self.s, progress)
        self.assertEqual(results[0]["status"], "found_local")
        self.assertEqual((target / p["dem_filename"]).read_bytes(), payload)
        receipt = d.load_json(d.receipt_path(self.s, p["dem_filename"]))
        self.assertIn("remote object not queried", receipt["verification"])

    def test_local_scan_only_marks_missing_jobs_for_download(self):
        a, b = product(1, "N00_E000"), product(2, "N00_E001")
        source = self.s.base / d.GLOBAL_FOLDER
        source.mkdir(exist_ok=True)
        (source / a["dem_filename"]).write_bytes(b"II*\x00local")
        target = self.s.base / "THA"
        target.mkdir()
        jobs = [{"product": a, "targets": [target]}, {"product": b, "targets": [target]}]
        local, missing = d.scan_local_jobs(jobs, self.s, d.Progress())
        self.assertEqual([job[0]["product"]["product_id"] for job in local], [a["product_id"]])
        self.assertEqual([job["product"]["product_id"] for job in missing], [b["product_id"]])

    def test_main_all_local_does_not_request_login(self):
        p = product()
        self.make_cache([p])
        source = self.s.base / d.GLOBAL_FOLDER
        source.mkdir(exist_ok=True)
        (source / p["dem_filename"]).write_bytes(b"II*\x00local-data")
        with patch.object(d.Tokens, "get", side_effect=AssertionError("Login should not be requested")):
            with contextlib.redirect_stdout(io.StringIO()):
                code = d.main(["--root", str(self.root), "--download-all"])
        self.assertEqual(code, 0)

    def test_short_transfer_never_publishes_raster(self):
        p = product()
        http = Mock()
        http.get.return_value = Response(200, {"Content-Length": "50"}, content=b"II*\x00short")
        with patch.object(d, "remote_info", return_value={"size_bytes": 50}), patch.object(d, "MAX_ATTEMPTS", 2), patch.object(d, "pause"):
            result = d.run_job({"product": p, "targets": [self.s.base / "THA"]}, self.s, http, d.Progress(), [])
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.s.base / "THA" / p["dem_filename"]).exists())
        self.assertTrue((self.s.meta / "partials" / (p["dem_filename"] + ".part")).exists())

    def test_stop_during_transfer_keeps_partial_not_final(self):
        class InterruptResponse(Response):
            def iter_content(self, chunk_size):
                yield b"II*\x00part"
                d.STOP.set()
                yield b"more"
        p = product()
        http = Mock()
        http.get.return_value = InterruptResponse(200, {"Content-Length": "50"})
        with patch.object(d, "remote_info", return_value={"size_bytes": 50}):
            result = d.run_job({"product": p, "targets": [self.s.base / "THA"]}, self.s, http, d.Progress(), [])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse((self.s.base / "THA" / p["dem_filename"]).exists())
        self.assertTrue((self.s.meta / "partials" / (p["dem_filename"] + ".part")).exists())

    def test_hardlink_placement_or_safe_copy_fallback(self):
        source, destination = self.root / "source.tif", self.root / "target" / "dest.tif"
        source.write_bytes(b"example")
        self.s.copy_mode = "hardlink"
        d.place_file(source, destination, self.s)
        self.assertEqual(source.read_bytes(), destination.read_bytes())

    @unittest.skipUnless(importlib.util.find_spec("rasterio"), "Optional rasterio is not installed")
    def test_deep_audit_reads_real_synthetic_geotiff(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        path = self.root / "synthetic.tif"
        with rasterio.open(path, "w", driver="GTiff", height=4, width=4, count=1,
                           dtype="float32", crs="EPSG:4326", transform=from_origin(0, 1, 0.25, 0.25)) as dataset:
            dataset.write(np.ones((4, 4), dtype="float32"), 1)
        d.deep_raster_check(path)


if __name__ == "__main__":
    unittest.main()
