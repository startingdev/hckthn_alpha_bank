
import os
import json
import time
import uuid
import html
from typing import Tuple, Any, Dict, List, Optional

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from groq import Groq

# === НАСТРОЙКИ ===

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8585273586:AAEJz8bjzrXOM6OusKuW7xTYflsTYK5BFow")
# HTML по умолчанию, чтобы везде работало форматирование
bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")  # TODO: подставь свой реальный ключ
MODEL_NAME = "llama-3.1-8b-instant"

client = Groq(api_key=GROQ_API_KEY)

DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "3"))

# Баннеры (file_id PNG из Telegram)
BANNER_WELCOME_ID = os.getenv("BANNER_WELCOME_ID", "AgACAgIAAxkBAAPdaRouNS26y2b8S9nt1K6ItTmiCLgAAuURaxtq0dFI5attTAw2YqABAAMCAAN5AAM2BA")   # привет, выбор бизнеса, ошибки по бизнесу
BANNER_FAQ_ID = os.getenv("BANNER_FAQ_ID", "AgACAgIAAxkBAAIBX2kakpgPBUVy_H_wy8XhZ6vTFL11AAJiD2sbgC3QSEk6pQ9Xrh_MAQADAgADeQADNgQ")           # список FAQ, навигация по вопросам
BANNER_ANSWER_ID = os.getenv("BANNER_ANSWER_ID", "AgACAgIAAxkBAAIBYWkakqu9MiOGp-kuSVf15XpMla3fAAJmD2sbgC3QSMJLQUvlJNJFAQADAgADeQADNgQ")     # свой вопрос, ответы, ошибки по вопросам


# === ВСПОМОГАТЕЛЬНОЕ: ЧИСТКА СТАРЫХ ЛОГОВ ===

