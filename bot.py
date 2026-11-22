#!/usr/bin/env python3
"""Автономный игровой канал. Раз в час (GitHub Actions) проверяет окно, дневной лимит и паузу,
пишет текст нейросетью (с запасными вариантами), рисует обложку, постит в Telegram.
Лонгрид выходит ответом на пост-анонс (так его забирает бот Дзена, как в «Завтра ближе»).

Флаги: --check (проверить источники) | --dry-run (печать, без отправки/сохранения) | --force news|hot|gem|longread|poll
"""
import argparse, html, json, logging, os, random, re, sys, time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import feedparser
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
MSK = timezone(timedelta(hours=3))
NAME = os.getenv("CHANNEL_NAME", "Игровой сигнал")

def num(name, default):
    return int(os.getenv(name) or default)


MAX_PER_DAY = num("MAX_PER_DAY", 5)        # публикаций в сутки (МСК)
MIN_GAP_MIN = num("MIN_GAP_MINUTES", 120)  # пауза между публикациями
HOUR_FROM, HOUR_TO = num("POST_HOUR_FROM", 9), num("POST_HOUR_TO", 23)
SILENCE_HOURS = num("SILENCE_ALERT_HOURS", 30)
WEB_SCOUT = os.getenv("WEB_SCOUT", "1") != "0"
LONGREADS = os.getenv("LONGREADS", "1") != "0"
ROTATION = ["news", "gem", "hot", "news", "longread", "poll", "news"]  # формат по кругу; воскресный longread = итоги недели
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
BLOCKED_HOSTS = ("google.", "youtube.", "youtu.be", "facebook.", "twitter.", "x.com", "instagram.", "t.me", "tiktok.", "reddit.")

RU_RSS = {"StopGame": "https://stopgame.ru/rss/rss_news.xml",
          "Игромания": "https://www.igromania.ru/rss/rss_news.xml",
          "Канобу": "https://kanobu.ru/rss/news/",
          "DTF": "https://dtf.ru/rss/games"}
EN_RSS = {"PC Gamer": "https://www.pcgamer.com/rss/",
          "IGN": "https://feeds.feedburner.com/ign/games-all"}
SUBS = {"Games": 1000, "pcgaming": 1000, "GamingLeaksAndRumours": 500}
STEAM_URL = "https://store.steampowered.com/api/featuredcategories?cc=ru&l=english"
STEAM_LABEL = {"new_releases": "новинка", "top_sellers": "в топе продаж", "coming_soon": "скоро выйдет", "specials": "скидка"}

ANGLES = ["ироничный, с мягким сарказмом", "аналитичный: что это значит для игроков", "как восторженный фанат",
          "коротко и сухо, как сводка", "интрига: начни с вопроса или контраста", "как разговор с другом в чате"]
TAGS = {"news": "НОВОСТЬ", "hot": "ГОРЯЧЕЕ", "gem": "НАХОДКА", "longread": "РАЗБОР"}
EVERGREEN = ["5 причин, почему старые игры иногда лучше новых", "Как выбрать игру на вечер, если всё надоело",
             "Почему игры-сервисы устают быстрее, чем одиночные кампании", "Инди против AAA: где сейчас интереснее",
             "Как не переплачивать за игры: простые правила скидок"]
POLLS = [("Во что играете на выходных?", ["Новинки", "Старые любимые", "Инди", "Не играю, работаю"]),
         ("Сколько вы готовы платить за игру?", ["До 500 ₽", "500–2000 ₽", "Дороже 2000 ₽", "Только по скидке"]),
         ("Что важнее в игре?", ["Сюжет", "Геймплей", "Графика", "Атмосфера"]),
         ("Бросаете игры, не дойдя до конца?", ["Постоянно", "Иногда", "Почти никогда", "Прохожу всё"])]

SYSTEM = (f"Ты автор популярного русскоязычного игрового Telegram-канала «{NAME}». Пишешь живо, по-человечески, "
          "с характером и собственным мнением, без канцелярита, воды и кликбейта-обмана. Используй ТОЛЬКО факты из блока "
          "«Факты»: не выдумывай цифры, даты, цитаты, названия; если фактов мало, пиши осторожно и давай мнение, а не факты. "
          "Не копируй исходный текст, пересказывай своими словами. Никаких вступлений вроде «Вот пост», сразу текст. "
          "Разрешены только теги <b> и <i>.")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
