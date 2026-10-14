import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "iris.py"
SCHEMA = ROOT / "schema.sql"


class PearCMStaticRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(SOURCE))
        cls.function_names = {
            node.name for node in ast.walk(cls.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_existing_core_handlers_are_preserved(self):
        required = {
            "help_command", "admins", "mute_cmd", "unmute_cmd", "ban_cmd",
            "unban_cmd", "kick_cmd", "warn_cmd", "unwarn_cmd", "clearwarns_cmd",
            "purge_cmd", "top_cmd", "logs_cmd", "rank_cmd", "promote_rank_cmd",
            "demote_rank_cmd", "open_chat_cmd", "close_chat_cmd",
            "moderation_history_cmd", "owner_diagnostics",
        }
        self.assertTrue(required.issubset(self.function_names), required - self.function_names)

    def test_advanced_handlers_exist(self):
        required = {
            "temporary_ban_cmd", "admin_notes_cmd", "trusted_users_cmd",
            "punishment_reasons_cmd", "granular_permissions_cmd",
            "export_chat_settings_cmd", "import_chat_settings_cmd",
            "configure_welcome_cmd", "chat_schedule_cmd", "moderator_stats_cmd",
            "_schedule_worker", "_is_trusted_user", "_validate_imported_settings",
        }
        self.assertTrue(required.issubset(self.function_names), required - self.function_names)

    def test_monitor_only_filter_does_not_apply_automatic_penalties(self):
        function = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "auto_moderation_filters"
        )
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(function) if isinstance(node, ast.Call)
        }
        self.assertNotIn("delete_message", calls)
        self.assertNotIn("ban_chat_member", calls)
        self.assertNotIn("restrict_chat_member", calls)

    def test_additive_schema_for_new_features_exists(self):
        schema = SCHEMA.read_text(encoding="utf-8")
        for fragment in (
            "create table if not exists public.bot_trusted_users",
            "alter table public.bot_notes add column if not exists chat_id bigint",
            "alter table public.bot_notes add column if not exists actor_id bigint",
            "bot_trusted_users_chat_active_idx",
            "bot_notes_chat_user_created_idx",
        ):
            self.assertIn(fragment, schema.lower())


if __name__ == "__main__":
    unittest.main()