def cleanup_old_logs() -> None:
    """
    Удаляем JSON-лог-файлы старше LOG_RETENTION_DAYS.
    """
    now = time.time()
    cutoff = now - LOG_RETENTION_DAYS * 86400

    try:
        for fname in os.listdir(DATA_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(DATA_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ts = data.get("timestamp")
                if not isinstance(ts, (int, float)):
                    raise ValueError("no ts")
            except Exception:
                ts = os.path.getmtime(path)

            if ts < cutoff:
                os.remove(path)
    except FileNotFoundError:
        pass


# === ЗАПИСЬ ПАКЕТОВ / ЛОГИ ДЛЯ docker_worker ===

def save_packet(packet: Dict[str, Any]) -> str:
    """
    Пишем JSON в data/*.json — это будет забирать docker_worker.
    Формат:
    {
      "packet_id": "...",
      "timestamp": 1234567890,
      "type": "...",
      "event": "...",
      ... payload ...
    }
    Параллельно пишем одну json-строку в stdout.
    """
    if "packet_id" not in packet:
        packet["packet_id"] = str(uuid.uuid4())
    if "timestamp" not in packet:
        packet["timestamp"] = int(time.time())
    if "type" not in packet:
        packet["type"] = "event"
    if "event" not in packet:
        packet["event"] = packet["type"]

    filename = os.path.join(DATA_DIR, f"{packet['packet_id']}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=4)

    try:
        line = json.dumps({"log_type": "packet", **packet}, ensure_ascii=False)
        print(line, flush=True)
    except Exception:
        pass

    cleanup_old_logs()
    return filename


# === СЕССИИ В ПАМЯТИ ===
# chat_id -> session

sessions: Dict[int, Dict[str, Any]] = {}


def get_session(chat_id: int) -> Dict[str, Any]:
    """
    Структура session:
    {
        "stage": "waiting_business" | "choose_question" | "custom_question",
        "business": str | None,          # текущий бизнес
        "saved_business": str | None,    # последний бизнес юзера
        "faqs": list[{"q","a"}],
        "faq_page": int,
        "faq_page_size": int,
        "history": list[{"q","a"}],
        "last_message_id": int | None,   # последний «экран» (сообщение бота)
        "last_banner_id": str | None,    # какой баннер был на последнем экране
        "first_start_seen": bool        # был ли уже хотя бы один /start от пользователя
    }
    """
    if chat_id not in sessions:
        sessions[chat_id] = {
            "stage": None,
            "business": None,
            "saved_business": None,
            "faqs": [],
            "faq_page": 0,
            "faq_page_size": 3,
            "history": [],
            "last_message_id": None,
            "last_banner_id": None,
            "first_start_seen": False,
        }
    return sessions[chat_id]


# === УНИВЕРСАЛЬНАЯ ОТРИСОВКА «СТРАНИЦЫ» ===

def send_screen(
    chat_id: int,
    session: Dict[str, Any],
    text: str,
    banner_id: Optional[str] = None,
    inline_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """
    Универсальная функция для «страницы»:
    - по возможности редактирует прошлое сообщение бота,
    - если редактировать нельзя — удаляет и отправляет новое,
    - на каждом экране есть баннер (PNG) и подпись text.
    """
    last_message_id = session.get("last_message_id")
    last_banner_id = session.get("last_banner_id")

    # Попытка аккуратно отредактировать прошлый экран
    if last_message_id:
        try:
            # если и раньше был баннер, и тот же самый — меняем только caption/кнопки
            if last_banner_id and banner_id and last_banner_id == banner_id:
                bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=last_message_id,
                    caption=text,
                    reply_markup=inline_markup,
                )
                return
            # если и раньше был текст без баннера, и сейчас без баннера — редактируем текст
            if not last_banner_id and not banner_id:
                bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=last_message_id,
                    reply_markup=inline_markup,
                )
                return
        except Exception:
            # редактирование не удалось — попробуем удалить
            try:
                bot.delete_message(chat_id, last_message_id)
            except Exception:
                pass

    # Если редактирование не сработало или баннер поменялся — отправляем новый экран
    if banner_id:
        msg = bot.send_photo(
            chat_id,
            banner_id,
            caption=text,
            reply_markup=inline_markup,
        )
    else:
        msg = bot.send_message(
            chat_id,
            text,
            reply_markup=inline_markup,
        )

    session["last_message_id"] = msg.message_id
    session["last_banner_id"] = banner_id


# === LLM: ГЕНЕРАЦИЯ FAQ ===

def generate_faqs(business_description: str, n: int = 9) -> List[Dict[str, str]]:
    """
    Генерация списка FAQ: [{q, a}, ...]
    """
    system_prompt = (
        "Ты помощник для владельцев очень маленького бизнеса (микробизнес).\n"
        "По описанию бизнеса придумай список типовых вопросов и коротких, "
        "практичных ответов.\n"
        "Ответ верни строго в формате JSON:\n"
        "{\"faqs\":[{\"q\":\"Вопрос 1\",\"a\":\"Ответ 1\"}, ...]}"
    )
    user_prompt = (
        f"Сфера бизнеса: {business_description}\n"
        f"Сделай {n} самых частых вопросов владельца к такому помощнику."
    )

    completion = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=1024,
    )

    text = completion.choices[0].message.content.strip()

    try:
        data = json.loads(text)
        faqs = data.get("faqs", [])
        clean: List[Dict[str, str]] = []
        for item in faqs:
            q = str(item.get("q") or "").strip()
            a = str(item.get("a") or "").strip()
            if q and a:
                clean.append({"q": q, "a": a})
        clean = clean[:n] if clean else []
        return clean
    except Exception:
        return [{
            "q": "Как мне запустить и развивать этот бизнес?",
            "a": text,
        }]


# === LLM: ФИЛЬТР ВОПРОСОВ ===

def classify_question(question: str, business: Optional[str]) -> str:
    """
    Возвращает:
      - "OK"           — вопрос про бизнес и законный
      - "NOT_BUSINESS" — вопрос не относится к бизнесу
      - "ILLEGAL"      — вопрос про незаконные действия
    """
    business_part = (
        f"Описание бизнеса пользователя: {business}."
        if business else
        "Описание бизнеса пользователя не задано."
    )

    system_prompt = (
        "Ты фильтр вопросов для Telegram-бота-помощника по микробизнесу. "
        "Твоя задача - решить, может ли бот отвечать на вопрос."
    )

    user_prompt = (
        f"{business_part}\n\n"
        f"Вопрос пользователя: {question}\n\n"
        "Если вопрос относится к запуску, развитию, управлению, маркетингу, "
        "продажам, финансам, налогам, юридическим вопросам, персоналу, рискам, "
        "автоматизации и т.п. для бизнеса пользователя (даже косвенно) и при этом "
        "не содержит просьб о нарушении закона — ответь: OK.\n"
        "Если вопрос вообще не про бизнес (личные отношения, игры, развлечения, "
        "учёба, здоровье, политика и т.п.) — ответь: NOT_BUSINESS.\n"
        "Если вопрос содержит просьбу о чём-то незаконном (мошенничество, обход "
        "налогов, наркотики, оружие, взлом, насилие и т.п.) — ответь: ILLEGAL.\n"
        "Ответь строго ОДНИМ словом: OK, NOT_BUSINESS или ILLEGAL."
    )

    completion = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=8,
    )

    label_raw = completion.choices[0].message.content.strip()
    label = label_raw.upper().split()[0]

    if label not in ("OK", "NOT_BUSINESS", "ILLEGAL"):
        label = "NOT_BUSINESS"

    return label


