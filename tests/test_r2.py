import io
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import jsonschema

from app.r2 import R2Store
from app.telegram_bot import DEFAULT_CONFIG, TelegramBot, normalize_config


class R2ContractTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.store = R2Store(client=self.client, bucket="books", root_id="novelreader")

    def test_job_paths_listing_and_json_round_trip(self):
        folder = self.store.ensure_folder("jobs", self.store.root_id)
        job = self.store.ensure_folder("abc", folder)
        self.assertEqual(job, "novelreader/jobs/abc")
        self.store.put_json(job, "job.json", {"status": "QUEUED"})
        args = self.client.put_object.call_args.kwargs
        self.assertEqual((args["Bucket"], args["Key"]), ("books", "novelreader/jobs/abc/job.json"))
        self.client.get_object.return_value = {"Body": io.BytesIO(args["Body"])}
        self.assertEqual(self.store.get_json(job, "job.json"), {"status": "QUEUED"})
        paginator = Mock()
        paginator.paginate.return_value = [{"CommonPrefixes": [{"Prefix": "novelreader/jobs/abc/"}],
                                            "Contents": [{"Key": "novelreader/jobs/"}]}]
        self.client.get_paginator.return_value = paginator
        self.assertEqual(self.store.list_children(folder), [{"name": "abc", "path": job, "type": "dir"}])

    def test_upload_download_and_private_link(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            source.write_bytes(b"example")
            self.assertEqual(self.store.upload(source, "novelreader/jobs/abc"),
                             {"path": "novelreader/jobs/abc/source.epub"})
            self.store.download("novelreader/jobs/abc/source.epub", Path(directory) / "out" / "source.epub")
        self.client.generate_presigned_url.return_value = "https://example.test/signed"
        self.assertEqual(self.store.share_link_reader("novelreader/jobs/abc/result"),
                         "https://example.test/signed")
        self.assertEqual(self.client.generate_presigned_url.call_args.kwargs["ExpiresIn"], 604800)
        self.assertEqual(self.client.generate_presigned_url.call_args.kwargs["Params"]["Key"],
                         "novelreader/jobs/abc/result/result.zip")

    def test_invalid_path_component(self):
        with self.assertRaises(ValueError):
            self.store.child(self.store.root_id, "../escape")

    def test_missing_configuration_lists_all_required_names(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "R2_BUCKET, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"):
                R2Store()


class BotStorageTests(unittest.TestCase):
    def test_old_preset_migrates_to_storage(self):
        old = deepcopy(DEFAULT_CONFIG)
        old["pixeldrain"] = old.pop("storage")
        normalized = normalize_config(old)
        self.assertNotIn("pixeldrain", normalized)
        self.assertEqual(normalized["storage"]["sharing"], "link_reader")
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "config.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(normalized, schema)

    def test_notifications_do_not_overwrite_worker_manifest(self):
        storage = Mock()
        storage.ensure_folder.side_effect = lambda name, parent=None: (parent + "/" if parent else "novelreader/") + name
        storage.get_json.side_effect = lambda parent, name, default=None: (
            {"offset": 0, "users": {}} if name == "bot_state.json" else
            {"job_id": "job", "owner_id": "123", "status": "FAILED", "progress": {},
             "errors": [{"stage": "worker", "detail": "boom"}], "result": None, "config": DEFAULT_CONFIG}
            if name == "job.json" else default)
        storage.list_children.return_value = [{"type": "dir", "path": "novelreader/NovelReader_Auto_Run/jobs/job"}]
        bot = TelegramBot("token", {"123"}, storage)
        bot.send = Mock()
        bot.notify_job_updates()
        self.assertEqual(bot.send.call_count, 2)
        written_names = [call.args[1] for call in storage.put_json.call_args_list]
        self.assertEqual(written_names, ["notifications.json", "notifications.json"])

    def test_start_queues_job_without_github_dispatch_token(self):
        storage = Mock()
        storage.ensure_folder.side_effect = lambda name, parent=None: (parent + "/" if parent else "novelreader/") + name
        storage.get_json.side_effect = lambda parent, name, default=None: (
            {"offset": 0, "users": {}} if name == "bot_state.json" else
            {"job_id": "job-123", "status": "CONFIGURING", "config": DEFAULT_CONFIG}
            if name == "job.json" else default)
        bot = TelegramBot("token", {"123"}, storage)
        bot.call = Mock()
        bot.send = Mock()
        bot.http.post = Mock()
        user = bot._user("123")
        user["draft"] = {"job_id": "job-123", "config": deepcopy(DEFAULT_CONFIG)}
        bot.handle({"callback_query": {"id": "callback", "from": {"id": 123},
                                       "message": {"chat": {"id": 456}}, "data": "start:job-123"}})
        bot.http.post.assert_not_called()
        manifest_writes = [call.args[2] for call in storage.put_json.call_args_list if call.args[1] == "job.json"]
        self.assertEqual(manifest_writes[-1]["status"], "QUEUED")
        self.assertIsNone(user["draft"])
