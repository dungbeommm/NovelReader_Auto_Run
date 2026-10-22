import base64
import json
import unittest
from pathlib import Path

import jsonschema

from app.telegram_bot import DEFAULT_CONFIG, set_path
from app.drive import DriveStore, credentials_from_json


ROOT = Path(__file__).resolve().parents[1]


class ConfigContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "schemas" / "config.schema.json").read_text(encoding="utf-8"))

    def test_default_config_matches_schema(self):
        jsonschema.validate(DEFAULT_CONFIG, self.schema)

    def test_set_path_converts_boolean_number_and_string(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        set_path(config, "translation.pronoun_harmonizer", "off")
        set_path(config, "audio.speed", "1.2")
        set_path(config, "audio.voice", "test-voice")
        self.assertIs(config["translation"]["pronoun_harmonizer"], False)
        self.assertEqual(config["audio"]["speed"], 1.2)
        self.assertEqual(config["audio"]["voice"], "test-voice")

    def test_rejects_unknown_or_malformed_values(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        with self.assertRaises(ValueError):
            set_path(config, "translation.unknown", "x")
        with self.assertRaises(ValueError):
            set_path(config, "audio.workers", "many")
        with self.assertRaises(ValueError):
            set_path(config, "audio.enabled", "sometimes")

    def test_schema_rejects_unsupported_format(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["audio"]["format"] = "flac"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(config, self.schema)

    def test_bad_drive_secret_reports_expected_credential_format(self):
        with self.assertRaisesRegex(RuntimeError, "not valid JSON or Base64 JSON"):
            credentials_from_json("not-json")

    def test_unrecognized_drive_json_reports_auth_options(self):
        with self.assertRaisesRegex(RuntimeError, "OAuth client configuration only"):
            credentials_from_json('{"installed": {"client_id": "example"}}')

    def test_drive_credentials_accept_base64_encoded_authorized_user_json(self):
        payload = json.dumps({
            "type": "authorized_user",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        credentials = credentials_from_json(encoded)
        self.assertEqual(credentials.refresh_token, "refresh-token")

    def test_drive_credentials_accept_wrapped_and_line_wrapped_base64(self):
        payload = json.dumps({
            "type": "authorized_user",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        wrapped = f"base64:{encoded[:32]}\n{encoded[32:]}"
        credentials = credentials_from_json(wrapped)
        self.assertEqual(credentials.refresh_token, "refresh-token")

    def test_drive_credentials_accept_environment_assignment_and_utf8_bom(self):
        payload = json.dumps({
            "type": "authorized_user",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        credentials = credentials_from_json("GDRIVE_OAUTH_TOKEN_JSON=\ufeff" + payload)
        self.assertEqual(credentials.refresh_token, "refresh-token")

    def test_drive_credentials_accept_shell_quoted_json(self):
        payload = json.dumps({
            "type": "authorized_user",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        credentials = credentials_from_json(f"'{payload}'")
        self.assertEqual(credentials.refresh_token, "refresh-token")

    def test_drive_credentials_accept_markdown_fenced_json(self):
        payload = json.dumps({
            "type": "authorized_user",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        credentials = credentials_from_json(f"```json\n{payload}\n```")
        self.assertEqual(credentials.refresh_token, "refresh-token")

    def test_drive_listing_follows_all_pages(self):
        class Request:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class Files:
            def __init__(self):
                self.tokens = []

            def list(self, **kwargs):
                self.tokens.append(kwargs.get("pageToken"))
                if kwargs.get("pageToken") is None:
                    return Request({"files": [{"id": "one"}], "nextPageToken": "page-2"})
                return Request({"files": [{"id": "two"}]})

        class Api:
            def __init__(self):
                self.files_api = Files()

            def files(self):
                return self.files_api

        drive = DriveStore.__new__(DriveStore)
        drive.api = Api()
        self.assertEqual([x["id"] for x in drive._list("trashed=false")], ["one", "two"])
        self.assertEqual(drive.api.files_api.tokens, [None, "page-2"])


if __name__ == "__main__":
    unittest.main()
