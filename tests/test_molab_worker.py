import unittest
from unittest.mock import Mock

from app.molab_worker import next_job, record_failure


class MoLabWorkerTests(unittest.TestCase):
    def test_next_job_picks_oldest_queued_or_running_job(self):
        store = Mock()
        store.ensure_folder.side_effect = lambda name, parent=None: (
            f"{parent}/{name}" if parent else name
        )
        store.list_children.return_value = [
            {"type": "dir", "path": "jobs/newer"},
            {"type": "dir", "path": "jobs/complete"},
            {"type": "dir", "path": "jobs/older"},
        ]
        manifests = {
            "jobs/newer": {"job_id": "newer", "status": "QUEUED", "created_at": "2026-02-02"},
            "jobs/complete": {"job_id": "done", "status": "COMPLETED", "created_at": "2026-01-01"},
            "jobs/older": {"job_id": "older", "status": "RUNNING", "created_at": "2026-01-02"},
        }
        store.get_json.side_effect = lambda path, name: manifests[path]

        folder, manifest = next_job(store)

        self.assertEqual(folder, "jobs/older")
        self.assertEqual(manifest["job_id"], "older")

    def test_record_failure_persists_terminal_manifest(self):
        store = Mock()
        manifest = {"status": "RUNNING", "errors": []}

        record_failure(store, "jobs/example", manifest, "RuntimeError: storage failed")

        self.assertEqual(manifest["status"], "FAILED")
        self.assertEqual(manifest["errors"][0]["stage"], "browser_worker")
        self.assertIn("storage failed", manifest["errors"][0]["detail"])
        store.put_json.assert_called_once_with("jobs/example", "job.json", manifest)


if __name__ == "__main__":
    unittest.main()
