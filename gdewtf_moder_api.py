"""Owner-only integration with the external GDE.WTF moderator API.

Configure GDEWTF_API_KEY as a secret environment variable. Never hardcode the
key in the repository. Commands are intentionally restricted to BOT_OWNER_ID.
"""
from __future__ import annotations

import json
import logging
import os
import re
from html import escape
from typing import Any

import aiohttp
from aiogram import F, Router
from aiogram.types import Message

log = logging.getLogger("pear_cm.gdewtf")
gdewtf_router = Router(name="gdewtf_moder_api")

API_BASE = os.getenv("GDEWTF_API_BASE", "https://map.gde.wtf/api/ext").rstrip("/")
API_KEY = os.getenv("GDEWTF_API_KEY", "").strip()
try:
    OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)


async def _api(method: str, path: str, *, params: dict[str, Any] | None = None,
               body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not API_KEY:
        return {"ok": False, "error": "api_key_not_configured"}
    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=headers) as session:
            async with session.request(
                method, f"{API_BASE}/{path.lstrip('/')}",
                params=params if method == "GET" else None,
                json=body if method != "GET" else None,
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError):
                    data = {"ok": False, "error": "invalid_api_response"}
                if not isinstance(data, dict):
                    return {"ok": False, "error": "invalid_api_response"}
                if response.status >= 400:
                    data.setdefault("ok", False)
                    data.setdefault("http_status", response.status)
                return data
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("GDE.WTF API request failed (%s %s): %s", method, path, type(exc).__name__)
        return {"ok": False, "error": "api_unavailable"}


def _clean(value: Any, default: str = "—") -> str:
    if value is None or value == "":
        return default
    return escape(str(value))


def _mod_line(mod: dict[str, Any]) -> str:
    display = mod.get("nick") or mod.get("name") or mod.get("username") or mod.get("pub") or "?"
    username = f" (@{mod['username'].lstrip('@')})" if mod.get("username") else ""
    city = mod.get("city_name") or mod.get("city") or "—"
    role = mod.get("role_label") or mod.get("role") or "—"
    return (
        f"• <b>{_clean(display)}</b>{_clean(username, '')}\n"
        f"  {_clean(role)} · город: {_clean(city)} · pub: <code>{_clean(mod.get('pub'))}</code>"
    )


def _error_text(data: dict[str, Any]) -> str:
    err = str(data.get("error") or "unknown_error")
    messages = {
        "api_key_not_configured": "Не задан секрет GDEWTF_API_KEY в окружении хоста.",
        "api_unavailable": "GDE.WTF API недоступен или не ответил за 8 секунд.",
        "unauthorized": "Ключ GDE.WTF отклонён. Проверь GDEWTF_API_KEY.",
        "not_found": "Модератор не найден.",
        "not_a_moderator": "Этот аккаунт не является модератором.",
        "unknown_city": "Неизвестный город. Посмотри список командой «gde города».",
        "cannot_manage_admin": "Через API нельзя изменять аккаунт администратора.",
        "reason_required": "Для предупреждения укажи причину.",
        "bad_nick": "Ник должен быть длиной 2–20 символов: буквы, цифры, _ или -.",
        "nick_reserved": "Этот ник зарезервирован.",
        "nick_taken": "Этот ник уже занят.",
        "bad_index": "Неверный номер предупреждения.",
        "invalid_api_response": "API вернул ответ в неожиданном формате.",
    }
    return messages.get(err, f"Ошибка GDE.WTF API: <code>{_clean(err)}</code>")


def _profile(mod: dict[str, Any]) -> str:
    warns = mod.get("warns") or []
    perms = mod.get("perms") or []
    return (
        f"🌐 <b>GDE.WTF · профиль модератора</b>\n\n"
        f"{_mod_line(mod)}\n"
        f"Telegram ID: <code>{_clean(mod.get('tg_id'))}</code>\n"
        f"Роль: {_clean(mod.get('role_label') or mod.get('role'))}\n"
        f"Город: {_clean(mod.get('city_name') or mod.get('city'))}\n"
        f"Регион: {_clean(mod.get('region'))}\n"
        f"Рейтинг: <b>{_clean(mod.get('rating', 0))}</b> · предупреждений: <b>{_clean(mod.get('warns_count', len(warns)))}</b>\n"
        f"Онлайн: {_clean('да' if mod.get('online') else 'нет')} · смена: {_clean('да' if mod.get('on_shift') else 'нет')}\n"
        f"Бан: {_clean('да' if mod.get('banned') else 'нет')}\n"
        f"Права: {_clean(', '.join(map(str, perms)) or '—')}\n"
        f"О себе: {_clean(mod.get('about'))}"
    )