def check_question_allowed(question: str, session: Dict[str, Any]) -> Tuple[bool, str]:
    business = session.get("business") or session.get("saved_business")
    label = classify_question(question, business)
    if label == "OK":
        return True, label
    return False, label


# === LLM: ФИЛЬТР ОПИСАНИЯ БИЗНЕСА ===

def classify_business(business: str) -> str:
    """
    Возвращает:
      - "OK"           — похоже на легальный бизнес / деятельность
      - "NOT_BUSINESS" — вообще не описание бизнеса
      - "ILLEGAL"      — заведомо незаконная деятельность
    """
    system_prompt = (
        "Ты фильтр описаний бизнеса для Telegram-бота-помощника по микробизнесу. "
        "Определи, является ли текст описанием бизнеса и не содержит ли он "
        "незаконной деятельности."
    )
    user_prompt = (
        f"Текст пользователя: {business}\n\n"
        "Если это похоже на описание бизнеса, вида деятельности, услуги или "
        "проекта, на котором человек может зарабатывать, и это не выглядит "
        "как заведомо незаконная деятельность — ответь: OK.\n"
        "Если это не описание бизнеса (шутка, бессвязный текст, личная жизнь, "
        "учёба, хобби без намёка на монетизацию и т.п.) — ответь: NOT_BUSINESS.\n"
        "Если это описание заведомо незаконной деятельности (мошенничество, "
        "наркотики, оружие, взлом, насилие и т.п.) — ответь: ILLEGAL.\n"
        "Ответь строго ОДНИМ словом: OK, NOT_BUSINESS или ILLEGAL."
    )

    completion = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=8,
    )

    label_raw = completion.choices[0].message.content.strip()
    label = label_raw.upper().split()[0]

    if label not in ("OK", "NOT_BUSINESS", "ILLEGAL"):
        label = "NOT_BUSINESS"

    return label


def check_business_allowed(business: str) -> Tuple[bool, str]:
    label = classify_business(business)
    if label == "OK":
        return True, label
    return False, label


# === LLM: ОТВЕТ НА ВОПРОС С УЧЁТОМ ИСТОРИИ ===

def ask_llm(session: Dict[str, Any], question: str) -> str:
    business = session.get("business") or session.get("saved_business") or "микробизнес"
    history = session.get("history") or []

    system_prompt = (
        "Ты Copilot-помощник для микробизнеса. "
        "Отвечай по делу, структурно и коротко, "
        "с конкретными шагами. Не уходи в воду."
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"Описание бизнеса: {business}. "
                       f"Ты помогаешь владельцу принимать решения.",
        },
    ]

    for pair in history[-3:]:
        messages.append({"role": "user", "content": f"Раньше владелец спрашивал: {pair['q']}"})
        messages.append({"role": "assistant", "content": f"Ты отвечал так: {pair['a']}"})

    messages.append({
        "role": "user",
        "content": f"Новый вопрос владельца: {question}\n"
                   f"Дай чёткий, практический ответ.",
    })

    completion = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.3,
        max_tokens=1024,
    )

    return completion.choices[0].message.content.strip()


