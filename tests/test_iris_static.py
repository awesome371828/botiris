import ast
import pathlib
import re
import unittest
from datetime import timedelta

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

    def test_natural_temporary_ban_duration_phrases(self):
        selected = []
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_TIME_UNITS"
                for target in node.targets
            ):
                selected.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "_unit_seconds", "parse_duration_prefix"
            }:
                selected.append(node)
        namespace = {"re": re, "timedelta": timedelta}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
        parse = namespace["parse_duration_prefix"]

        cases = [
            (["1", "день"], timedelta(days=1), 2),
            (["день"], timedelta(days=1), 1),
            (["один", "день", "спам"], timedelta(days=1), 2),
            (["на", "одну", "неделю", "спам"], timedelta(days=7), 3),
            (["1", "месяц"], timedelta(days=30), 2),
            (["год"], timedelta(days=365), 1),
            (["2", "дня", "и", "3", "часа", "флуд"], timedelta(days=2, hours=3), 5),
            (["1", "час", "30", "минут", "причина"], timedelta(hours=1, minutes=30), 4),
            (["день"], timedelta(days=1), 1),
            (["год"], timedelta(days=365), 1),
        ]
        for tokens, expected_duration, expected_consumed in cases:
            with self.subTest(tokens=tokens):
                result = parse(tokens)
                self.assertIsNotNone(result)
                self.assertEqual(result[0], expected_duration)
                self.assertEqual(result[1], expected_consumed)
        self.assertIsNone(parse(["2", "года"]))


    def test_moderation_and_runtime_safety_guards_are_present(self):
        for fragment in (
            "Activity queue is full",
            "Three active warnings",
            "Telegram cannot restrict a chat creator or administrator",
        ):
            self.assertIn(fragment.casefold(), self.source.casefold())

    def test_runtime_reliability_guards_exist(self):
        for fragment in (
            "def _is_transient_db_error",
            "def _db_read_with_retry",
            "acquire_single_instance_lock()",
            "_ACTIVITY_OVERFLOW_MAX = 20000",
            "async def _drain_activity_queue",
            "_ACTIVITY_WRITER_BUSY = False",
            "Temporary Supabase",
        ):
            self.assertIn(fragment, self.source)

    def test_schema_has_query_indexes_for_chat_scoped_lists(self):
        schema = SCHEMA.read_text(encoding="utf-8").lower()
        self.assertIn("bot_activity_daily_chat_day_idx", schema)
        self.assertIn("bot_audit_chat_target_created_idx", schema)
        self.assertIn("bot_marriages_chat_idx", schema)
        self.assertIn("bot_users_chat_last_seen_idx", schema)
        self.assertIn("bot_marriages_chat_status_created_idx", schema)

    def test_activity_counters_are_idempotent_and_retryable(self):
        self.assertIn('"message_id": int(message_id)', self.source)
        self.assertIn('db.rpc("record_bot_activity_batch"', self.source)
        self.assertIn("Activity batch persistence failed (attempt %s/3; size=%s)", self.source)
        schema = SCHEMA.read_text(encoding="utf-8").lower()
        self.assertIn("alter table public.bot_activity_events add column if not exists message_id bigint", schema)
        self.assertIn("bot_activity_events_chat_message_uidx", schema)
        self.assertIn("create or replace function public.record_bot_activity_batch", schema)
        self.assertIn("on conflict (chat_id, message_id) do nothing", schema)

    def test_polling_conflicts_stop_the_restart_loop(self):
        self.assertIn("except TelegramConflictError", self.source)
        self.assertIn("raise SystemExit(23)", self.source)
        workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")
        self.assertIn('elif [ "$code" -eq 23 ]', workflow)
        self.assertIn("stopping instead of restarting forever", workflow)

    def test_moderation_protects_chat_creator_and_telegram_admins(self):
        self.assertIn("async def ensure_moderation_target_safe", self.source)
        self.assertIn("Нельзя применять это действие к создателю чата", self.source)
        self.assertIn("Telegram не позволяет боту применять это действие к администратору чата", self.source)
        self.assertIn('ensure_moderation_target_safe(message, target, "mute")', self.source)
        self.assertIn('ensure_moderation_target_safe(message, target, "ban")', self.source)
        self.assertIn('ensure_moderation_target_safe(message, target, "kick")', self.source)

    def test_global_ban_is_enforced_before_message_handlers(self):
        self.assertIn("await asyncio.to_thread(_is_globally_banned, event.from_user.id)", self.source)
        self.assertIn("Доступ к функциям Pear CM для этого аккаунта ограничен", self.source)
        self.assertIn("await asyncio.to_thread(_is_globally_banned, callback.from_user.id)", self.source)

    def test_admin_listing_includes_real_telegram_admins(self):
        self.assertIn("telegram_admin_ids.add(u.id)", self.source)
        self.assertIn("Администраторы Telegram", self.source)
        self.assertIn("Telegram administrators are listed once", self.source)

    def test_warning_removal_requires_audit_ledger_write(self):
        unwarn_start = self.source.index("async def unwarn_cmd")
        unwarn_end = self.source.index("@router.message(Command(\"clearwarns\")", unwarn_start)
        unwarn_body = self.source[unwarn_start:unwarn_end]
        self.assertLess(unwarn_body.index('ledger_saved = await audit_wait'), unwarn_body.index('db_upsert, "bot_users"'))
        clear_start = self.source.index("async def clearwarns_cmd")
        clear_end = self.source.index("# ── Message tools", clear_start)
        clear_body = self.source[clear_start:clear_end]
        self.assertLess(clear_body.index('ledger_saved = await audit_wait'), clear_body.index('db_upsert, "bot_users"'))

    def test_warning_updates_check_database_write_results(self):
        self.assertIn("Не удалось записать снятие предупреждения", self.source)
        self.assertIn("Не удалось записать сброс предупреждений", self.source)

    def test_chat_lock_persists_restore_state_before_telegram_change(self):
        close_start = self.source.index("async def close_chat_cmd")
        close_end = self.source.index("@router.message(Command(\"schedule\"", close_start)
        close_body = self.source[close_start:close_end]
        self.assertLess(close_body.index("pre_saved = await asyncio.to_thread(db_upsert"), close_body.index("await bot.set_chat_permissions"))
        self.assertIn("Не закрываю чат: база данных не настроена", close_body)
        self.assertIn("не удалось сохранить исходные разрешения", close_body)

    def test_unmute_restores_chat_default_permissions(self):
        start = self.source.index("async def unmute_cmd")
        end = self.source.index("@router.message(Command(\"ban\")", start)
        body = self.source[start:end]
        self.assertIn("default_permissions = getattr(chat_info, \"permissions\", None)", body)
        self.assertIn("ChatPermissions(**default_permissions.model_dump(exclude_none=True))", body)

    def test_chat_lock_restores_saved_telegram_permissions(self):
        self.assertIn("current_permissions = getattr(chat_info, \"permissions\", None)", self.source)
        self.assertIn("current_permissions.model_dump(exclude_none=True)", self.source)
        self.assertIn('settings["chat_permissions_before_lock"]', self.source)

    def test_delete_command_is_bounded(self):
        self.assertIn("requested < 1", self.source)
        self.assertIn("requested > 100", self.source)
        self.assertIn("previous_count = max(0, count - 1)", self.source)
        self.assertIn("deleted_total = individually_deleted + int(command_deleted)", self.source)
        self.assertIn("Delete individually", self.source)
        self.assertIn("Удалено сообщений:", self.source)
        self.assertIn("За один раз можно удалить максимум", self.source)

    def test_marriage_flow_authorizes_buttons_and_confirms_database_transitions(self):
        callback_start = self.source.index("async def marriage_proposal_callback")
        callback_end = self.source.index("@router.message(Command(\"marriage\"", callback_start)
        callback = self.source[callback_start:callback_end]
        self.assertIn('row.get("status") != "pending"', callback)
        self.assertIn('row.get("user2_id") or 0) != callback.from_user.id', callback)
        self.assertIn("Эти кнопки предназначены только тому, кому предложили брак", callback)
        divorce_start = self.source.index("async def divorce_cmd")
        divorce_end = self.source.index("# ── Integrated moderation stages", divorce_start)
        divorce = self.source[divorce_start:divorce_end]
        self.assertIn('.eq("status", "married").select("*").execute()', divorce)
        self.assertIn("Брак не подтверждён как расторгнутый", divorce)

    def test_database_enforces_single_active_marriage_and_valid_sql_function_quotes(self):
        schema = SCHEMA.read_text(encoding="utf-8").lower()
        self.assertIn("create or replace function public.prevent_overlapping_bot_marriages()", schema)
        self.assertIn("pg_advisory_xact_lock(new.chat_id)", schema)
        self.assertIn("create trigger bot_marriages_no_overlap", schema)
        self.assertNotRegex(schema, r"(?m)^as \\$")
        self.assertNotRegex(schema, r"(?m)^\\$;$")

    def test_command_aliases_and_answer_commands_do_not_share_marriage_matcher(self):
        marriage_start = self.source.index("async def marry_cmd")
        marriage_end = self.source.index("@router.callback_query(F.data.startswith(\"marriage:\"))", marriage_start)
        marriage = self.source[marriage_start:marriage_end]
        self.assertIn("(?:брак|браки|брачок|брачиться", self.source)
        self.assertIn('брак да', self.source)
        self.assertIn('брак нет', self.source)
        self.assertTrue(marriage.rstrip().endswith(")"))
        self.assertIn("CommandRateLimitMiddleware", self.source)
        self.assertIn("command_match = re.match", self.source)

    def test_top_defaults_to_current_chat_in_groups(self):
        self.assertIn("chat_only = is_group(message) and not global_scope", self.source)
        self.assertIn('global_scope = arg.startswith(("все чаты"', self.source)


if __name__ == "__main__":
    unittest.main()