def _help() -> str:
    return (
        "🌐 <b>GDE.WTF · управление модераторами</b>\n\n"
        "<code>gde ping</code> — проверить связь\n"
        "<code>gde профиль @username|ID|RND-AB3K</code> — профиль\n"
        "<code>gde модераторы [city_id]</code> — список команды\n"
        "<code>gde города</code> — список городов\n"
        "<code>gde журнал [q] [limit]</code> — журнал действий\n"
        "<code>gde варн q причина</code> — выдать предупреждение\n"
        "<code>gde снятьварны q [index]</code> — снять одно предупреждение (index с 0) или все\n"
        "<code>gde ник q новый_ник</code> / <code>gde ник q -</code> — назначить/снять ник\n"
        "<code>gde город q city_id</code> — назначить/сменить город; <code>-</code> — убрать\n"
        "<code>gde амб q</code> — назначить амбассадором города\n"
        "<code>gde назначить q [город] [права]</code> — назначить/обновить модератора\n"
        "<code>gde снять q</code> — снять с модерации\n"
        "<code>gde бан q [причина]</code> / <code>gde разбан q</code> — бан/разбан\n\n"
        "В качестве q указывай pub-код, @username или Telegram ID. Только владелец Pear CM может пользоваться интеграцией."
    )


@gdewtf_router.message(F.text.regexp(r"(?i)^\s*[/.!]?(?:gde|gdewtf)(?:\s|$)"))
async def gdewtf_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != OWNER_ID or OWNER_ID <= 0:
        await message.answer("⛔ Эта интеграция доступна только владельцу Pear CM.")
        return
    text = (message.text or "").strip()
    parts = text.split(maxsplit=3)
    if len(parts) == 1 or parts[1].lower() in {"help", "помощь", "команды"}:
        await message.answer(_help())
        return

    action = parts[1].lower()
    rest = parts[2:] if len(parts) > 2 else []
    data: dict[str, Any]
    try:
        if action in {"ping", "пинг"}:
            data = await _api("GET", "/ping")
            if data.get("ok"):
                await message.answer(
                    "✅ <b>GDE.WTF API доступен</b>\n"
                    f"Сервис: {_clean(data.get('service'))} · v{_clean(data.get('v'))}\n"
                    f"Модераторов: {_clean(data.get('mods'))} · городов: {_clean(data.get('cities'))}"
                )
            else:
                await message.answer("⚠️ " + _error_text(data))
            return

        if action in {"профиль", "who", "mod"}:
            if not rest:
                await message.answer("Укажи модератора: <code>gde профиль @username</code>")
                return
            data = await _api("GET", "/mod", params={"q": rest[0]})
            if data.get("ok") and isinstance(data.get("mod"), dict):
                await message.answer(_profile(data["mod"]))
            else:
                await message.answer("⚠️ " + _error_text(data))
            return

        if action in {"модераторы", "mods"}:
            params = {"city": rest[0]} if rest else None
            data = await _api("GET", "/mods", params=params)
            if not data.get("ok"):
                await message.answer("⚠️ " + _error_text(data))
                return
            mods = data.get("mods") or []
            body = "\n\n".join(_mod_line(m) for m in mods[:60] if isinstance(m, dict))
            await message.answer(f"🌐 <b>Модераторы GDE.WTF · {len(mods)}</b>\n\n{body or 'Список пуст.'}"[:3900])
            return

        if action in {"города", "cities"}:
            data = await _api("GET", "/cities")
            if not data.get("ok"):
                await message.answer("⚠️ " + _error_text(data))
                return
            rows = []
            for city in (data.get("cities") or [])[:100]:
                if isinstance(city, dict):
                    rows.append(f"• <b>{_clean(city.get('name'))}</b> — <code>{_clean(city.get('id'))}</code> · регион: {_clean(city.get('region'))} · команда: {_clean(city.get('team', 0))}")
            await message.answer(f"🌐 <b>Города GDE.WTF · {len(data.get('cities') or [])}</b>\n\n" + ("\n".join(rows) or "Список пуст.")[:3700])
            return

        if action in {"журнал", "лог", "log"}:
            params: dict[str, Any] = {"limit": 50}
            if rest:
                if rest[-1].isdigit():
                    params["limit"] = max(1, min(200, int(rest[-1])))
                    if len(rest) > 1:
                        params["q"] = rest[0]
                else:
                    params["q"] = rest[0]
            data = await _api("GET", "/log", params=params)
            if not data.get("ok"):
                await message.answer("⚠️ " + _error_text(data))
                return
            lines = []
            for item in (data.get("log") or [])[:40]:
                if isinstance(item, dict):
                    lines.append(f"• {_clean(item.get('act'))} · {_clean(item.get('who'))} → {_clean(item.get('target'))}\n  {_clean(item.get('detail') or item.get('reason'))}")
            await message.answer(f"📜 <b>Журнал GDE.WTF · {data.get('count', len(lines))}</b>\n\n" + ("\n".join(lines) or "Записей нет.")[:3700])
            return

        if action in {"варн", "warn"}:
            if len(rest) < 2:
                await message.answer("Формат: <code>gde варн @username причина</code>")
                return
            q, reason = rest[0], " ".join(rest[1:])
            data = await _api("POST", "/warn", body={"q": q, "reason": reason, "by": f"Pear CM / {message.from_user.id}"})
        elif action in {"снятьварны", "снять-варны", "warnclear"}:
            if not rest:
                await message.answer("Формат: <code>gde снятьварны @username [index]</code>")
                return
            body = {"q": rest[0]}
            if len(rest) > 1:
                try:
                    body["index"] = int(rest[1])
                except ValueError:
                    await message.answer("index должен быть целым числом от 0.")
                    return
            data = await _api("POST", "/warn/clear", body=body)
        elif action in {"ник", "nick"}:
            if len(rest) < 2:
                await message.answer("Формат: <code>gde ник @username новый_ник</code> или <code>gde ник @username -</code>")
                return
            data = await _api("POST", "/nick", body={"q": rest[0], "nick": "" if rest[1] == "-" else " ".join(rest[1:])})
        elif action in {"город", "city"}:
            if len(rest) < 2:
                await message.answer("Формат: <code>gde город @username city_id</code> (для удаления укажи -)")
                return
            data = await _api("POST", "/city", body={"q": rest[0], "city": "" if rest[1] == "-" else rest[1]})
        elif action in {"амб", "amb"}:
            if not rest:
                await message.answer("Формат: <code>gde амб @username</code>")
                return
            data = await _api("POST", "/mod/set", body={"q": rest[0], "amb": True})
        elif action in {"назначить", "modset"}:
            if not rest:
                await message.answer("Формат: <code>gde назначить @username [city_id] [права]</code>")
                return
            body = {"q": rest[0]}
            if len(rest) > 1 and rest[1] != "-":
                body["city"] = rest[1]
            if len(rest) > 2:
                body["perms"] = rest[2] if rest[2].lower() in {"all", "full", "*", "лента", "старший"} else [p for p in re.split(r"[,; ]+", rest[2]) if p]
            data = await _api("POST", "/mod/set", body=body)
        elif action in {"снять", "modremove"}:
            if not rest:
                await message.answer("Формат: <code>gde снять @username</code>")
                return
            data = await _api("POST", "/mod/remove", body={"q": rest[0]})
        elif action in {"бан", "ban"}:
            if not rest:
                await message.answer("Формат: <code>gde бан @username [причина]</code>")
                return
            body = {"q": rest[0], "ban": True}
            if len(rest) > 1:
                body["reason"] = " ".join(rest[1:])
            data = await _api("POST", "/ban", body=body)
        elif action in {"разбан", "unban"}:
            if not rest:
                await message.answer("Формат: <code>gde разбан @username</code>")
                return
            data = await _api("POST", "/ban", body={"q": rest[0], "ban": False})
        else:
            await message.answer(_help())
            return

        if data.get("ok"):
            mod = data.get("mod") if isinstance(data.get("mod"), dict) else {}
            pub = data.get("pub") or mod.get("pub") or "—"
            if action in {"варн", "warn"}:
                result = f"✅ Предупреждение выдано · <code>{_clean(pub)}</code> · всего: <b>{_clean(data.get('warns_count', 0))}</b>"
            elif action in {"снятьварны", "снять-варны", "warnclear"}:
                result = f"✅ Предупреждения обновлены · <code>{_clean(pub)}</code> · осталось: <b>{_clean(data.get('warns_count', 0))}</b>"
            elif action in {"ник", "nick"}:
                result = f"✅ Ник обновлён: <b>{_clean(data.get('nick'))}</b> · <code>{_clean(pub)}</code>"
            elif action in {"город", "city", "амб", "amb", "назначить", "modset"}:
                result = "✅ Изменения сохранены.\n" + (_profile(mod) if mod else f"Модератор: <code>{_clean(pub)}</code>")
            elif action in {"снять", "modremove"}:
                result = f"✅ Модератор снят с должности · <code>{_clean(pub)}</code>"
            else:
                result = f"✅ Статус бана обновлён · <code>{_clean(pub)}</code> · banned: <b>{_clean(data.get('banned'))}</b>"
            await message.answer(result)
        else:
            await message.answer("⚠️ " + _error_text(data))
    except Exception:
        log.exception("Unhandled GDE.WTF command failure")
        await message.answer("⚠️ Не удалось выполнить команду GDE.WTF. Подробности записаны в лог.")