# === ФОРМАТИРОВАНИЕ ОТВЕТОВ ПОД TELEGRAM ===

def _humanize_json_for_telegram(data: Any) -> str:
    """
    Переводим JSON в более удобочитаемый текст.
    """
    if isinstance(data, dict) and isinstance(data.get("faqs"), list):
        lines: List[str] = ["Вот что я для тебя собрал:\n"]
        for idx, item in enumerate(data["faqs"], start=1):
            if not isinstance(item, dict):
                continue
            q = str(item.get("q", "")).strip()
            a = str(item.get("a", "")).strip()
            if not q and not a:
                continue
            lines.append(f"{idx}. {q}")
            if a:
                lines.append(f"   → {a}")
        return "\n".join(lines) if len(lines) > 1 else json.dumps(data, ensure_ascii=False, indent=2)

    if isinstance(data, dict):
        lines = ["Я получил структурированный ответ:\n"]
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                v_str = json.dumps(v, ensure_ascii=False)
            else:
                v_str = str(v)
            lines.append(f"• {k}: {v_str}")
        return "\n".join(lines)

    if isinstance(data, list):
        lines = ["Я получил список:\n"]
        for i, item in enumerate(data, start=1):
            lines.append(f"{i}. {item}")
        return "\n".join(lines)

    return json.dumps(data, ensure_ascii=False, indent=2)


def format_answer_for_telegram(text: str) -> str:
    """
    Если модель вернула JSON/код вместо текста – красиво развернём.
    """
    raw = (text or "").strip()

    # цельный JSON
    try:
        data = json.loads(raw)
        return _humanize_json_for_telegram(data)
    except Exception:
        pass

    # JSON внутри ```json ... ```
    if "```" in raw:
        parts = raw.split("```")
        for i in range(len(parts) - 1):
            if "json" in parts[i].lower():
                candidate = parts[i + 1].strip()
                try:
                    data = json.loads(candidate)
                    return _humanize_json_for_telegram(data)
                except Exception:
                    continue

    return text


# === ТЕКСТЫ ДЛЯ ЭКРАНОВ ===

def get_welcome_text(saved_business: Optional[str]) -> str:
    if saved_business:
        safe_business = html.escape(saved_business)
        return (
            "<b>Привет! Я Copilot для микробизнеса 👋</b>\n\n"
            "Раньше мы уже работали с этим направлением:\n"
            f"• <b>{safe_business}</b>\n\n"
            "Сейчас покажу типовые вопросы по нему.\n"
            "Если хочешь сменить направление — нажми кнопку "
            "<b>«🔁 Другой бизнес»</b>."
        )
    else:
        return (
            "<b>Привет! Я Copilot для микробизнеса 👋</b>\n\n"
            "Я помогаю владельцам и будущим владельцам маленьких дел:\n"
            "• <b>Разобраться, с чего начать</b>\n"
            "• <b>Провести первичную диагностику</b>\n"
            "• <b>Подсказать по маркетингу, деньгам и процессам</b>\n\n"
            "<b>Шаг 1.</b> Напиши, какой бизнес тебя интересует.\n"
            "<i>Например: кофейня у дома, маникюр на дому, "
            "магазин одежды в ТЦ, продажа на маркетплейсе.</i>"
        )


def get_faq_header_text(session: Dict[str, Any]) -> str:
    business = session.get("business") or session.get("saved_business") or "твой бизнес"
    safe_business = html.escape(business)
    return (
        "<b>Твои типовые вопросы по бизнесу 🔍</b>\n\n"
        f"Направление: <b>{safe_business}</b>\n\n"
        "Выбери один из вариантов ниже или задай свой вопрос."
    )


