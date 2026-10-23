import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from huggingface_hub.errors import EntryNotFoundError
from app.huggingface import HuggingFaceStore
from app.storage import create_store


class HuggingFaceBucketTests(unittest.TestCase):
    def setUp(self):
        self.store = HuggingFaceStore(bucket="uri410/audio", token="test-token", check_bucket=False)

    def test_bucket_paths_and_result_url(self):
        job = self.store.ensure_folder("job-id", self.store.ensure_folder("jobs"))
        self.assertEqual(job, "novelreader/jobs/job-id")
        self.assertEqual(
            self.store.share_link_reader(job + "/result"),
            "https://huggingface.co/buckets/uri410/audio/resolve/novelreader/jobs/job-id/result/result.zip",
        )
        with self.assertRaises(ValueError):
            self.store.child(self.store.root_id, "../escape")

    @patch("app.huggingface.list_bucket_tree")
    def test_lists_directories_and_files(self, listing):
        listing.return_value = [
            SimpleNamespace(path="novelreader/jobs/job-id", type="directory"),
            SimpleNamespace(path="novelreader/jobs/readme.txt", type="file"),
        ]
        rows = self.store.list_children("novelreader/jobs")
        self.assertEqual(rows, [
            {"name": "job-id", "path": "novelreader/jobs/job-id", "type": "dir"},
            {"name": "readme.txt", "path": "novelreader/jobs/readme.txt", "type": "file"},
        ])
        self.assertEqual(listing.call_args.kwargs["prefix"], "novelreader/jobs/")
        self.assertIs(listing.call_args.kwargs["recursive"], False)

    @patch("app.huggingface.download_bucket_files")
    @patch("app.huggingface.batch_bucket_files")
    def test_manifest_and_file_round_trip(self, batch, download):
        folder = "novelreader/jobs/job-id"
        self.store.put_json(folder, "job.json", {"status": "QUEUED"})
        self.assertEqual(batch.call_args.kwargs["add"][0][1], folder + "/job.json")
        self.assertEqual(json.loads(batch.call_args.kwargs["add"][0][0]), {"status": "QUEUED"})

        def write_json(bucket, files, **kwargs):
            Path(files[0][1]).write_text('{"status":"QUEUED"}', encoding="utf-8")
        download.side_effect = write_json
        self.assertEqual(self.store.get_json(folder, "job.json"), {"status": "QUEUED"})
        download.side_effect = EntryNotFoundError("missing")
        self.assertEqual(self.store.get_json(folder, "notifications.json", {"errors": 0}), {"errors": 0})
        download.side_effect = write_json

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            source.write_bytes(b"epub")
            self.assertEqual(self.store.upload(source, folder), {"path": folder + "/source.epub"})
            self.store.download(folder + "/source.epub", Path(directory) / "out" / "source.epub")
            self.assertTrue(download.call_args.kwargs["raise_on_missing_files"])

    @patch("app.huggingface.bucket_info")
    def test_default_provider_uses_named_bucket(self, info):
        info.return_value = SimpleNamespace(private=False)
        with patch.dict("os.environ", {"HF_TOKEN": "test-token"}, clear=True):
            store = create_store()
        self.assertIsInstance(store, HuggingFaceStore)
        self.assertEqual(store.bucket, "uri410/audio")
        info.assert_called_once_with("uri410/audio", token="test-token")