log = logging.getLogger("bot")
S = requests.Session()
S.headers["User-Agent"] = "python:game-channel-bot:2.0"
if os.getenv("PROXY"):
    S.proxies = {"http": os.environ["PROXY"], "https": os.environ["PROXY"]}


class NoTopic(Exception):
    pass


# ───────────── сеть и источники ─────────────
def get(url, tries=3, **kw):
    for i in range(tries):
        try:
            r = S.get(url, timeout=20, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            log.warning("%s (%d/%d): %s", url, i + 1, tries, e)
            time.sleep(2 * (i + 1))
    return None


def jget(url):
    r = get(url)
    try:
        return r.json() if r else None
    except ValueError:
        return None


def plain(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(t or ""))).strip()


def fetch_rss(name, url):
    r = get(url)
    if not r:
        return []
    return [dict(kind="news", src=name, title=plain(e.title), link=e.link, text=plain(e.get("summary", ""))[:700], key=e.link)
            for e in feedparser.parse(r.content).entries[:8] if e.get("title") and e.get("link")]


def fetch_reddit():
    out = []
    for sub, mn in SUBS.items():
        d = jget(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=15")
        for p in (d or {}).get("data", {}).get("children", []):
            x = p["data"]
            if x.get("score", 0) >= mn:
                link = "https://www.reddit.com" + x["permalink"]
                out.append(dict(kind="hot", src=f"Reddit r/{sub}", title=x["title"], link=link,
                                text=plain(x.get("selftext", ""))[:600], score=x["score"], key=link, leak=sub == "GamingLeaksAndRumours"))
    return sorted(out, key=lambda x: -x["score"])


def fetch_steam():
    d = jget(STEAM_URL) or {}
    out = []
    for cat, lab in STEAM_LABEL.items():
        for g in d.get(cat, {}).get("items", []):
            if "id" in g:
                disc = g.get("discount_percent")
                out.append(dict(kind="gem", id=g["id"], name=g["name"], label=lab + (f" -{disc}%" if disc else ""), key=f"steam:{g['id']}"))
    return out


def steam_details(app_id):
    d = (jget(f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=ru&l=russian") or {}).get(str(app_id), {})
    return d.get("data") if d.get("success") else None


# ───────────── состояние ─────────────
def load_state():
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    for k, v in (("seen", []), ("log", []), ("count", 0), ("meta", {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    st["seen"] = st["seen"][-3000:]
    st["log"] = st["log"][-80:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ───────────── нейросеть (цепочка запасных) ─────────────
_BAD = ("image", "tts", "audio", "live", "embedding", "robotics", "computer", "aqa", "imagen", "veo", "gemma", "learnlm", "vision", "exp", "thinking", "native")
_MODELS = None


def _ver(n):
    m = re.match(r"gemini-(\d+(?:\.\d+)?)-", n)
    return float(m.group(1)) if m else 0.0


def gemini_models():
    """Модели Gemini определяются автоматически: если Google переименует или отключит модель, бот подберёт новую."""
    global _MODELS
    if _MODELS is None:
        names = []
        try:
            d = S.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                      headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=30).json()
            names = [m["name"].replace("models/", "") for m in d.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("список моделей Gemini: %s", e)
        ok = [n for n in names if n.startswith("gemini-") and not any(b in n for b in _BAD)]
        ordered = []
        for suf in ("flash", "flash-lite"):
            ordered += sorted([n for n in ok if re.fullmatch(rf"gemini-\d+(?:\.\d+)?-{suf}", n)], key=_ver, reverse=True)
        ordered += [n for n in ok if "flash" in n and n not in ordered]
        pin = os.getenv("GEMINI_MODEL")
        _MODELS = ([pin] if pin else []) + ordered or ["gemini-2.5-flash", "gemini-2.0-flash"]
    return _MODELS


def _gemini_call(prompt, mt, tools=None, system=True):
    for model in gemini_models()[:3]:
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": mt + 3000, "temperature": 0.85}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": SYSTEM}]}
        if tools:
            body["tools"] = tools
        r = S.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", timeout=90,
                   headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json=body)
        if r.ok:
            return r.json()["candidates"][0]
        log.warning("Gemini %s: %s %s", model, r.status_code, r.text[:150])


def _gemini(prompt, mt):
    c = _gemini_call(prompt, mt)
    return "".join(p.get("text", "") for p in c["content"]["parts"]) if c else None


def gemini_search(prompt):
    """Поиск Google через Gemini: возвращает [(uri, title)] реальных найденных страниц."""
    try:
        c = _gemini_call(prompt, 600, tools=[{"google_search": {}}], system=False)
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        log.warning("поиск Gemini: %s", e)
        return []
    ch = ((c or {}).get("groundingMetadata") or {}).get("groundingChunks") or []
    return [(x["web"]["uri"], x["web"].get("title", "")) for x in ch if x.get("web", {}).get("uri")]


def _groq(prompt, mt):
    r = S.post("https://api.groq.com/openai/v1/chat/completions", timeout=90,
               headers={"Authorization": "Bearer " + os.environ["GROQ_API_KEY"]},
               json={"model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), "max_tokens": mt, "temperature": 0.85,
                     "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
    if r.ok:
        return r.json()["choices"][0]["message"]["content"]
    log.warning("Groq: %s %s", r.status_code, r.text[:150])


def _claude(prompt, mt):
    r = S.post("https://api.anthropic.com/v1/messages", timeout=90,
               headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
               json={"model": os.getenv("CLAUDE_MODEL", "claude-haiku-5-5"), "max_tokens": mt, "system": SYSTEM,
                     "messages": [{"role": "user", "content": prompt}]})
    if r.ok:
        return r.json()["content"][0]["text"]
    log.warning("Claude: %s %s", r.status_code, r.text[:150])


def llm(prompt, mt=1200):
    """Первый сработавший провайдер. None, если все недоступны (тогда бот публикует по шаблону)."""
    for env, fn in (("GEMINI_API_KEY", _gemini), ("GROQ_API_KEY", _groq), ("ANTHROPIC_API_KEY", _claude)):
        if not os.getenv(env):
            continue
        for attempt in range(2):
            try:
                out = fn(prompt, mt)
                if out and out.strip():
                    return out.strip()
            except (requests.RequestException, KeyError, IndexError, ValueError) as e:
                log.warning("%s: %s", env, e)
            time.sleep(3)
    return None


def fmt(t):
    """Markdown/HTML от модели -> безопасный Telegram-HTML (только <b>, <i>)."""
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = html.escape(t, quote=False)
    for tag in "bi":
        t = t.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    if t.count("<b>") != t.count("</b>") or t.count("<i>") != t.count("</i>"):
        t = re.sub(r"</?[bi]>", "", t)
    t = re.sub(r"^(вот|конечно|пост:|готово)[^\n]*\n+", "", t.strip(), flags=re.I)
    return t.strip()


# ───────────── обложки ─────────────
FONTS = [str(BASE / "fonts" / "DejaVuSans-Bold.ttf"), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
PALETTES = [((20, 24, 60), (120, 40, 160)), ((10, 40, 50), (20, 130, 120)), ((50, 15, 25), (200, 70, 50)),
            ((15, 15, 25), (60, 90, 200)), ((30, 30, 30), (190, 150, 40))]


def font(size):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_cover(title, tag, bg_bytes=None):
    W, H = 1280, 720
    rnd = random.Random(title)
    accent = rnd.choice([(255, 90, 60), (255, 190, 40), (70, 200, 160), (110, 140, 255)])
    sharp = None
    if bg_bytes:
        try:
            src = Image.open(BytesIO(bg_bytes)).convert("RGB")
            img = Image.blend(ImageOps.fit(src, (W, H)).filter(ImageFilter.GaussianBlur(20)), Image.new("RGB", (W, H)), 0.5)
            sharp = ImageOps.contain(src, (1000, 400))
        except Exception as e:
            log.warning("фон обложки: %s", e)
    if sharp is None:
        c1, c2 = rnd.choice(PALETTES)
        img = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(img)
        for y in range(H):
            k = y / H
            d.line([(0, y), (W, y)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * k) for i in range(3)))
    else:
        img.paste(sharp, ((W - sharp.width) // 2, 100))
    d = ImageDraw.Draw(img)
    tf, size = font(30), 54 if sharp else 70
    tw = int(d.textlength(tag, font=tf))
    d.rounded_rectangle((60, 36, 60 + tw + 40, 90), 12, fill=accent)
    d.text((80, 42), tag, font=tf, fill=(15, 15, 15))
    f = font(size)
    words, lines, cur = title.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= W - 140:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    lines = [l for l in lines if l][:3]
    if len(words) > len(" ".join(lines).split()):
        lines[-1] = lines[-1].rstrip(" .,") + "…"
    lh = int(size * 1.25)
    y = 520 if sharp else (H - lh * len(lines)) // 2
    for l in lines:
        d.text((72, y + 3), l, font=f, fill=(0, 0, 0))
        d.text((70, y), l, font=f, fill=(255, 255, 255))
        y += lh
    ff = font(26)
    d.text((W - 60 - d.textlength(NAME, font=ff), H - 56), NAME, font=ff, fill=accent)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def download(url):
    r = get(url, tries=2) if url else None
    return r.content if r else None


# ───────────── Telegram / Telegraph ─────────────
class TGParseError(Exception):
    pass


def tg(method, **kw):
    url = f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/{method}"
    for _ in range(4):
        try:
            r = S.post(url, timeout=40, **kw)
        except requests.RequestException as e:
            log.warning("Telegram: %s", e)
            time.sleep(4)
            continue
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if r.ok:
            return r.json()
        if "parse entities" in r.text:
            raise TGParseError(r.text)
        raise RuntimeError(f"Telegram {method}: {r.status_code} {r.text[:200]}")
    raise RuntimeError(f"Telegram {method}: не удалось отправить")


def send_post(text, img=None, preview=False, dry=False, reply_to=None):
    """Отправляет пост, возвращает message_id (для ответа-лонгрида)."""
    chat = os.getenv("TG_CHAT_ID", "")
    if dry:
        print("─" * 40, "\n[обложка: %s]\n" % ("есть" if img else "нет"), text, "\n")
        return 0
    rep = {"reply_to_message_id": reply_to} if reply_to else {}
    for t in (text, re.sub(r"</?[bi]>", "", text)):  # вторая попытка без тегов
        mode = {"parse_mode": "HTML"} if t is text else {}
        try:
            if img and len(plain(t)) <= 1000:
                r = tg("sendPhoto", data={"chat_id": chat, "caption": t, **mode, **rep}, files={"photo": ("c.jpg", img, "image/jpeg")})
            else:
                if img:
                    tg("sendPhoto", data={"chat_id": chat, **rep}, files={"photo": ("c.jpg", img, "image/jpeg")})
                r = tg("sendMessage", json={"chat_id": chat, "text": t, "disable_web_page_preview": not preview, **mode, **rep})
            return (r.get("result") or {}).get("message_id", 0)
        except TGParseError:
            continue
    raise RuntimeError("Telegram: текст не принят")


def alert(msg):
    if os.getenv("TG_ADMIN_ID") and os.getenv("TG_TOKEN"):
        try:
            tg("sendMessage", json={"chat_id": os.environ["TG_ADMIN_ID"], "text": "⚠️ " + msg[:3500]})
        except Exception as e:
            log.warning("alert: %s", e)


# ───────────── выбор тем ─────────────
def page_text(h):
    h = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", "", h)
    return " ".join(p for p in (plain(x) for x in re.findall(r"(?is)<p[^>]*>(.*?)</p>", h)) if len(p) > 40)


def fetch_body(url):
    r = get(url, tries=1, headers={"User-Agent": BROWSER_UA})
    return page_text(r.text)[:3000] if r else ""


SCOUT_TOPICS = ["анонсы и трейлеры игр", "громкие релизы и обновления игр", "слухи и утечки об играх", "новости Steam и PC-игр",
                "игровая индустрия: студии, сделки, увольнения", "консоли PlayStation, Xbox, Nintendo"]


def scout(st):
    """Свежие игровые новости со всего интернета (поиск Google через Gemini); каждую страницу открываем и проверяем."""
    if not (WEB_SCOUT and os.getenv("GEMINI_API_KEY")):
        return []
    seen, out = set(st["seen"]), []
    for uri, title in gemini_search(f"Найди самые свежие (за последние сутки) новости: {SCOUT_TOPICS[st['count'] % len(SCOUT_TOPICS)]}. Перечисли 5."):
        r = get(uri, tries=1, headers={"User-Agent": BROWSER_UA})
        if not r or not r.url.startswith("http"):
            continue
        host = r.url.split("/")[2].lower()
        body = page_text(r.text)
        if any(b in host for b in BLOCKED_HOSTS) or r.url in seen or len(body) < 400:
            continue
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
        out.append(dict(kind="news", src=host.removeprefix("www."), title=plain(m.group(1) if m else title)[:200], link=r.url,
                        text=body[:1500], key=r.url))
        if len(out) >= 3:
            break
    return out


def topic(kind, st):
    seen = set(st["seen"])
    if kind == "hot":
        c = [x for x in fetch_reddit() if x["key"] not in seen]
        if not c:
            raise NoTopic("reddit")
        x = c[0]
        note = "Это слух/неподтверждённая информация, прямо скажи об этом." if x["leak"] else ""
        return dict(x, facts=f"Источник: {x['src']} (обсуждение набрало {x['score']} голосов)\nЗаголовок: {x['title']}\nТекст: {x['text']}\n{note}",
                    fallback=f"<b>{html.escape(x['title'])}</b>\n\nОбсуждают на {x['src']}. Что думаете?")
    if kind == "gem":
        c = [x for x in fetch_steam() if x["key"] not in seen]
        if not c:
            raise NoTopic("steam")
        x = random.choice(c)
        d = steam_details(x["id"]) or {}
        genres = ", ".join(g["description"] for g in d.get("genres", [])[:4])
        price = (d.get("price_overview") or {}).get("final_formatted") or ("бесплатно" if d.get("is_free") else "")
        desc = plain(d.get("short_description", ""))
        facts = (f"Игра: {x['name']}\nСтатус в Steam: {x['label']}\nЖанры: {genres}\nДата выхода: {(d.get('release_date') or {}).get('date', '')}\n"
                 f"Цена: {price}\nРазработчик: {', '.join(d.get('developers', []))}\nОписание: {desc}")
        return dict(x, facts=facts, bg=d.get("header_image") or f"https://cdn.akamai.steamstatic.com/steam/apps/{x['id']}/header.jpg",
                    src="Steam", link=f"https://store.steampowered.com/app/{x['id']}", title=x["name"],
                    fallback=f"<b>{html.escape(x['name'])}</b> ({x['label']})\n\n{html.escape(desc[:400])}\n\n{html.escape(price)}")
    # news: свежие записи из случайного порядка лент
    items = []
    for name, url in list(RU_RSS.items()) + list(EN_RSS.items()):
        items += fetch_rss(name, url)
    c = [x for x in items if x["key"] not in seen]
    random.shuffle(c)
    if random.random() < 0.5:  # через раз ищем новость сами, а не только в лентах
        c = scout(st) + c
    if not c:
        raise NoTopic("rss")
    x = c[0]
    if len(x["text"]) < 400:
        x["text"] = fetch_body(x["link"]) or x["text"]
    return dict(x, facts=f"Источник: {x['src']}\nЗаголовок: {x['title']}\nТекст: {x['text']}",
                fallback=f"<b>{html.escape(x['title'])}</b>\n\n{html.escape(x['text'][:350])}")


def any_topic(st):
    for k in random.sample(["gem", "hot", "news"], 3):
        try:
            return topic(k, st)
        except NoTopic:
            continue
    raise NoTopic("any")


def mark(st, t, title, url=""):
    if t and t.get("key"):
        st["seen"].append(t["key"])
    now = datetime.now(MSK)
    st["log"].append({"d": now.strftime("%Y-%m-%d"), "ts": now.isoformat(), "t": title[:120], "u": url})
    st["count"] += 1


def hook(text):
    m = re.search(r"<b>(.*?)</b>", text, re.S)
    return plain(m.group(1) if m else text.split("\n")[0])[:90]


# ───────────── форматы ─────────────
TASKS = {
    "news": "Напиши пост для Telegram 450–700 знаков. Структура: цепляющая первая строка в <b>…</b>, суть в 2–3 предложениях, твоя оценка одним предложением, вопрос читателям. В конце 2–3 хэштега.",
    "hot": "Напиши пост 450–700 знаков про то, что сейчас обсуждают игроки. Первая строка в <b>…</b>, суть, почему это цепляет, вопрос читателям, 2–3 хэштега.",
    "gem": "Напиши пост-находку 500–750 знаков: что за игра, чем цепляет, кому зайдёт, цена/скидка если известна. Первая строка в <b>…</b>, в конце вопрос и 2–3 хэштега.",
}


def f_short(st, kind, dry):
    t = topic(kind, st)
    recent = "; ".join(x["t"] for x in st["log"][-5:])
    raw = llm(f"{TASKS[kind]} Подача: {random.choice(ANGLES)}. Первая строка должна отличаться от недавних: {recent}.\n\nФакты:\n{t['facts']}")
    text = fmt(raw) if raw and len(raw) > 120 else t["fallback"]
    text += f'\n\n🔗 <a href="{html.escape(t["link"], quote=True)}">{html.escape(t["src"])}</a>'
    img = make_cover(hook(text), TAGS[kind], download(t.get("bg")))
    send_post(text, img, dry=dry)
    if not dry:
        mark(st, t, hook(text), t["link"])


def f_long(st, dry):
    if not LONGREADS:
        raise NoTopic("longreads off")
    now = datetime.now(MSK)
    week = [x["t"] for x in st["log"] if x["d"] >= (now - timedelta(days=7)).strftime("%Y-%m-%d")]
    if now.weekday() == 6 and len(week) >= 5:
        t = dict(facts="Темы, которые мы разбирали за неделю:\n" + "\n".join("- " + w for w in week[-20:]), bg=None, key=None,
                 src=NAME, link="", task="Итоги недели в играх: о чём говорили, главные сюжеты, что запомнилось, что ждать дальше. Опирайся только на эти темы.")
    else:
        try:
            t = any_topic(st)
            t["task"] = "Разбор: объясни, что произошло, почему это важно игрокам, какие есть мнения и что дальше."
        except NoTopic:
            e = random.choice(EVERGREEN)
            t = dict(facts=f"Тема: {e}. Опирайся только на общеизвестные факты, без конкретных цифр.", bg=None, key=None, src=NAME, link="", task="Статья на тему.")
    raw = llm(f"{t['task']} Статья 2000–3200 знаков. Первая строка: заголовок до 80 знаков, без решёток и кавычек. Затем 3–4 раздела, "
              "каждый начинается со строки «## Заголовок», абзацы короткие, живой язык, без markdown-списков. В конце вывод и вопрос читателям. "
              "После статьи отдельной строкой «---TG---» и анонс для Telegram до 280 знаков, без хэштегов.\n\nФакты:\n" + t["facts"], mt=4500)
    if not raw or len(raw) < 1200:
        raise RuntimeError("нет текста статьи")
    body, _, teaser = raw.partition("---TG---")
    lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
    title = plain(lines[0].lstrip("# "))[:90]
    parts = [f"<b>{html.escape(title)}</b>"]
    for p in lines[1:]:
        parts.append(f"<b>{html.escape(plain(p.lstrip('# ')))}</b>" if p.startswith("#") else html.escape(plain(p.replace("**", ""))))
    if t["link"]:
        parts.append(f'🔗 <a href="{html.escape(t["link"], quote=True)}">{html.escape(t["src"])}</a>')
    while len("\n\n".join(parts)) > 3900 and len(parts) > 3:
        parts.pop(-2)  # статья не влезает в одно сообщение Telegram: убираем предпоследний абзац
    article = "\n\n".join(parts)
    teaser = plain(teaser) or plain(next((p for p in lines[1:] if not p.startswith("#")), ""))[:260]
    img = make_cover(title, TAGS["longread"], download(t.get("bg")))
    mid = send_post(f"<b>{html.escape(title)}</b>\n\n{html.escape(teaser)}\n\n👇 Разбор в комментарии ниже", img, dry=dry)
    send_post(article, dry=dry, reply_to=mid or None)
    if not dry:
        mark(st, t, title, t["link"])


def f_poll(st, dry):
    recent = "; ".join(x["t"] for x in st["log"][-5:]) or "игры в целом"
    raw = llm("Придумай опрос для игрового канала по мотивам недавних тем: " + recent + ". Формат строго: первая строка вопрос (до 200 знаков), "
              "затем 4 строки с вариантами ответа (до 60 знаков каждый), без нумерации и пояснений.", mt=300)
    q, opts = random.choice(POLLS)
    if raw:
        ls = [re.sub(r"^[\-\*\d\.\)\s]+", "", l).strip() for l in raw.splitlines() if l.strip()]
        if len(ls) >= 3:
            q, opts = ls[0][:250], [o[:90] for o in ls[1:5]]
    if dry:
        print("─" * 40, "\n[опрос]", q, opts)
        return
    tg("sendPoll", json={"chat_id": os.environ["TG_CHAT_ID"], "question": q, "options": opts, "is_anonymous": True})
    mark(st, None, "Опрос: " + q)


def run_format(st, fmt_name, dry):
    if fmt_name == "longread":
        return f_long(st, dry)
    if fmt_name == "poll":
        return f_poll(st, dry)
    return f_short(st, fmt_name, dry)


# ───────────── главный цикл ─────────────
def check():
    for n, u in {**RU_RSS, **EN_RSS}.items():
        print("OK  " if fetch_rss(n, u) else "FAIL", "RSS", n)
    print("OK  " if fetch_reddit() else "FAIL", "Reddit (может быть пусто, если нет постов с нужным рейтингом)")
    print("OK  " if fetch_steam() else "FAIL", "Steam")
    for env in ("GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        print("ключ" if os.getenv(env) else "нет ", env)
    if any(os.getenv(k) for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY")):
        print("Тест нейросети:", (llm("Скажи одно слово: работает", 50) or "НЕ ОТВЕТИЛА")[:60])


def blocked(st, now, manual):
    """Почему сейчас нельзя публиковать (None = можно). Ручной запуск обходит окно и паузу, но не дневной лимит."""
    day = now.strftime("%Y-%m-%d")
    n = sum(1 for x in st["log"] if x["d"] == day)
    if n >= MAX_PER_DAY:
        return f"дневной лимит ({n}/{MAX_PER_DAY})"
    if manual:
        return None
    if not HOUR_FROM <= now.hour < HOUR_TO:
        return f"вне окна {HOUR_FROM}–{HOUR_TO} МСК"
    if st["log"] and st["log"][-1].get("ts"):
        gap = (now - datetime.fromisoformat(st["log"][-1]["ts"])).total_seconds() / 60
        if gap < MIN_GAP_MIN:
            return f"пауза между постами ({int(gap)}/{MIN_GAP_MIN} мин)"
    return None


def throttled_alert(st, now, key, hours, msg):
    last = st["meta"].get(key)
    if last and now - datetime.fromisoformat(last) < timedelta(hours=hours):
        return
    st["meta"][key] = now.isoformat()
    alert(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force")
    a = ap.parse_args()
    if a.check:
        return check()
    if not a.dry_run and not (os.getenv("TG_TOKEN") and os.getenv("TG_CHAT_ID")):
        sys.exit("Задайте TG_TOKEN и TG_CHAT_ID")
    st = load_state()
    now = datetime.now(MSK)
    manual = bool(a.force) or os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    if not a.dry_run and st["log"] and st["log"][-1].get("ts") and now - datetime.fromisoformat(st["log"][-1]["ts"]) > timedelta(hours=SILENCE_HOURS):
        throttled_alert(st, now, "silence", 24, f"Нет публикаций больше {SILENCE_HOURS} ч. Проверьте логи в GitHub Actions.")
    why = blocked(st, now, manual)
    if why and not a.dry_run:
        save_state(st)
        return log.info("Не публикуем: %s", why)

    first = a.force or ROTATION[st["count"] % len(ROTATION)]
    fmts = [first] + [f for f in ("news", "gem", "poll") if f != first]  # запасные форматы
    ok = False
    for f in fmts:
        try:
            run_format(st, f, a.dry_run)
            ok = True
            log.info("Опубликовано: %s", f)
            break
        except Exception as e:  # любой сбой формата -> пробуем следующий
            log.warning("Формат %s не удался: %s", f, e)
    if not a.dry_run:
        if not ok:
            throttled_alert(st, now, "fail", 6, "Не удалось ничего опубликовать (источники/нейросеть/Telegram). Смотрите логи в Actions.")
        save_state(st)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
