import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.telegram_bot import TelegramBot
from app.storage import create_store


class BrowserAndTxtTests(unittest.TestCase):
    def test_create_store_supports_gdrive(self):
        with patch.dict("os.environ", {"STORAGE_BACKEND": "gdrive", "GDRIVE_OAUTH_TOKEN_JSON": '{"token": "test"}'}):
            with patch("google.oauth2.credentials.Credentials.from_authorized_user_info") as mock_cred:
                mock_cred.return_value = Mock(expired=False)
                with patch("googleapiclient.discovery.build") as mock_build:
                    store = create_store()
                    self.assertIsNotNone(store)
                    self.assertEqual(type(store).__name__, "DriveStore")

    def test_telegram_bot_accepts_txt_file(self):
        storage = Mock()
        storage.ensure_folder.side_effect = lambda name, parent=None: (parent + "/" if parent else "novelreader/") + name
        storage.upload.return_value = {"path": "novelreader/NovelReader_Auto_Run/jobs/job_1/source.txt"}
        storage.get_json.side_effect = lambda parent, name, default=None: (
            {"offset": 0, "users": {}} if name == "bot_state.json" else default
        )

        bot = TelegramBot("test_token", {"42"}, storage)
        bot.call = Mock()
        bot.call.return_value = {"file_path": "path/to/truyen_goc.txt"}
        bot.http = Mock()
        bot.http.get.return_value.content = b"Content of raw novel text"
        bot.http.get.return_value.raise_for_status = Mock()
        bot.send = Mock()

        message = {
            "chat": {"id": 100},
            "document": {"file_name": "truyen_goc.txt", "file_size": 25, "file_id": "file_xyz"}
        }
        bot._handle_document(message, "42")
        self.assertTrue(storage.upload.called)
        call_args = storage.upload.call_args
        self.assertEqual(call_args[0][2], "source.txt")


if __name__ == "__main__":
    unittest.main()