# === КЛАВИАТУРА FAQ С ПАГИНАЦИЕЙ ===

def build_faq_keyboard(session: Dict[str, Any]) -> Tuple[InlineKeyboardMarkup, int, int]:
    faqs: List[Dict[str, str]] = session.get("faqs") or []
    page = session.get("faq_page", 0)
    size = session.get("faq_page_size", 3)

    markup = InlineKeyboardMarkup(row_width=1)

    if not faqs:
        markup.add(
            InlineKeyboardButton(
                text="✏️ Своего вопроса нет в списке",
                callback_data="faq_other",
            )
        )
        markup.add(
            InlineKeyboardButton(
                text="🔁 Другой бизнес",
                callback_data="business_other",
            )
        )
        return markup, 0, 1

    total_pages = (len(faqs) + size - 1) // size
    if page < 0:
        page = 0
    if page > total_pages - 1:
        page = total_pages - 1
    session["faq_page"] = page

    start = page * size
    end = start + size

    for idx in range(start, min(end, len(faqs))):
        item = faqs[idx]
        title = item["q"]
        if len(title) > 60:
            title = title[:57] + "..."
        # каждый вопрос - отдельной строкой
        markup.add(
            InlineKeyboardButton(
                text=f"❓ {title}",
                callback_data=f"faq_{idx}",
            )
        )

    # навигация держим строго внизу
    nav_buttons = []
    if total_pages > 1 and page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="faq_prev",
            )
        )
    if total_pages > 1 and page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Вперёд ➡️",
                callback_data="faq_next",
            )
        )
    if nav_buttons:
        markup.row(*nav_buttons)

    # сервисные кнопки — отдельными строками
    markup.add(
        InlineKeyboardButton(
            text="✏️ Моего вопроса нет в списке",
            callback_data="faq_other",
        )
    )
    markup.add(
        InlineKeyboardButton(
            text="🔁 Другой бизнес",
            callback_data="business_other",
        )
    )

    return markup, page, total_pages


def add_common_nav(markup: Optional[InlineKeyboardMarkup] = None) -> InlineKeyboardMarkup:
    """
    Добавляет внизу кнопку возврата в главное меню.
    Используем на всех страницах, чтобы всегда был быстрый выход.
    """
    if markup is None:
        markup = InlineKeyboardMarkup(row_width=1)
    # Кнопка меню всегда отдельной строкой, чтобы визуально выделялась
    markup.add(
        InlineKeyboardButton(
            text="🏠 В меню",
            callback_data="go_menu",
        )
    )
    return markup


# === ПОКАЗАТЬ FAQ ПО БИЗНЕСУ ===
def present_faqs_for_business(chat_id: int, session: Dict[str, Any], reuse: bool = False) -> None:
    business = session.get("business")
    if not business:
        session["stage"] = "waiting_business"
        text = (
            "<b>Опиши бизнес, с которым будем работать.</b>\n"
            "<i>Например: кофейня у дома, маникюр на дому, маркетплейс.</i>"
        )
        send_screen(chat_id, session, text, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())
        return

    if reuse:
        time.sleep(7)
        pre_text = "<i>Продолжаем работать с этим бизнесом. Собираю типовые вопросы…</i>"
    else:
        pre_text = "<i>Принял описание бизнеса. Думаю над типовыми вопросами для такого дела…</i>"

    # Показать «промежуточную» страницу со статусом
    send_screen(chat_id, session, pre_text, banner_id=BANNER_FAQ_ID, inline_markup=add_common_nav())

    faqs = generate_faqs(business, n=9)
    session["faqs"] = faqs
    session["stage"] = "choose_question"
    session["faq_page"] = 0
    session["faq_page_size"] = 3

    save_packet({
        "type": "business_profile",
        "chat_id": chat_id,
        "business": business,
    })

    header = get_faq_header_text(session)
    markup, page, total_pages = build_faq_keyboard(session)
    footer = f"\n\n<i>Страница {page + 1} из {total_pages}</i>"
    markup = add_common_nav(markup)

    send_screen(chat_id, session, header + footer, banner_id=BANNER_FAQ_ID, inline_markup=markup)


