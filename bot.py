"""
Quiz Poll Bot
تبعتله أسئلة بأي شكل -> AI يفهمها -> معاينة وتعديل -> نشر كـ Quiz Polls في القناة/الجروب.
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
import openai
from openai import AsyncOpenAI

try:  # جيميناي اختياري
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types
except ImportError:  # pragma: no cover
    genai = genai_errors = types = None
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Poll, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("quizbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
CONFIG_FILE = Path(os.environ.get("CONFIG_PATH", "config.json"))


Q_MAX, OPT_MAX, EXP_MAX, MAX_OPTS = 300, 100, 200, 10

SYSTEM_PROMPT = """You convert raw quiz material into structured JSON. You are a parser, not a conversational assistant: never explain, never comment, never ask questions.

The input may be in Arabic, English or mixed, in any format: numbered questions, options labelled a/b/c/d or أ/ب/ج/د or 1/2/3/4, answers inline (marked with ✅, *, bold, parentheses) or in a separate answer key at the end (e.g. "1-b 2-c" or "الإجابات: ..."). Match each answer key entry to its question.

Return ONLY a JSON array, no markdown fences, no text before or after. Each item:
{"question": "<question text only, without its number>", "options": ["<option text without letter/number label>", ...], "correct": <0-based index of the correct option, or null if the input does not say>, "explanation": "<explanation if the input gives one, else empty string>"}

