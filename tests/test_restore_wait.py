import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SANITIZER_RDS_CLUSTER_ID", "unit-test-cluster")

from botocore.exceptions import WaiterError  # noqa: E402

from sanitizer import rds  # noqa: E402
from sanitizer.settings import settings  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _waiter_error(status: str | None) -> WaiterError:
    clusters = [] if status is None else [{"Status": status}]
    return WaiterError(
        "db_cluster_available",
        "Max attempts exceeded",
        {"DBClusters": clusters},
    )


class RestoreWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.output: list[str] = []
        self.client = MagicMock()
        self.waiter = MagicMock()
        self.client.get_waiter.return_value = self.waiter
        self.client.describe_events.return_value = {"Events": []}
        self.patches = [
            patch.object(rds.time, "monotonic", self.clock.monotonic),
            patch.object(rds.time, "sleep", self.clock.sleep),
            patch.object(rds, "rds_client", self.client),
            patch.object(
                rds.click,
                "echo",
                side_effect=lambda message="", **kwargs: self.output.append(
                    str(message)
                ),
            ),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self.patches])
        settings.restore_timeout_mins = rds._MAX_RESTORE_MINS

    def test_creating_restore_survives_past_the_old_timeout(self) -> None:
        def wait(**kwargs):
            if self.clock.now >= 200 * 60:
                return None
            raise _waiter_error("creating")

        self.waiter.wait.side_effect = wait
        self.client.describe_events.return_value = {
            "Events": [
                {"Date": "t1", "Message": "DB cluster is being restored"},
                {"Date": "t1", "Message": "DB cluster is being restored"},
                {"Date": "t2", "Message": "Restored DB cluster from snapshot"},
            ]
        }

        rds.wait_for_cluster_restore("tmp-cluster")

        self.assertGreaterEqual(self.clock.now, 200 * 60)
        self.assertEqual(
            sum("DB cluster is being restored" in line for line in self.output),
            1,
        )
        self.assertTrue(
            any("Restored DB cluster from snapshot" in line for line in self.output)
        )
        self.client.describe_db_clusters.assert_not_called()

    def test_unknown_status_keeps_the_180_minute_limit(self) -> None:
        self.waiter.wait.side_effect = lambda **kwargs: (_ for _ in ()).throw(
            _waiter_error("backing-up")
        )

        with self.assertRaises(TimeoutError) as caught:
            rds.wait_for_cluster_restore("tmp-cluster")

        self.assertIn("backing-up", str(caught.exception))
        self.assertLess(self.clock.now, 190 * 60)
        self.assertGreaterEqual(self.clock.now, 180 * 60)

    def test_unreadable_status_keeps_the_180_minute_limit(self) -> None:
        self.waiter.wait.side_effect = lambda **kwargs: (_ for _ in ()).throw(
            _waiter_error(None)
        )
        with self.assertRaises(TimeoutError) as caught:
            rds.wait_for_cluster_restore("tmp-cluster")

        self.assertIn("None", str(caught.exception))
        self.assertLess(self.clock.now, 190 * 60)
        self.client.describe_db_clusters.assert_not_called()

    def test_terminal_status_fails_immediately(self) -> None:
        self.waiter.wait.side_effect = lambda **kwargs: (_ for _ in ()).throw(
            _waiter_error("incompatible-restore")
        )

        with self.assertRaises(TimeoutError) as caught:
            rds.wait_for_cluster_restore("tmp-cluster")

        self.assertIn("incompatible-restore", str(caught.exception))
        self.assertEqual(self.clock.now, 0)
        self.assertEqual(self.waiter.wait.call_count, 1)

    def test_event_lookup_failure_does_not_fail_the_restore(self) -> None:
        calls = {"n": 0}

        def wait(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _waiter_error("creating")
            return None

        self.waiter.wait.side_effect = wait
        self.client.describe_events.side_effect = RuntimeError("access denied")

        rds.wait_for_cluster_restore("tmp-cluster")

        self.assertEqual(self.client.describe_events.call_count, 1)
        self.assertTrue(any("RDS event logging disabled" in line for line in self.output))

    def test_creating_cluster_still_stops_at_the_cap(self) -> None:
        self.waiter.wait.side_effect = lambda **kwargs: (_ for _ in ()).throw(
            _waiter_error("creating")
        )

        with self.assertRaises(TimeoutError) as caught:
            rds.wait_for_cluster_restore("tmp-cluster")

        self.assertIn("creating", str(caught.exception))
        self.assertGreaterEqual(self.clock.now, rds._MAX_RESTORE_MINS * 60)
        self.assertLess(self.clock.now, (rds._MAX_RESTORE_MINS + 5) * 60)

    def test_unexpected_error_falls_back_to_the_historical_waiter(self) -> None:
        with patch.object(
            rds, "_wait_for_cluster_restore", side_effect=RuntimeError("boom")
        ):
            with patch.object(rds, "wait_resource") as fallback:
                rds.wait_for_cluster_restore("tmp-cluster")

        fallback.assert_called_once_with(
            "db_cluster_available",
            {"DBClusterIdentifier": "tmp-cluster"},
            "Restoring to temporary cluster",
            "Timed out when restoring cluster",
            180,
        )
        self.assertTrue(any("Falling back" in line for line in self.output))

    def test_timeout_is_not_swallowed_by_the_fallback(self) -> None:
        with patch.object(
            rds, "_wait_for_cluster_restore", side_effect=TimeoutError("still creating")
        ):
            with patch.object(rds, "wait_resource") as fallback:
                with self.assertRaises(TimeoutError):
                    rds.wait_for_cluster_restore("tmp-cluster")

        fallback.assert_not_called()

    def test_timeout_setting_is_clamped(self) -> None:
        self.waiter.wait.return_value = None

        settings.restore_timeout_mins = 10
        rds.wait_for_cluster_restore("tmp-cluster")
        self.assertTrue(any("up to 180 min" in line for line in self.output))

        self.output.clear()
        settings.restore_timeout_mins = 10000
        rds.wait_for_cluster_restore("tmp-cluster")
        self.assertTrue(any("up to 720 min" in line for line in self.output))

        settings.restore_timeout_mins = "nope"  # type: ignore[assignment]
        with patch.object(rds, "wait_resource") as fallback:
            rds.wait_for_cluster_restore("tmp-cluster")
        fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
