"""Telegram owner review gate for Nightmare Eater."""

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters


ROOT = Path(__file__).resolve().parent
load_dotenv(Path(os.environ.get("NE_BOT_ENV", r"C:\Claude Code\content_bot\.env")))
DATA = Path(os.environ.get("NE_BOT_DATA", ROOT / "data")).resolve()
REPO = "Statusq8/Nightmare-Eater"
MAX_SOURCE = 250_000


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def github_token():
    token = os.getenv("NE_GITHUB_TOKEN") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        return token
    result = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                            text=True, capture_output=True, timeout=8,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line.partition("=")[2]
    raise RuntimeError("Для сохранения главы нужен доступ GitHub к репозиторию Nightmare-Eater.")


def save_chapter(number, source):
    path = f"chapters/{number:03}.md"
    endpoint = f"https://api.github.com/repos/{REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {github_token()}", "Accept": "application/vnd.github+json"}

    def existing():
        try:
            with urlopen(Request(endpoint + "?ref=main", headers=headers), timeout=20) as response:
                return json.load(response)
        except HTTPError as e:
            if e.code == 404:
                return None
            raise

    def identical(current):
        if current and base64.b64decode(current["content"]) == source:
            return current["sha"]
        if current:
            raise ValueError(f"Глава {number} уже в GitHub и отличается. Исходник защищён.")

    current = existing()
    if current:
        return identical(current)
    payload = {"branch": "main", "message": f"Save submitted Nightmare Eater chapter {number:03}",
               "content": base64.b64encode(source).decode("ascii")}
    try:
        request = Request(endpoint, json.dumps(payload).encode(), headers, method="PUT")
        with urlopen(request, timeout=30) as response:
            created = json.load(response)["content"]["sha"]
    except HTTPError as e:
        if e.code not in (409, 422):
            raise
        current = existing()
        if current:
            return identical(current)
        raise
    if identical(existing()) != created:
        raise RuntimeError("GitHub не подтвердил сохранение главы.")
    return created


def jobs():
    return DATA / "jobs"


def state_path(key):
    return jobs() / key / "state.json"


def queue(key, chapter_sha, scene, phase, revision, comment="", approved=None):
    root = state_path(key).parent
    request = {"job": key, "chapter_blob": chapter_sha, "scene": scene, "phase": phase,
               "revision": revision, "owner_comment": comment, "approved_reference": approved}
    request["request_id"] = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    state = {**request, "status": "queued"}
    atomic_json(root / "request.json", request)
    atomic_json(root / "state.json", state)


def artifact(root, name, allowed):
    if not isinstance(name, str):
        raise ValueError("Путь к результату отсутствует.")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix.lower() not in allowed or not path.is_file():
        raise ValueError("Материал отсутствует или имеет неверный формат.")
    if path.stat().st_size == 0:
        raise ValueError("Материал пуст.")
    return path


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_result(state, result, root):
    if result.get("request_id") != state["request_id"]:
        return []
    phase = state["phase"]
    allowed = {".png", ".jpg", ".jpeg", ".webp"} if phase == "concept" else {".mp4"}
    names = result.get("files")
    if not isinstance(names, list) or not (1 <= len(names) <= (10 if phase == "concept" else 1)):
        raise ValueError("Неверное число материалов для этапа.")
    files = [artifact(root, name, allowed) for name in names]
    if phase == "concept":
        for file in files:
            with Image.open(file) as image:
                image.verify()
    else:
        if files[0].stat().st_size > 50_000_000:
            raise ValueError("Telegram принимает видео до 50 МБ через текущее подключение.")
        metadata = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(files[0])], timeout=20)
        streams = json.loads(metadata)["streams"]
        language = lambda kind: {s.get("tags", {}).get("language") for s in streams if s["codec_type"] == kind}
        if not language("video") or any(not {"eng", "rus"} <= language(kind) for kind in ("audio", "subtitle")):
            raise ValueError("В сцене нужны видео, озвучка EN/RU и субтитры EN/RU.")
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(files[0]), "-map", "0:v", "-map", "0:a",
                        "-f", "null", os.devnull], check=True, timeout=180)
    return files


async def main_menu(update, context):
    await update.message.reply_text("Студия «Пожиратель кошмаров»", reply_markup=ReplyKeyboardMarkup(
        [["Глава", "Статус"]], resize_keyboard=True))


