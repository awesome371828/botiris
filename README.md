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
