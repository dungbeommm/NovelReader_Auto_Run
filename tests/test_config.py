import json
import unittest
from pathlib import Path

import jsonschema

from app.telegram_bot import DEFAULT_CONFIG, set_path
from app.pixeldrain import PixeldrainStore
from unittest.mock import Mock


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

    def test_pixeldrain_paths_and_result_sharing(self):
        store = PixeldrainStore("test-key", "/me")
        self.assertEqual(store.child("/me/jobs", "a b"), "/me/jobs/a b")
        self.assertEqual(store._url("/me/jobs/a b"),
                         "https://pixeldrain.com/api/filesystem/me/jobs/a%20b")
        with self.assertRaises(ValueError):
            store.child("/me", "../escape")
        response = Mock()
        response.json.return_value = {"id": "abcd1234"}
        store.session.request = Mock(return_value=response)
        self.assertEqual(store.share_link_reader("/me/jobs/result"),
                         "https://pixeldrain.com/d/abcd1234")
        self.assertEqual(store.session.request.call_args.args[:2],
                         ("POST", "https://pixeldrain.com/api/filesystem/me/jobs/result"))

    def test_pixeldrain_manifest_round_trip_requests(self):
        store = PixeldrainStore("test-key")
        response = Mock()
        response.json.side_effect = [
            {"path": [{"type": "dir"}], "base_index": 0,
             "children": [{"type": "file", "name": "job.json"}]},
            {"job_id": "example", "status": "QUEUED"},
        ]
        store.session.request = Mock(return_value=response)
        manifest = {"job_id": "example", "status": "QUEUED"}
        store.put_json("/me/jobs/example", "job.json", manifest)
        self.assertEqual(store.get_json("/me/jobs/example", "job.json"), manifest)
        method, url = store.session.request.call_args_list[0].args[:2]
        self.assertEqual((method, url),
                         ("PUT", "https://pixeldrain.com/api/filesystem/me/jobs/example/job.json"))

    def test_pixeldrain_forbidden_explains_filesystem_requirement(self):
        store = PixeldrainStore("test-key")
        response = Mock(status_code=403)
        store.session.request = Mock(return_value=response)
        with self.assertRaisesRegex(RuntimeError, "Pro/Prepaid"):
            store.stat("/me")


if __name__ == "__main__":
    unittest.main()
