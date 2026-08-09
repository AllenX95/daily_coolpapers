import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db
from daily_coolpapers.form_commands import (
    FormValidationError,
    SettingsCommand,
    parse_bool,
    parse_json_object,
)


class SettingsCommandTests(unittest.TestCase):
    def test_boolean_parser_does_not_treat_zero_as_true(self):
        self.assertFalse(parse_bool("0", "enabled"))
        self.assertFalse(parse_bool("false", "enabled"))
        self.assertTrue(parse_bool("1", "enabled"))
        self.assertTrue(parse_bool("on", "enabled"))
        with self.assertRaises(FormValidationError):
            parse_bool("sometimes", "enabled")

    def test_json_headers_must_be_an_object(self):
        self.assertEqual(parse_json_object('{"X-Test":"yes"}', "headers"), '{"X-Test":"yes"}')
        for value in ["[]", "null", '"text"', "not-json"]:
            with self.subTest(value=value):
                with self.assertRaises(FormValidationError):
                    parse_json_object(value, "headers")

    def test_settings_command_validates_range_before_write(self):
        with self.assertRaises(FormValidationError) as caught:
            SettingsCommand.from_form({"abstract_concurrency": "21"})
        self.assertIn("abstract_concurrency", caught.exception.errors)


class SettingsPersistenceTests(unittest.TestCase):
    def test_save_settings_rolls_back_all_values_on_serialization_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "settings.sqlite3"):
                db.init_db()
                db.set_setting("test.first", "old")

                with self.assertRaises(TypeError):
                    db.save_settings(
                        {
                            "test.first": "new",
                            "test.invalid": object(),
                        }
                    )

                self.assertEqual(db.get_setting("test.first"), "old")
                self.assertIsNone(db.get_setting("test.invalid"))

    def test_get_settings_uses_defaults_for_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "settings.sqlite3"):
                db.init_db()
                db.set_setting("test.present", 7)
                values = db.get_settings({"test.present": 1, "test.missing": 2})

        self.assertEqual(values, {"test.present": 7, "test.missing": 2})

    def test_settings_view_falls_back_from_malformed_stored_values(self):
        from daily_coolpapers import app as app_module

        malformed = {
            **app_module.SETTINGS_DEFAULTS,
            "llm.abstract_concurrency": "not-an-int",
            "cache.cleanup_on_start": "sometimes",
        }
        with patch.object(app_module.db, "get_settings", return_value=malformed):
            values = app_module._settings_form_values()

        self.assertEqual(values["abstract_concurrency"], 4)
        self.assertTrue(values["cleanup_on_start"])


if __name__ == "__main__":
    unittest.main()
