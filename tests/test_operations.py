import datetime as dt
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from monitor_intraday_freshness import SCHEMA, observe
from publish_outputs import validate_paths


UTC = dt.timezone.utc
TEMP_ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TEMP_ROOT.parent


def snapshot(source, generated):
    return {
        "schema": "btc_multi_provider_intraday_v2",
        "generated_at_utc": generated.isoformat().replace("+00:00", "Z"),
        "freshness": {"source_as_of_utc": source.isoformat().replace("+00:00", "Z")},
        "integrity": {"status": "partial"},
    }


class OperationsTest(unittest.TestCase):
    def test_freshness_slo_uses_source_time_at_observation(self):
        source = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        first = observe(
            snapshot(source, source + dt.timedelta(minutes=1)),
            observed_at=source + dt.timedelta(minutes=14),
        )
        self.assertEqual(first["schema"], SCHEMA)
        self.assertTrue(first["latest_observation"]["within_target"])
        second = observe(
            snapshot(source, source + dt.timedelta(minutes=1)),
            first,
            observed_at=source + dt.timedelta(minutes=16),
        )
        self.assertFalse(second["latest_observation"]["within_target"])
        self.assertEqual(second["rolling_24h"]["pass_rate_pct"], 50.0)

    def test_publish_scope_rejects_traversal(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = pathlib.Path(directory).resolve()
            output = root / "output.json"
            output.write_text("{}", encoding="utf-8")
            self.assertEqual(len(validate_paths(["output.json"], root)), 1)
            with self.assertRaisesRegex(ValueError, "inside the repository"):
                validate_paths(["../outside.json"], root)

    def test_intraday_workflow_preserves_bounded_recovery_checkpoint(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "intraday.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("timeout-minutes: 6", workflow)
        self.assertIn("--perp-backfill-chunk-minutes 5", workflow)
        self.assertIn("--perp-backfill-workers 12", workflow)
        self.assertIn("--perp-max-requests 1600", workflow)
        self.assertIn("--perp-runtime-budget-seconds 150", workflow)


if __name__ == "__main__":
    unittest.main()
