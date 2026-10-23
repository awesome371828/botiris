# Community moderation bot (aiogram 3)

This is an original Iris-inspired community bot, not a full copy of Iris. It implements a starter set of common features within Telegram Bot API limits.

## Setup
1. Create a Telegram bot with BotFather and add it to your group.
2. Promote it to admin with only the permissions it needs: delete messages, restrict users, ban users, pin messages.
3. Create a Supabase project and run `schema.sql` in SQL Editor.
4. In GitHub repository Settings → Secrets and variables → Actions, add:
   - `TELEGRAM_BOT_TOKEN`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY` (preferred; keep secret)
   - optional `SUPABASE_ANON` fallback
5. Upload `bot.py`, `requirements.txt`, `schema.sql`, and `.github/workflows/main.yml`.
6. Go to Actions → Run Telegram bot → Run workflow.

## Commands
`/start`, `/help`, `профиль`, `варны`, `права`, `топ`, `кто админ`, `кто модер`,
`брак` (reply to a user), `развод`, `мут @user 10м reason`, `размут @user`,
`бан @user reason`, `разбан USER_ID`, `кик @user`, `варн @user reason`,
`снять варн @user`, `-смс` (reply), `.пин` (reply), `.анпин`, `очистить 10`,
`повысить @user`, `понизить @user`.

## Expanded Iris-style community commands

The main bot file now also includes these additional features:

- `.дк` — configurable per-command rank thresholds with expanded Russian word-form matching.
- `лдк` — per-user allow/deny overrides for moderation commands; use it as a reply, for example `лдк мут запрет`.
- `кто я`, `кто ты` (reply or known `@username`), `/whois` — participant status card.
- `браки`, `мой брак`, `брак участника` (reply or username), `/marriages` — list and inspect registered marriages.
- `награда Название` (reply), `награды`, `/awards` — award participants and view awards.
- `анкета`, `анкета интересы ...`, `анкета о себе ...`, `/profileform` — participant questionnaire.
- Role-play commands: `обнять`, `поцеловать`, `дать пять`, `пожать руку`, `погладить`, `похвалить`, `поздравить`, `поддержать`, `отношения`.
- Random community commands: `выбери участника`, `рандом участник`, `да или нет`, `шипперинг`, `жребий`.
- Virtual economy: `баланс`, `ежедневный бонус`, `передать 50` (reply), `магазин`, `купить активный`.
- Clans: `клан создать Название`, `клан вступить Название`, `клан инфо`, `клан участники`, `клан передать` (reply), `клан распустить`.
- `сбор` — notify a limited set of participants already observed by the bot.
- Leaderboard variants: `топ деньги`, `топ ранги`, `топ репутация`, `топ отношения`, `топ браки`, `топ кланы`, `топ награды`, `топ варны`, `топ сообщения`, `топ новички`.

The welcome handler already greets new non-bot members by default. The Telegram Bot API does not let bots enumerate every group member, so the participant picker and `сбор` use only users the bot has already observed.

**Database migration:** after updating the repository, run the updated `schema.sql` in Supabase SQL Editor to create the new profile, economy, clan, award and relationship tables. The bot cannot create those tables through the normal Bot API.

## Important limitations
- Telegram usernames cannot always be resolved by the Bot API unless the bot can access the user/chat. Reply to the person's message or use numeric IDs where supported.
- Internal ranks are not Telegram admin privileges. The bot cannot grant real Telegram admin status to a user.
- Marriage records currently live in process memory and reset when the bot restarts. User profile, warnings, ranks, message counts, and audit events use Supabase when configured.
- GitHub-hosted runners are not designed for permanent 24/7 bot hosting. Workflow runs have time limits and may be stopped. For stable uptime use a VPS or a persistent hosting service.
- This is not and cannot be “everything in Iris”: Iris has proprietary/large-scale features and platform constraints; this code is a base you can extend.


## Iris-style activity statistics and moderator list

- `топ вся`, `топ день` / `топ сутки`, `топ неделя`, `топ месяц`, `топ год` show chat-specific activity with clickable profile names and the total message count.
- `+ник АВЕСОМ` sets your display nickname for this chat; reply to a participant with `+ник Новый ник` to rename them as an administrator. `-ник` removes your own nickname; reply with `-ник` to clear another member's nickname as an administrator. Statistics show the display nickname/name as a clickable profile link, not the `@username`.
- The supplied 14 Oct 2026 Iris snapshot is used as the all-time baseline only in the matching XVII chat; Pear CM counts observed after the snapshot are added on top, with known counters from the same moment offset to avoid double-counting. Period statistics still show messages actually tracked for each period.
- `стата вся`, `стата неделя`, etc. are aliases; `топ 30 месяц` selects the number of rows (1–100).
- `кто админ` groups Pear CM ranks into star-headed sections and links each name to the Telegram profile.
- The fast period-statistics path uses `bot_activity_daily`; run the updated `schema.sql` in Supabase to enable atomic daily aggregation. If the table is not present, the bot falls back to the raw activity-event log.
- Message counts begin when Pear CM observes messages in the chat. Telegram Bot API does not provide a method to fetch a group's complete historical message archive, so older messages from before the bot was added cannot be reconstructed automatically.


## GDE.WTF moderator API integration

Pear CM can operate your external GDE.WTF moderator API from Telegram. This is an independent integration with `https://map.gde.wtf/api/ext`; it does not access or modify Pear CM's own moderation database.

Set the following environment variables in the persistent host (for example Relax Dev):
- `GDEWTF_API_KEY` — the external API key, stored as a secret; never commit it to Git.
- `BOT_OWNER_ID` — the existing Pear CM owner Telegram ID. Only this account can execute the integration commands.
- Optional `GDEWTF_API_BASE` — API base URL; defaults to `https://map.gde.wtf/api/ext`.

Commands (owner-only; use `q` as pub code, `@username`, or Telegram ID):
- `!ping`, `!профиль @username`, `!модераторы [city_id]`, `!города`, `!журнал [q] [limit]`
- `!варн q причина`, `!снятьварны q [index]`, `!ник q новый_ник` (use `-` to clear)
- `!город q city_id` (use `-` to clear), `!амб q`, `!назначить q [city_id] [права]`
- `!снять q`, `!бан q [причина]`, `!разбан q`
- `!help` — command reference.

The API key grants powerful external moderator-management access. Because the key was pasted into a chat while configuring this integration, rotate it in GDE.WTF's `auth_config.json` and restart that service, then set the replacement only as `GDEWTF_API_KEY` on the host. Do not paste the replacement into GitHub, source files, or chat.