Rules:
- Keep the original language and wording. Do not fix, translate or rewrite content.
- Never guess the correct answer. If it is not given in the input, use null.
- "explanation": fill it ONLY if the input itself gives a reason/explanation for that question's answer (labels like التعليل, الشرح, السبب, لأن, Explanation, Reason, Because, or a sentence after the answer that justifies it). Copy it as written. NEVER write your own explanation: if the input gives none, use "".
- If a question has no options, skip it.
- If the input contains no questions, return [].
"""


# ---------------------------------------------------------------- config
def _read_file() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _to_chat(v):
    v = str(v).strip()
    return int(v) if v.lstrip("-").isdigit() else v


def _env_user_targets() -> dict:
    """USER_TARGETS=123:@chan1,456:-100999  ->  {123: '@chan1', 456: -100999}"""
    out = {}
    for part in os.environ.get("USER_TARGETS", "").split(","):
        if ":" in part:
            uid, tgt = part.split(":", 1)
            if uid.strip().isdigit() and tgt.strip():
                out[int(uid)] = _to_chat(tgt)
    return out


def load_config(uid: int | None = None) -> dict:
    """الإعدادات الفعلية لمستخدم: الافتراضي من .env < الأدمن في USER_TARGETS < اللي ضبطه بنفسه بالأوامر."""
    raw = _read_file()
    cfg: dict = {k: raw[k] for k in ("target", "target_title", "prefix") if k in raw}  # قديم/عام
    if "target" not in cfg and os.environ.get("TARGET_CHAT", "").strip():
        cfg["target"] = _to_chat(os.environ["TARGET_CHAT"])
    if "prefix" not in cfg and os.environ.get("DEFAULT_PREFIX", "").strip():
        cfg["prefix"] = os.environ["DEFAULT_PREFIX"].strip()
    if uid is not None:
        env_t = _env_user_targets().get(uid)
        if env_t is not None:
            cfg["target"] = env_t
            cfg.pop("target_title", None)
        cfg.update(raw.get("users", {}).get(str(uid), {}))
    return cfg


def set_user_cfg(uid: int, **kv) -> None:
    raw = _read_file()
    raw.setdefault("users", {}).setdefault(str(uid), {}).update(kv)
    CONFIG_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


USERS_POST_TO_TARGET = os.environ.get("USERS_POST_TO_TARGET", "true").strip().lower() not in {"false", "0", "no"}
OPEN_ACCESS = os.environ.get("OPEN_ACCESS", "true").strip().lower() not in {"false", "0", "no"}
MAX_CHARS_USER = int(os.environ.get("USER_MAX_CHARS", "15000"))  # أقصى حجم رسالة للمستخدم العادي
AI_SEM = asyncio.Semaphore(int(os.environ.get("AI_CONCURRENCY", "3")))  # عدد طلبات AI في نفس الوقت


# أدمنز مضافين بالأمر /addadmin (متخزنين في ملف الإعدادات). الأدمنز الأساسيين في ADMIN_IDS.
EXTRA_ADMINS: set = {int(x) for x in _read_file().get("extra_admins", []) if str(x).isdigit()}


def _save_extra_admins() -> None:
    raw = _read_file()
    raw["extra_admins"] = sorted(EXTRA_ADMINS)
    CONFIG_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in EXTRA_ADMINS


def can_use(user_id: int) -> bool:
    return OPEN_ACCESS or is_admin(user_id)


# ---------------------------------------------------------------- AI providers
# كل المزودين مجانيين. البوت بيجرب بالترتيب ولو واحد زحمة/وصل للحد بيروح للتاني.
PROVIDER_DEFS = {
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_MODEL"),
    "cerebras": ("CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", "gpt-oss-120b", "CEREBRAS_MODEL"),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-small-latest", "MISTRAL_MODEL"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free", "OPENROUTER_MODEL"),
}
ORDER = [x.strip() for x in os.environ.get("PROVIDER_ORDER", "groq,cerebras,mistral,openrouter,gemini").split(",") if x.strip()]
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "5000"))

PROVIDERS: list[dict] = []
for name in ORDER:
    if name == "gemini":
        if os.environ.get("GEMINI_API_KEY") and genai:
            PROVIDERS.append(
                {
                    "name": "gemini",
                    "kind": "gemini",
                    "model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
                    "client": genai.Client(api_key=os.environ["GEMINI_API_KEY"]),
                }
            )
    elif name in PROVIDER_DEFS:
        key_env, base_url, default_model, model_env = PROVIDER_DEFS[name]
        if os.environ.get(key_env):
            PROVIDERS.append(
                {
                    "name": name,
                    "kind": "openai",
                    "model": os.environ.get(model_env, default_model),
                    "client": AsyncOpenAI(api_key=os.environ[key_env], base_url=base_url, timeout=90),
                }
            )
if not PROVIDERS:
    raise SystemExit("حط مفتاح واحد على الأقل في .env (GROQ_API_KEY أو CEREBRAS_API_KEY أو ...)")
log.info("AI providers: %s", ", ".join(f"{p['name']}:{p['model']}" for p in PROVIDERS))


def is_retryable(e: Exception) -> bool:
    if isinstance(e, (openai.RateLimitError, openai.InternalServerError, openai.APITimeoutError, openai.APIConnectionError)):
        return True
    if genai_errors and isinstance(e, genai_errors.ServerError):
        return True
    if genai_errors and isinstance(e, genai_errors.ClientError) and e.code == 429:
        return True
    return False


async def ask_provider(p: dict, text: str) -> str:
    if p["kind"] == "gemini":
        resp = await p["client"].aio.models.generate_content(
            model=p["model"],
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        return resp.text or ""
    resp = await p["client"].chat.completions.create(
        model=p["model"],
        temperature=0,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
    )
    return resp.choices[0].message.content or ""


def extract_questions(raw: str) -> list[dict]:
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in answer")
    data = json.loads(raw[start : end + 1])
    out = []
    for d in data:
        q = str(d.get("question", "")).strip()
        opts = [str(o).strip() for o in d.get("options", []) if str(o).strip()][:MAX_OPTS]
        if not q or len(opts) < 2:
            continue
        c = d.get("correct")
        c = c if isinstance(c, int) and 0 <= c < len(opts) else None
        out.append(
            {
                "question": q,
                "options": opts,
                "correct": c,
                "explanation": str(d.get("explanation") or "").strip(),
            }
        )
    return out


async def parse_chunk(text: str) -> list[dict]:
    """بيجرب كل مزود بالترتيب، وبيعيد المحاولة لو زحمة."""
    last: Exception | None = None
    for p in PROVIDERS:
        for attempt in range(2):
            try:
                return extract_questions(await ask_provider(p, text))
            except Exception as e:  # noqa: BLE001
                last = e
                if is_retryable(e) and attempt == 0:
                    log.warning("%s busy/limited, retrying: %s", p["name"], type(e).__name__)
                    await asyncio.sleep(4)
                    continue
                log.warning("%s failed (%s), trying next provider", p["name"], type(e).__name__)
                break
    raise last if last else RuntimeError("no provider answered")


QSTART = re.compile(r"^\s*(\d+\s*[\.\)\-:]|Q\s*\d+|س\s*\d*\s*[\.\)\-:])", re.IGNORECASE)


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) > limit and (not line.strip() or QSTART.match(line) or len(cur) > limit * 1.6):
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


async def parse_questions(text: str, progress=None) -> list[dict]:
    chunks = split_chunks(text)
    out: list[dict] = []
    for i, c in enumerate(chunks, 1):
        if progress and len(chunks) > 1:
            await progress(i, len(chunks))
        async with AI_SEM:
            out.extend(await parse_chunk(c))
        if i < len(chunks):
            await asyncio.sleep(2)
    return out


# ---------------------------------------------------------------- views
def short(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def preview_view(draft: list[dict], uid: int):
    n = len(draft)
    qw, aw = (70, 40) if n <= 20 else (50, 30) if n <= 40 else (38, 22)
    missing = sum(1 for q in draft if q["correct"] is None)
    head = f"📋 لقيت {n} سؤال\n"
    cfg = load_config(uid) if (is_admin(uid) or USERS_POST_TO_TARGET) else {}
    tgt = cfg.get("target")
    head += f"📍 هينزل في: {cfg.get('target_title') or tgt}\n" if tgt else "📍 هينزل في الشات ده\n"
    if missing:
        head += f"⚠️ {missing} سؤال من غير إجابة صح (لازم تحددها قبل النشر)\n"
    foot = "\nابعت رقم السؤال لتعديله، أو دوس نشر."
    lines, used = [], len(head) + len(foot) + 80
    for i, q in enumerate(draft, 1):
        ans = "✅ " + q["options"][q["correct"]] if q["correct"] is not None else "⚠️ مفيش إجابة صح"
        flags = ""
        if len(q["question"]) > Q_MAX or len(q["explanation"]) > EXP_MAX or any(len(o) > OPT_MAX for o in q["options"]):
            flags += " 📏"
        if q["explanation"]:
            flags += " 💡"
        line = f"{i}. {short(q['question'], qw)}\n   {short(ans, aw)}{flags}"
        if used + len(line) + 1 > 3800:
            break
        lines.append(line)
        used += len(line) + 1
    text = head + "\n" + "\n".join(lines)
    if len(lines) < n:
        text += f"\n… و{n - len(lines)} سؤال كمان (ابعت رقم أي سؤال لتعديله)"
    text += foot
    kb = InlineKeyboardMarkup([[btn("✅ نشر الكل", "pub"), btn("❌ إلغاء", "cancel")]])
    return text, kb


def editor_view(draft: list[dict], i: int):
    q = draft[i]
    n = len(q["options"])
    text = f"✏️ سؤال {i + 1}/{len(draft)}\n\n{q['question']}\n\n"
    text += "\n".join(
        f"{'✅' if k == q['correct'] else '▫️'} {k + 1}. {o}" for k, o in enumerate(q["options"])
    )
    if q["explanation"]:
        text += f"\n\n💡 {q['explanation']}"
    if q["correct"] is None:
        text += "\n\n⚠️ لسه محدد إجابة صح"
    rows = [[btn("📝 نص السؤال", f"et:{i}"), btn("💡 الشرح", f"ee:{i}")]]
    rows += chunk([btn(f"✏️ خيار {k + 1}", f"eo:{i}:{k}") for k in range(n)], 4)
    rows += chunk([btn(f"✅ {k + 1}", f"sc:{i}:{k}") for k in range(n)], 5)
    rows.append([btn("🗑 حذف السؤال", f"dq:{i}"), btn("🔙 رجوع", "back")])
    return text[:4000], InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------- publishing
async def send_with_retry(coro_factory):
    while True:
        try:
            return await coro_factory()
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)


async def publish(context: ContextTypes.DEFAULT_TYPE, draft: list[dict], default_chat: int, admin: bool = False, uid: int | None = None):
    # النشر في القناة/الجروب المحدد (/target) لأي حد يستخدم البوت.
    # لو USERS_POST_TO_TARGET=false المستخدم العادي بينزل عنده هو بس.
    cfg = load_config(uid) if (admin or USERS_POST_TO_TARGET) else {}
    chat_id = cfg.get("target") or default_chat
    prefix = cfg.get("prefix", "")
    ids, failed = [], []
    for n, q in enumerate(draft, 1):
        question = f"{prefix}\n{q['question']}" if prefix else q["question"]
        try:
            if len(question) > Q_MAX:
                m = await send_with_retry(lambda: context.bot.send_message(chat_id, question))
                ids.append(m.message_id)
                question = "اختر الإجابة الصحيحة 👇"
            options = [o if len(o) <= OPT_MAX else o[: OPT_MAX - 1] + "…" for o in q["options"]]
            exp = (q["explanation"] if len(q["explanation"]) <= EXP_MAX else q["explanation"][: EXP_MAX - 1] + "…") or None
            m = await send_with_retry(
                lambda: context.bot.send_poll(
                    chat_id,
                    question,
                    options,
                    type=Poll.QUIZ,
                    is_anonymous=True,
                    correct_option_id=q["correct"],
                    explanation=exp,
                )
            )
            ids.append(m.message_id)
        except TelegramError as e:
            log.warning("poll %s failed: %s", n, e)
            failed.append((n, str(e)))
        await asyncio.sleep(1.2)
    context.user_data["last_batch"] = {"chat": chat_id, "ids": ids, "draft": draft}
    return failed


# ---------------------------------------------------------------- commands
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"الـ id بتاعك: {update.effective_user.id}"
    if update.effective_chat.type != "private":
        text += f"\nالـ id بتاع الشات ده: {update.effective_chat.id}"
    await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text(f"البوت خاص. الـ id بتاعك: {update.effective_user.id}")
        return
    await update.message.reply_text(
        "👋 أهلاً! ابعتلي أسئلتك مع إجاباتها بأي شكل (نص أو ملف txt) وأنا أحولها Quiz Polls جاهزة.\n\n"
        "للتفاصيل: /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use(uid):
        return
    text = (
        "📖 طريقة الاستخدام\n\n"
        "1️⃣ ابعت الأسئلة والإجابات (نص أو ملف txt) بأي شكل.\n"
        "2️⃣ هتظهرلك معاينة. ابعت رقم أي سؤال عشان تعدل نصه أو خياراته أو الإجابة الصح أو الشرح، أو تحذفه.\n"
        "3️⃣ دوس \"نشر الكل\" وتنزل Quiz Polls جاهزة.\n\n"
        "💡 لو الأسئلة كتير خلي الإجابة جنب كل سؤال مش في آخر الرسالة.\n"
        "💡 لو فيه تعليل للإجابة اكتبه وهيتحط في الـ Poll، ولو مفيش هيتعمل عادي.\n\n"
        "الأوامر:\n\n"
        "↩️ مسح آخر دفعة اتنشرت ورجوعها للتعديل:\n"
        "/undo\n\n"
        "❌ إلغاء المسودة الحالية:\n"
        "/cancel\n\n"
        "🆔 معرفة الـ user id:\n"
        "/id"
    )
    if is_admin(uid):
        text += (
            "\n\n👑 للأدمن فقط:\n\n"
            "📍 مكان النشر بتاعك (قناة أو جروب). كل أدمن بيحدد مكانه لوحده:\n"
            "/target @channel\n\n"
            "🔥 عنوان فوق كل سؤالك (off للإلغاء):\n"
            "/prefix Grammar\n\n"
            "👥 إضافة وحذف أدمن (للأدمن الأساسي بس):\n"
            "/addadmin 123456789\n"
            "/removeadmin 123456789\n\n"
            "📋 قايمة الأدمنز:\n"
            "/admins"
        )
    await update.message.reply_text(text)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        return
    context.user_data.pop("awaiting", None)
    if context.user_data.pop("draft", None):
        await update.message.reply_text("❌ اتلغت المسودة.")
    else:
        await update.message.reply_text("مفيش مسودة أصلاً.")


async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("الأمر ده للأدمن بس.")
        return
    in_group = update.effective_chat.type != "private"
    arg = context.args[0] if context.args else ("here" if in_group else None)
    if arg is None:
        cfg = load_config(uid)
        cur = cfg.get("target_title") or cfg.get("target")
        await update.message.reply_text(
            f"مكان النشر بتاعك: {cur or 'الشات الخاص'}\n\n"
            "لتغييره:\n/target @channel\n/target -100123456789\n\n"
            "أو ابعت /target جوه الجروب نفسه.\n"
            "لإلغائه: /target off\n\n"
            "(كل أدمن بيحدد مكانه لوحده)"
        )
        return
    if arg.lower() == "off":
        set_user_cfg(uid, target=None, target_title=None)
        await update.message.reply_text("اتلغى مكان النشر بتاعك. هتنزل الأسئلة في الشات الخاص.")
        return
    target = update.effective_chat.id if arg.lower() == "here" else _to_chat(arg)
    try:
        chat = await context.bot.get_chat(target)
    except TelegramError as e:
        await update.message.reply_text(f"معرفتش أوصل للمكان ده: {e}\nتأكد إن البوت مضاف أدمن فيه.")
        return
    set_user_cfg(uid, target=chat.id, target_title=chat.title or str(chat.id))
    await update.message.reply_text(f"تمام، أسئلتك هتنزل في: {chat.title or chat.id}")


async def cmd_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("الأمر ده للأدمن بس.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(f"العنوان بتاعك: {load_config(uid).get('prefix') or 'مفيش'}\nلتغييره: /prefix 🔥 Grammar")
        return
    if text.lower() == "off":
        set_user_cfg(uid, prefix="")
        await update.message.reply_text("اتلغى العنوان.")
    else:
        set_user_cfg(uid, prefix=text)
        await update.message.reply_text(f"تمام، العنوان: {text}")


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("الأمر ده للأدمن الأساسي بس (اللي في ADMIN_IDS).")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "اكتب الأمر مع الـ id بتاع الشخص:\n/addadmin 123456789\n\n"
            "الشخص يبعت /id للبوت وهيرد عليه برقمه."
        )
        return
    new = int(context.args[0])
    if is_admin(new):
        await update.message.reply_text("هو أدمن أصلاً.")
        return
    EXTRA_ADMINS.add(new)
    _save_extra_admins()
    text = f"✅ اتضاف {new} أدمن. يقدر يحدد قناته بـ /target."
    if not os.environ.get("CONFIG_PATH"):
        text += "\n\nملاحظة: لو البوت على استضافة بتمسح الملفات مع كل تشغيل، ضيفه كمان في ADMIN_IDS عشان يفضل أدمن."
    await update.message.reply_text(text)
    try:
        await context.bot.send_message(new, "✅ اتضفت أدمن في البوت. ابعت /help تشوف الأوامر.")
    except TelegramError:
        pass  # لسه ما بدأش البوت، عادي


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("الأمر ده للأدمن الأساسي بس (اللي في ADMIN_IDS).")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("اكتب الأمر مع الـ id:\n/removeadmin 123456789")
        return
    target = int(context.args[0])
    if target in ADMIN_IDS:
        await update.message.reply_text("ده أدمن أساسي، شيله من ADMIN_IDS.")
    elif target in EXTRA_ADMINS:
        EXTRA_ADMINS.discard(target)
        _save_extra_admins()
        await update.message.reply_text(f"✅ اتشال {target} من الأدمنز.")
    else:
        await update.message.reply_text("مش أدمن أصلاً.")


async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    base = "\n".join(f"• {x}" for x in sorted(ADMIN_IDS)) or "-"
    extra = "\n".join(f"• {x}" for x in sorted(EXTRA_ADMINS)) or "-"
    await update.message.reply_text(f"👑 الأدمنز الأساسيين:\n{base}\n\n👥 الأدمنز المضافين:\n{extra}")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        return
    batch = context.user_data.get("last_batch")
    if not batch:
        await update.message.reply_text("مفيش دفعة أمسحها.")
        return
    for mid in batch["ids"]:
        try:
            await context.bot.delete_message(batch["chat"], mid)
        except TelegramError:
            pass
        await asyncio.sleep(0.3)
    context.user_data["draft"] = batch["draft"]
    context.user_data.pop("last_batch", None)
    text, kb = preview_view(batch["draft"], update.effective_user.id)
    await update.message.reply_text("🗑 اتمسحت. رجعتها للتعديل:\n\n" + text, reply_markup=kb)


# ---------------------------------------------------------------- messages
async def handle_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    msg = update.message
    awaiting = context.user_data.get("awaiting")
    draft = context.user_data.get("draft")

    # 1) في انتظار نص جديد لتعديل
    if awaiting and draft:
        kind, i, k = awaiting
        context.user_data.pop("awaiting")
        if i >= len(draft):
            return
        q = draft[i]
        value = text.strip()
        if kind == "et":
            q["question"] = value
        elif kind == "ee":
            q["explanation"] = "" if value == "-" else value
        elif kind == "eo" and k < len(q["options"]):
            q["options"][k] = value
        view, kb = editor_view(draft, i)
        await msg.reply_text(view, reply_markup=kb)
        return

    # 2) رقم سؤال = افتح المحرر
    if text.strip().isdigit() and draft:
        i = int(text.strip()) - 1
        if 0 <= i < len(draft):
            view, kb = editor_view(draft, i)
            await msg.reply_text(view, reply_markup=kb)
        else:
            await msg.reply_text(f"الأرقام من 1 لـ {len(draft)}.")
        return

    # 3) أسئلة جديدة -> AI
    uid = update.effective_user.id
    if not is_admin(uid) and len(text) > MAX_CHARS_USER:
        await msg.reply_text("الرسالة كبيرة أوي. قسمها على كذا رسالة.")
        return
    wait = await msg.reply_text("⏳ بفهم الأسئلة...")
    async def progress(i: int, n: int):
        try:
            await wait.edit_text(f"⏳ بفهم الأسئلة... جزء {i}/{n}")
        except TelegramError:
            pass

    try:
        parsed = await parse_questions(text, progress)
    except Exception:
        log.exception("parse failed")
        await wait.edit_text("كل مزودين الـ AI مشغولين دلوقتي. جرب تبعتها تاني بعد شوية.")
        return
    if not parsed:
        await wait.edit_text("مالقيتش أسئلة في الرسالة دي.")
        return
    context.user_data["draft"] = parsed
    view, kb = preview_view(parsed, uid)
    await wait.edit_text(view, reply_markup=kb)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        return
    await handle_input_text(update, context, update.message.text)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        return
    doc = update.message.document
    if not (doc.mime_type or "").startswith("text/") and not (doc.file_name or "").endswith(".txt"):
        await update.message.reply_text("ابعت ملف .txt أو الصق النص في الرسالة.")
        return
    f = await doc.get_file()
    data = await f.download_as_bytearray()
    await handle_input_text(update, context, bytes(data).decode("utf-8", errors="ignore"))


# ---------------------------------------------------------------- buttons
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not can_use(q.from_user.id):
        await q.answer()
        return
    await q.answer()
    draft = context.user_data.get("draft")
    if not draft:
        await q.edit_message_text("مفيش مسودة. ابعت أسئلة جديدة.")
        return
    context.user_data.pop("awaiting", None)
    parts = q.data.split(":")
    act = parts[0]

    if act == "back":
        text, kb = preview_view(draft, q.from_user.id)
        await q.edit_message_text(text, reply_markup=kb)
    elif act == "cancel":
        context.user_data.pop("draft", None)
        await q.edit_message_text("❌ اتلغت المسودة.")
    elif act == "pub":
        missing = [str(n) for n, x in enumerate(draft, 1) if x["correct"] is None]
        if missing:
            await q.message.reply_text("⚠️ الأسئلة دي من غير إجابة صح، حددها الأول: " + ", ".join(missing))
            return
        if context.user_data.get("publishing"):
            return
        context.user_data["publishing"] = True
        try:
            await q.edit_message_text(f"⏳ بنشر {len(draft)} سؤال...")
            failed = await publish(context, draft, q.message.chat_id, is_admin(q.from_user.id), q.from_user.id)
        finally:
            context.user_data["publishing"] = False
        context.user_data.pop("draft", None)
        report = f"✅ اتنشر {len(draft) - len(failed)} من {len(draft)}."
        if failed:
            report += "\n❌ فشل: " + ", ".join(f"#{n} ({e[:60]})" for n, e in failed)
        report += "\nلو عايز تعدل: /undo"
        await q.message.reply_text(report)
    elif act in {"q", "et", "ee", "eo", "sc", "dq"}:
        i = int(parts[1])
        if not 0 <= i < len(draft):
            return
        item = draft[i]
        if act == "q":
            text, kb = editor_view(draft, i)
            await q.edit_message_text(text, reply_markup=kb)
        elif act == "et":
            context.user_data["awaiting"] = ("et", i, None)
            await q.message.reply_text("ابعت نص السؤال الجديد:")
        elif act == "ee":
            context.user_data["awaiting"] = ("ee", i, None)
            await q.message.reply_text("ابعت الشرح الجديد (أو - لمسحه):")
        elif act == "eo":
            k = int(parts[2])
            context.user_data["awaiting"] = ("eo", i, k)
            await q.message.reply_text(f"ابعت نص الخيار {k + 1} الجديد:")
        elif act == "sc":
            item["correct"] = int(parts[2])
            text, kb = editor_view(draft, i)
            await q.edit_message_text(text, reply_markup=kb)
        elif act == "dq":
            draft.pop(i)
            if not draft:
                context.user_data.pop("draft", None)
                await q.edit_message_text("مفيش أسئلة فاضلة.")
            else:
                text, kb = preview_view(draft, q.from_user.id)
                await q.edit_message_text(text, reply_markup=kb)


# ---------------------------------------------------------------- main
async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "بداية"),
            BotCommand("help", "طريقة الاستخدام"),
            BotCommand("undo", "مسح آخر دفعة"),
            BotCommand("cancel", "إلغاء المسودة"),
            BotCommand("id", "معرفة الـ id"),
        ]
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("حصل خطأ غير متوقع. جرب تاني.")
    except TelegramError:
        pass


def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("target", cmd_target))
    app.add_handler(CommandHandler("prefix", cmd_prefix))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_error_handler(on_error)
    log.info("open access: %s | admins: %s", OPEN_ACCESS, len(ADMIN_IDS))
    app.run_polling()


if __name__ == "__main__":
    main()