# === ХЕНДЛЕРЫ ===

@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = message.chat.id
    session = get_session(chat_id)

    # Первый /start не удаляем, все последующие стараемся убрать
    if session.get("first_start_seen"):
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass

    session["faqs"] = []
    session["faq_page"] = 0
    session["stage"] = None
    session["last_message_id"] = None
    session["last_banner_id"] = None
    session["first_start_seen"] = True

    saved_business = session.get("saved_business")
    text = get_welcome_text(saved_business)

    # главный экран с баннером
    send_screen(chat_id, session, text, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())

    if saved_business:
        session["business"] = saved_business
        present_faqs_for_business(chat_id, session, reuse=True)
    else:
        session["stage"] = "waiting_business"

@bot.message_handler(commands=["/help"])
def handle_help(message):
    chat_id = message.chat.id
    #text_msg = (message.text or "").strip()
    session = get_session(chat_id)
    text_msg = "Это краткая страница помощи для полного использования функционала данного бота!\n"\
                                 "Если вы на <i>Шаге 1</i>, то Вам нужно внести данные о своем бизнесе (опишите его идею, суть, не внося никакого вопроса касательно него на данном этапе), после чего (в случае легитимности идеи бизнеса) будет открыта <b>панель</b>.\n"\
                                 "При открытии панели вы сможете узнать ответы на ЧаВо касательно бизнес-идеи или задать <b>свой собственный</b>, а также задать совершенно новую бизнес идею (тогда прошлая идея будет отложена, вернуться к ней можно будет через повторное нажатие кнопки <b>Другой бизнес</b> и повторном внесении идеи), используя кнопку <b>Другой бизнес</b>, после чего Вы будете возвращены на Шаг 1.\n"\
                                 "Также можно вернуться на начальную страницу (при отсуствии внесенной идеи или находясь на странице помощи) через кнопку <b>В меню</b>. Навигация между ответами на ЧаВо производится через панель (кнопки <b>Вперёд</b> и <b>Назад</b> соответственно. Спасибо за использование LogiQ!)\n"
    #session["last_banner_id"] = BANNER_FAQ_ID
    if session["business"]:
        send_screen(chat_id, session, text_msg, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())
    else:
        session["stage"] = "waiting_business"
        #session["last_message_id"] = "Something"
        send_screen(chat_id, session, text_msg, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())


@bot.message_handler(commands=["/"])
def handle_random_cmd(message):
    chat_id = message.chat.id
    text_msg = (message.text or "").strip()
    session = get_session(chat_id)
    if text_msg != "/help":
        text_ = "Простите, я не распознаю данную команду... Воспользуйтесь /help, если возникают трудности."
        #session["last_banner_id"] = BANNER_FAQ_ID
        #saved_business = session.get("saved_business")
        if session["business"] is None:
            session["stage"] = "waiting_business"
            send_screen(chat_id, session, text_, banner_id=BANNER_FAQ_ID)
        else:
            send_screen(chat_id, session, text_, banner_id=BANNER_FAQ_ID)

    else:
        handle_help(message)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def router(message):
    chat_id = message.chat.id
    text_msg = (message.text or "").strip()
    session = get_session(chat_id)

    # Первый /start/старт оставляем в чате, все последующие сообщения пользователя стараемся удалять
    is_start_like = text_msg.lower() in ("старт", "start", "/start") or text_msg == "/start"
    if not (is_start_like and not session.get("first_start_seen")):
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass

    if text_msg.lower() in ("старт", "start"):
        return handle_start(message)

    if "/" in text_msg.lower():
        return handle_random_cmd(message)
    stage = session.get("stage")

    if stage == "waiting_business":
        return handle_business_description(message, session)
    elif stage == "custom_question":
        return handle_custom_question(message, session)
    else:
        return handle_start(message)


