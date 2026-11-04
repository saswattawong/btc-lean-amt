import datetime as dt
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from monitor_intraday_freshness import SCHEMA, observe
from publish_outputs import publish, validate_paths


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
    @staticmethod
    def git(*args, cwd):
        result = subprocess.run(
            ("git", *args), cwd=cwd, check=False, text=True,
            capture_output=True,
        )
        if result.returncode:
            raise AssertionError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def test_freshness_slo_uses_source_time_at_observation(self):
        source = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        first = observe(
            snapshot(source, source + dt.timedelta(minutes=1)),
            observed_at=source + dt.timedelta(minutes=14),
            observation_kind="schedule",
        )
        self.assertEqual(first["schema"], SCHEMA)
        self.assertTrue(first["latest_observation"]["within_target"])
        second = observe(
            snapshot(source, source + dt.timedelta(minutes=1)),
            first,
            observed_at=source + dt.timedelta(minutes=26),
            observation_kind="scheduled_fallback",
        )
        self.assertFalse(second["latest_observation"]["within_target"])
        self.assertEqual(second["rolling_24h"]["pass_rate_pct"], 50.0)
        self.assertEqual(second["rolling_24h"]["expected_observations"], 144)
        self.assertEqual(second["rolling_24h"]["target_met"], "insufficient_history")
        self.assertEqual(second["latest_observation"]["observation_kind"], "scheduled_fallback")

    def test_manual_and_legacy_observations_do_not_inflate_schedule_coverage(self):
        source = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        manual = observe(
            snapshot(source, source + dt.timedelta(minutes=1)),
            observed_at=source + dt.timedelta(minutes=2),
            observation_kind="manual",
        )
        self.assertEqual(manual["rolling_24h"]["observations"], 0)
        legacy = dict(manual, schema="btc_intraday_freshness_slo_v1")
        legacy["observations"] = [
            {key: value for key, value in manual["latest_observation"].items()
             if key not in {"observation_kind", "slo_eligible"}}
        ]
        scheduled = observe(
            snapshot(source, source + dt.timedelta(minutes=1)),
            legacy,
            observed_at=source + dt.timedelta(minutes=12),
            observation_kind="schedule",
        )
        self.assertEqual(scheduled["rolling_24h"]["observations"], 1)
        self.assertEqual(scheduled["observations"][0]["observation_kind"], "legacy_unclassified")

    def test_publish_scope_rejects_traversal(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = pathlib.Path(directory).resolve()
            output = root / "output.json"
            output.write_text("{}", encoding="utf-8")
            self.assertEqual(len(validate_paths(["output.json"], root)), 1)
            with self.assertRaisesRegex(ValueError, "inside the repository"):
                validate_paths(["../outside.json"], root)

    @unittest.skipIf(os.name == "nt", "local bare-repository push requires MSYS IPC")
    def test_publish_preserves_scoped_output_when_remote_changes_during_collection(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = pathlib.Path(directory)
            origin = root / "origin.git"
            seed = root / "seed"
            collector = root / "collector"
            concurrent = root / "concurrent"
            self.git("init", "--bare", str(origin), cwd=root)
            self.git("init", "-b", "main", str(seed), cwd=root)
            (seed / "output").mkdir()
            (seed / "output" / "latest.json").write_text("seed\n", encoding="utf-8")
            self.git("add", ".", cwd=seed)
            self.git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                     "commit", "-m", "seed", cwd=seed)
            self.git("remote", "add", "origin", str(origin), cwd=seed)
            self.git("push", "-u", "origin", "main", cwd=seed)
            self.git("clone", "--branch", "main", str(origin), str(collector), cwd=root)
            self.git("clone", "--branch", "main", str(origin), str(concurrent), cwd=root)

            (collector / "output" / "latest.json").write_text("collector\n", encoding="utf-8")
            (concurrent / "output" / "latest.json").write_text("concurrent\n", encoding="utf-8")
            self.git("add", "output/latest.json", cwd=concurrent)
            self.git("-c", "user.name=Other", "-c", "user.email=other@example.com",
                     "commit", "-m", "concurrent", cwd=concurrent)
            self.git("push", "origin", "main", cwd=concurrent)

            previous = pathlib.Path.cwd()
            try:
                os.chdir(collector)
                published = publish(["output/latest.json"], "publish collector", attempts=1)
            finally:
                os.chdir(previous)
            self.assertIsNotNone(published)
            self.assertEqual(
                self.git("--git-dir", str(origin), "show", "main:output/latest.json", cwd=root),
                "collector\n",
            )

    def test_intraday_workflow_preserves_bounded_recovery_checkpoint(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "intraday.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("timeout-minutes: 9", workflow)
        self.assertIn("cron: '7-57/10 * * * *'", workflow)
        self.assertIn("for attempt in 1 2", workflow)
        self.assertIn("--expected-cadence-seconds 600", workflow)
        self.assertIn("--observation-kind", workflow)
        self.assertIn("scheduled_fallback", workflow)
        self.assertIn("- 'derivatives.py'", workflow)
        self.assertIn("--perp-backfill-chunk-minutes 5", workflow)
        self.assertIn("--perp-backfill-workers 12", workflow)
        self.assertIn("--perp-max-requests 1600", workflow)
        self.assertIn("--perp-runtime-budget-seconds 150", workflow)
        self.assertIn("output/perp_cvd_state.json", workflow)

    def test_archive_workflow_requires_current_source_and_dispatches_fallbacks(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "cvd-hourly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("cron: '7,22,37,52 * * * *'", workflow)
        self.assertIn("--require-latest-completed-day", workflow)
        self.assertIn("for attempt in 1 2 3", workflow)
        self.assertIn("gh workflow run intraday.yml", workflow)
        self.assertIn("observation_kind=scheduled_fallback", workflow)
        self.assertIn("gh workflow run daily.yml", workflow)

    def test_daily_workflow_enforces_target_day_and_scoped_publish(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "daily.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--expected-source-day ${{ steps.freshness.outputs.target }}", workflow)
        self.assertIn("python publish_outputs.py", workflow)
        self.assertNotIn("git pull --rebase", workflow)


if __name__ == "__main__":
    unittest.main()
