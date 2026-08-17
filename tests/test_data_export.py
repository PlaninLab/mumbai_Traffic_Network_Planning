from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.data import store
from src.web.app import app


class DataExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "traffic.db"
        self.db_patch = patch.object(store, "DB_PATH", self.database_path)
        self.db_patch.start()

        store.insert_readings(
            [{
                "fetched_utc": "2026-08-17T04:00:00+00:00",
                "segment": "peak",
                "label": "morning",
                "idx": 7,
                "lat": 19.1,
                "lon": 72.8,
                "current_speed_kph": 31.5,
                "free_speed_kph": 52.0,
                "tti": 1.65,
                "confidence": 0.91,
                "road_closure": False,
                "provider": "here",
            }],
            "corridor-run",
        )
        store.insert_intersection_readings(
            [{
                "point_id": "junction-42",
                "scope": "bmc",
                "fetched_utc": "2026-08-17T04:05:00+00:00",
                "segment": "peak",
                "label": "morning",
                "lat": 19.11,
                "lon": 72.81,
                "name": "Test Junction",
                "current_speed_kph": 22.0,
                "free_speed_kph": 44.0,
                "tti": 2.0,
                "confidence": 0.88,
                "road_closure": False,
                "provider": "here",
            }],
            "intersection-run",
        )

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temporary_directory.cleanup()

    def test_combined_export_normalizes_both_reading_tables(self) -> None:
        output = Path(self.temporary_directory.name) / "all.csv"

        count = store.export_collected_readings_csv(output, batch_size=1)

        self.assertEqual(count, 2)
        with output.open(encoding="utf-8", newline="") as csv_file:
            rows = list(csv.DictReader(csv_file))
        self.assertEqual([row["source"] for row in rows], ["corridor", "intersection"])
        self.assertEqual(rows[0]["point_index"], "7")
        self.assertEqual(rows[0]["point_id"], "")
        self.assertEqual(rows[1]["point_id"], "junction-42")
        self.assertEqual(rows[1]["point_name"], "Test Junction")

    def test_dataset_filter_exports_only_intersections(self) -> None:
        output = Path(self.temporary_directory.name) / "intersections.csv"

        count = store.export_collected_readings_csv(output, "intersections")

        self.assertEqual(count, 1)
        rows = list(csv.DictReader(io.StringIO(output.read_text(encoding="utf-8"))))
        self.assertEqual(rows[0]["source"], "intersection")

    def test_endpoint_is_disabled_without_a_configured_token(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATA_EXPORT_TOKEN", None)
            with TestClient(app) as client:
                response = client.get("/api/export/readings.csv")

        self.assertEqual(response.status_code, 503)

    def test_endpoint_requires_token_and_returns_download(self) -> None:
        with patch.dict(os.environ, {"DATA_EXPORT_TOKEN": "export-secret"}):
            with TestClient(app) as client:
                unauthorized = client.get("/api/export/readings.csv")
                response = client.get(
                    "/api/export/readings.csv?dataset=intersections",
                    headers={"Authorization": "Bearer export-secret"},
                )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.headers["www-authenticate"], "Bearer")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-export-rows"], "1")
        self.assertIn("attachment;", response.headers["content-disposition"])
        self.assertIn("no-store", response.headers["cache-control"])
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual(rows[0]["point_id"], "junction-42")


if __name__ == "__main__":
    unittest.main()