async def receive(update, context):
    message = update.message
    if message.text == "Глава":
        context.user_data.clear()
        context.user_data["step"] = "chapter_number"
        await message.reply_text("Пришли номер главы.")
        return
    if message.text == "Статус":
        states = [load_json(p) for p in jobs().glob("*/state.json")]
        status = {"queued": "в очереди", "review": "на утверждении", "comment": "ждёт комментария",
                  "approved": "утверждена", "blocked": "заблокирована"}
        await message.reply_text("\n".join(f"{s['job']} · {status[s['status']]}" for s in states[-25:]) or "Сцен в очереди пока нет.")
        return
    step = context.user_data.get("step")
    if step == "chapter_number":
        if not message.text or not re.fullmatch(r"[0-9]{1,3}", message.text.strip()) or not int(message.text):
            await message.reply_text("Нужен номер от 1 до 999.")
            return
        context.user_data.update(step="chapter_text", number=int(message.text))
        await message.reply_text("Пришли текст одним сообщением или прикрепи полный файл .md или .txt в UTF-8.")
        return
    if step == "chapter_text":
        number = context.user_data["number"]
        if message.document:
            doc = message.document
            if (doc.file_size or 0) > MAX_SOURCE or Path(doc.file_name or "").suffix.lower() not in {".md", ".txt"}:
                await message.reply_text("Подойдёт файл .md или .txt до 250 КБ.")
                return
            source = bytes(await (await doc.get_file()).download_as_bytearray())
        elif message.text:
            source = message.text.encode("utf-8")
        else:
            return
        if len(source) > MAX_SOURCE or not source.strip():
            await message.reply_text("Пришли непустой текст главы до 250 КБ.")
            return
        try:
            source.decode("utf-8-sig")
            chapter_sha = await asyncio.to_thread(save_chapter, number, source)
        except UnicodeDecodeError:
            await message.reply_text("Файл должен быть сохранён в UTF-8.")
            return
        except (ValueError, RuntimeError) as e:
            await message.reply_text(str(e))
            return
        except (OSError, subprocess.SubprocessError):
            await message.reply_text("Не удалось подтвердить запись на GitHub. Повтори отправку текста позже.")
            return
        chapter = DATA / "chapters" / f"{number:03}"
        chapter.mkdir(parents=True, exist_ok=True)
        (chapter / "source.md").write_bytes(source)
        atomic_json(chapter / "request.json", {"chapter": number, "chapter_blob": chapter_sha, "source": "source.md"})
        context.user_data.clear()
        await message.reply_text(f"Глава {number} сохранена в GitHub. Запрос на разбивку добавлен в очередь.")
        return
    if step == "comment" and message.text:
        key = context.user_data["job"]
        state = load_json(state_path(key))
        if state["status"] == "comment" and state["revision"] == context.user_data["revision"]:
            queue(key, state["chapter_blob"], state["scene"], state["phase"], state["revision"] + 1,
                  message.text, state["approved_reference"])
            await message.reply_text("Комментарий сохранён. Новый дубль поставлен в очередь.")
        context.user_data.clear()


def buttons(state):
    identifier = f"{state['job']}:{state['revision']}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("Утвердить", callback_data=f"ok:{identifier}"),
                                  InlineKeyboardButton("Переделать", callback_data=f"redo:{identifier}")]])