def handle_business_description(message, session: Dict[str, Any]) -> None:
    chat_id = message.chat.id
    business = (message.text or "").strip()

    allowed, reason = check_business_allowed(business)

    if not allowed:
        save_packet({
            "type": "rejected_business",
            "chat_id": chat_id,
            "business_raw": business,
            "reason": reason,
        })

        if reason == "NOT_BUSINESS":
            text = (
                "<b>Похоже, это не описание бизнеса.</b>\n"
                "Опиши, на чём ты хочешь <b>зарабатывать</b>: товар, услуга или формат дела."
            )
        elif reason == "ILLEGAL":
            text = "<b>Я не могу помогать с заведомо незаконными видами деятельности.</b>"
        else:
            text = (
                "<b>Не смог понять описание бизнеса.</b>\n"
                "Попробуй сформулировать по-другому и указать, что ты продаёшь и кому."
            )

        send_screen(chat_id, session, text, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())
        return

    session["business"] = business
    session["saved_business"] = business

    present_faqs_for_business(chat_id, session, reuse=False)


def handle_custom_question(message, session: Dict[str, Any]) -> None:
    chat_id = message.chat.id
    question = (message.text or "").strip()

    allowed, reason = check_question_allowed(question, session)

    if not allowed:
        # ЛОГИ И ОТВЕТ ДЛЯ ВЫКИНУТЫХ ВОПРОСОВ
        save_packet({
            "type": "rejected_question",
            "chat_id": chat_id,
            "business": session.get("business") or session.get("saved_business"),
            "question": question,
            "reason": reason,
        })

        if reason == "NOT_BUSINESS":
            text = (
                "<b>Я помогаю только с вопросами про запуск и ведение бизнеса.</b>\n"
                "Попробуй переформулировать так, чтобы вопрос был про твой микробизнес."
            )
        elif reason == "ILLEGAL":
            text = "<b>Я не могу помогать с незаконными запросами или серыми схемами.</b>"
        else:
            text = "<b>Не могу обработать этот вопрос в рамках помощника по бизнесу.</b>"

        # ВАЖНО: на странице выкинутых вопросов НЕ добавляем кнопку «В меню»
        send_screen(chat_id, session, text, banner_id=BANNER_ANSWER_ID)
        return

    # Сразу считаем итоговый ответ и рисуем одну аккуратную страницу
    raw_answer = ask_llm(session, question)
    formatted_answer = format_answer_for_telegram(raw_answer)

    history = session.get("history") or []
    history.append({"q": question, "a": raw_answer})
    session["history"] = history[-10:]

    save_packet({
        "type": "user_question",
        "chat_id": chat_id,
        "business": session.get("business") or session.get("saved_business"),
        "question": question,
        "answer": raw_answer,
    })

    safe_q = html.escape(question)
    safe_a = html.escape(formatted_answer)

    text = (
        "<b>Вопрос:</b>\n"
        f"{safe_q}\n\n"
        "<b>Ответ:</b>\n"
        f"{safe_a}"
    )

    send_screen(chat_id, session, text, banner_id=BANNER_ANSWER_ID, inline_markup=add_common_nav())


# === CALLBACK-КНОПКИ ===

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("faq_"))
def on_faq_button(callback_query):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    session = get_session(chat_id)

    if data == "faq_prev":
        session["faq_page"] = max(0, session.get("faq_page", 0) - 1)
        header = get_faq_header_text(session)
        markup, page, total_pages = build_faq_keyboard(session)
        footer = f"\n\n<i>Страница {page + 1} из {total_pages}</i>"
        markup = add_common_nav(markup)
        send_screen(chat_id, session, header + footer, banner_id=BANNER_FAQ_ID, inline_markup=markup)
        bot.answer_callback_query(callback_query.id)
        return

    if data == "faq_next":
        faqs = session.get("faqs") or []
        size = session.get("faq_page_size", 3)
        total_pages = (len(faqs) + size - 1) // size or 1
        session["faq_page"] = min(total_pages - 1, session.get("faq_page", 0) + 1)
        header = get_faq_header_text(session)
        markup, page, total_pages = build_faq_keyboard(session)
        footer = f"\n\n<i>Страница {page + 1} из {total_pages}</i>"
        markup = add_common_nav(markup)
        send_screen(chat_id, session, header + footer, banner_id=BANNER_FAQ_ID, inline_markup=markup)
        bot.answer_callback_query(callback_query.id)
        return

    if data == "faq_other":
        session["stage"] = "custom_question"
        bot.answer_callback_query(callback_query.id)
        text = (
            "<b>Окей, напиши свой вопрос текстом.</b>\n"
            "<i>Сформулируй конкретно, что тебя волнует по бизнесу.</i>"
        )
        send_screen(chat_id, session, text, banner_id=BANNER_ANSWER_ID, inline_markup=add_common_nav())
        return

    # faq_N
    try:
        idx = int(data.split("_")[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(callback_query.id, "Что-то пошло не так.")
        return

    faqs = session.get("faqs") or []
    if idx < 0 or idx >= len(faqs):
        bot.answer_callback_query(
            callback_query.id,
            "Список вопросов устарел, давай начнём заново."
        )
        session["stage"] = "waiting_business"
        text = (
            "<b>Напиши, какой бизнес тебя интересует.</b>\n"
            "<i>Например: кофейня у дома, маникюр на дому, маркетплейс.</i>"
        )
        send_screen(chat_id, session, text, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())
        return

    faq = faqs[idx]
    question = faq["q"]
    answer = faq["a"]
    formatted_answer = format_answer_for_telegram(answer)

    bot.answer_callback_query(callback_query.id)

    history = session.get("history") or []
    history.append({"q": question, "a": answer})
    session["history"] = history[-10:]

    save_packet({
        "type": "faq_click",
        "chat_id": chat_id,
        "business": session.get("business") or session.get("saved_business"),
        "question": question,
        "answer": answer,
    })

    safe_q = html.escape(question)
    safe_a = html.escape(formatted_answer)

    text = (
        "<b>Вопрос:</b>\n"
        f"{safe_q}\n\n"
        "<b>Ответ:</b>\n"
        f"{safe_a}"
    )

    send_screen(chat_id, session, text, banner_id=BANNER_ANSWER_ID, inline_markup=add_common_nav())


@bot.callback_query_handler(func=lambda c: c.data == "business_other")
def on_business_other(callback_query):
    chat_id = callback_query.message.chat.id
    session = get_session(chat_id)

    bot.answer_callback_query(callback_query.id)

    session["stage"] = "waiting_business"
    session["business"] = None

    text = (
        "<b>Хорошо, давай другой бизнес.</b>\n"
        "Напиши, какой бизнес тебя интересует теперь."
    )
    send_screen(chat_id, session, text, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())


@bot.callback_query_handler(func=lambda c: c.data == "go_menu")
def on_go_menu(callback_query):
    """
    Кнопка "🏠 В меню" доступна почти на всех экранах.
    Возвращает пользователя на главный экран, как будто он нажал /start.
    """
    chat_id = callback_query.message.chat.id
    session = get_session(chat_id)

    bot.answer_callback_query(callback_query.id)

    # не трогаем saved_business, чтобы можно было быстро вернуться к прошлому направлению
    session["faqs"] = []
    session["faq_page"] = 0
    session["stage"] = None
    session["last_message_id"] = None
    session["last_banner_id"] = None
    #session["first_start_seen"] = False
    # first_start_seen оставляем True, чтобы последующие /start уже чистились

    saved_business = session.get("saved_business")
    text = get_welcome_text(saved_business)
    send_screen(chat_id, session, text, banner_id=BANNER_WELCOME_ID, inline_markup=add_common_nav())

    # если бизнес уже был сохранён – сразу покажем FAQ по нему
    if saved_business:
        session["business"] = saved_business
        present_faqs_for_business(chat_id, session, reuse=True)
    else:
        session["stage"] = "waiting_business"


if __name__ == "__main__":
    print("Bot started")
    bot.infinity_polling()