async def decide(update, context):
    query = update.callback_query
    await query.answer()
    action, key, revision = query.data.split(":")
    path = state_path(key)
    if not path.exists():
        return
    state = load_json(path)
    if state["status"] == "comment" and action == "redo" and state["revision"] == int(revision):
        context.user_data.update(step="comment", job=key, revision=int(revision))
        await query.message.reply_text("Пришли одним сообщением, что исправить в сцене.")
        return
    if state["status"] not in {"review", "blocked"} or state["revision"] != int(revision) or (state["status"] == "blocked" and action != "redo"):
        await query.message.reply_text("Это решение уже обработано или появился новый дубль.")
        return
    if action == "redo":
        if state["status"] == "blocked":
            queue(key, state["chapter_blob"], state["scene"], state["phase"], state["revision"] + 1,
                  state["owner_comment"], state["approved_reference"])
            await query.message.reply_text("Поставил повторную попытку в очередь.")
        elif state["phase"] == "concept":
            queue(key, state["chapter_blob"], state["scene"], "concept", state["revision"] + 1)
            await query.message.reply_text("Референсы поставлены на перегенерацию.")
        else:
            state["status"] = "comment"
            atomic_json(path, state)
            context.user_data.update(step="comment", job=key, revision=int(revision))
            await query.message.reply_text("Пришли одним сообщением, что исправить в сцене.")
            return
    elif state["phase"] == "concept":
        for item in state["review_files"]:
            if digest(artifact(path.parent, item["file"], {".png", ".jpg", ".jpeg", ".webp"})) != item["sha256"]:
                await query.message.reply_text("Материалы изменились после отправки. Нужно повторить просмотр.")
                return
        queue(key, state["chapter_blob"], state["scene"], "scene", state["revision"] + 1,
              approved={"request_id": state["request_id"], "files": state["review_files"]})
        await query.message.reply_text("Референсы утверждены. Сцена поставлена в очередь производства.")
    else:
        state["status"] = "approved"
        atomic_json(path, state)
        await query.message.reply_text("Сцена утверждена.")
    await query.edit_message_reply_markup(reply_markup=None)


async def deliver(context):
    for request_path in (DATA / "chapters").glob("*/request.json"):
        plan = request_path.with_name("plan.json")
        if not plan.exists():
            continue
        request, result = load_json(request_path), load_json(plan)
        if request["chapter_blob"] != result.get("chapter_blob"):
            continue
        scenes = result.get("scenes", [])
        if not 1 <= len(scenes) <= 999 or any(not isinstance(s, dict) for s in scenes):
            continue
        for i, scene in enumerate(scenes, 1):
            key = f"{request['chapter']:03}-{i:03}"
            if not state_path(key).exists():
                queue(key, request["chapter_blob"], scene, "concept", 1)
    for path in jobs().glob("*/state.json"):
        state = load_json(path)
        if state["status"] != "queued":
            continue
        result_path = path.with_name(f"result-{state['request_id']}.json")
        if not result_path.exists():
            continue
        try:
            result = load_json(result_path)
            if result.get("request_id") != state["request_id"]:
                continue
            if result.get("blocked"):
                raise ValueError(str(result["blocked"])[:600])
            files = await asyncio.to_thread(validate_result, state, result, path.parent)
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            state["status"] = "blocked"
            atomic_json(path, state)
            retry = InlineKeyboardMarkup([[InlineKeyboardButton("Повторить", callback_data=f"redo:{state['job']}:{state['revision']}")]])
            await context.bot.send_message(OWNER, f"Сцена {state['job']}: {e}", reply_markup=retry)
            continue
        for file in files:
            with file.open("rb") as data:
                if state["phase"] == "concept":
                    await context.bot.send_document(OWNER, data, filename=file.name)
                else:
                    await context.bot.send_video(OWNER, data, filename=file.name, read_timeout=120, write_timeout=120)
        state["review_files"] = [{"file": f.relative_to(path.parent).as_posix(), "sha256": digest(f)} for f in files]
        state["status"] = "review"
        atomic_json(path, state)
        label = "Референсы" if state["phase"] == "concept" else "Готовая сцена"
        await context.bot.send_message(OWNER, f"{label} · {state['job']} · дубль {state['revision']}", reply_markup=buttons(state))


OWNER = int(os.getenv("TELEGRAM_OWNER_ID", "0") or "0")


async def plan_chapters(context):
    from studio_worker import run

    await asyncio.to_thread(run)


def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token or OWNER <= 0:
        sys.exit("Нужна действующая конфигурация Telegram и ID владельца.")
    app = Application.builder().token(token).build()
    owner = filters.User(user_id=OWNER) & filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", main_menu, filters=owner))
    app.add_handler(MessageHandler(owner & ((filters.TEXT & ~filters.COMMAND) | filters.Document.ALL), receive))

    async def owner_decide(update, context):
        if update.effective_user.id == OWNER and update.effective_chat.type == "private":
            await decide(update, context)
        else:
            await update.callback_query.answer()

    app.add_handler(CallbackQueryHandler(owner_decide, pattern=r"^(ok|redo):[0-9]{3}-[0-9]{3}:[0-9]+$"))
    if os.getenv("NE_PROJECT"):
        app.job_queue.run_repeating(plan_chapters, interval=8, first=2)
    app.job_queue.run_repeating(deliver, interval=5, first=1)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
