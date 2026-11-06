"""Статический сайт-копия Telegram-канала @crypto_news_ca для поиска (Google, Яндекс).

Источник — публичная лента t.me/s/<channel>: секретов и доступа к боту не нужно.
Собирает в ./_site: главную с пагинацией, страницу на каждый пост, разделы по странам и
форматам, sitemap.xml, feed.xml (RSS), robots.txt, 404.

    SITE_URL=https://example.com python build.py
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

CHANNEL = os.environ.get("CHANNEL", "crypto_news_ca")
SITE_URL = os.environ.get("SITE_URL", "https://asagynbaev.github.io/crypto-news-ca-site").rstrip("/")
BASE = urlparse(SITE_URL).path.rstrip("/")  # GitHub Pages без домена: /crypto-news-ca-site
SITE_NAME = "Крипта Центральной Азии"
SITE_DESC = ("Новости крипты Кыргызстана, Казахстана, Узбекистана, Таджикистана и Туркменистана: "
             "законы и регуляторы, лицензии бирж, налоги, суды, схемы мошенников.")
ROOT = Path(__file__).parent
OUT = ROOT / "_site"
CACHE = ROOT / ".cache" / "img"
PER_PAGE = 24
# Не новости: закреп «что здесь» (msg 254). Через запятую, можно переопределить env.
SKIP_IDS = {int(x) for x in os.environ.get("SKIP_IDS", "254").split(",") if x.strip()}
MAX_PAGES_TO_FETCH = 60  # 60 × ~20 постов — с запасом на весь архив

SECTIONS = {
    "kg": ("Кыргызстан", "#KG", "🇰🇬"),
    "kz": ("Казахстан", "#KZ", "🇰🇿"),
    "uz": ("Узбекистан", "#UZ", "🇺🇿"),
    "tj": ("Таджикистан", "#TJ", "🇹🇯"),
    "tm": ("Туркменистан", "#TM", "🇹🇲"),
    "likbez": ("Ликбез", "#ликбез", "📚"),
    "digest": ("Дайджест недели", "#дайджест", "🗓"),
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; crypto-news-ca-site/1.0)"}


@dataclass
class Post:
    id: int
    date: datetime
    html: str            # тело поста (очищенный HTML из Telegram)
    body_html: str       # то же без первой строки — она уходит в <h1>
    text: str            # плоский текст
    title: str
    description: str
    photo_url: str | None
    image: str | None = None    # путь картинки на сайте
    sections: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"/p/{self.id}/"

    @property
    def tg_url(self) -> str:
        return f"https://t.me/{CHANNEL}/{self.id}"


# ---------------------------------------------------------------------------
# Сбор
# ---------------------------------------------------------------------------

_MSG_RE = re.compile(r'<div class="tgme_widget_message_wrap[^"]*"[^>]*>(.*?)(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
_EMOJI_RE = re.compile(r'<i class="emoji"[^>]*>(?:<b>)?(.*?)(?:</b>)?</i>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_ALLOWED = {"b", "strong", "i", "em", "u", "s", "a", "br", "code", "pre", "blockquote"}


def _inner_div(block: str, cls: str) -> str | None:
    """Содержимое <div class="...cls..."> с учётом вложенных div."""
    m = re.search(rf'<div class="[^"]*\b{cls}\b[^"]*"[^>]*>', block)
    if not m:
        return None
    i, depth = m.end(), 1
    for t in re.finditer(r"<(/?)div\b[^>]*>", block[i:]):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return block[i:i + t.start()]
    return block[i:]


def _clean_html(raw: str) -> str:
    raw = _EMOJI_RE.sub(r"\1", raw)
    raw = raw.replace(".…", "…")  # бот режет длинную подпись по предложению и дописывает «…»
    raw = re.sub(r"<br\s*/?>", "<br>", raw)

    def keep(m: re.Match) -> str:
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""
        if name not in _ALLOWED:
            return ""
        if name == "br":
            return "<br>"
        if closing:
            return f"</{name}>"
        if name == "a":
            href = re.search(r'href="([^"]*)"', attrs)
            if not href:
                return "<a>"
            h = href.group(1)
            if h.startswith("?q="):  # хэштег Telegram → без ссылки
                return '<a class="tag">'
            ext = "" if f"t.me/{CHANNEL}" in h else ' rel="nofollow noopener" target="_blank"'
            return f'<a href="{h}"{ext}>'
        return f"<{name}>"

    return re.sub(r"<(/?)([a-zA-Z0-9]+)([^>]*)>", keep, raw).strip()


def _plain(html_s: str) -> str:
    t = html.unescape(_TAG_RE.sub(" ", html_s.replace("<br>", "\n")))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


_LEAD_SYMBOLS = re.compile(r"^[^\w«\"$€(]+")
# Флаги-эмодзи: на Windows рисуются буквами «KG» — в заголовках и описаниях не нужны.
_FLAGS = re.compile("[🇦-🇿]")


def _title(text: str) -> str:
    first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    first = _LEAD_SYMBOLS.sub("", first).strip()
    if len(first) > 110:
        cut = first[:110].rsplit(" ", 1)[0]
        first = cut.rstrip(",;:—-") + "…"
    return first or "Пост"


def _description(text: str, title: str) -> str:
    rest = text.split("\n", 1)[1] if "\n" in text else text
    rest = re.sub(r"#\S+", "", rest)
    rest = re.sub(r"Источник:.*", "", rest)
    rest = " ".join(_FLAGS.sub("", rest).split()).rstrip(" .…")
    if len(rest) < 40:
        rest = title
    if len(rest) > 160:
        return rest[:157].rsplit(" ", 1)[0].rstrip(" ,.;:—-…") + "…"
    return rest


def parse_page(page_html: str) -> list[Post]:
    posts = []
    for m in _MSG_RE.finditer(page_html):
        block = m.group(1)
        pid = re.search(rf'data-post="{CHANNEL}/(\d+)"', block)
        if not pid or "service_message" in block or "tgme_widget_message_poll" in block:
            continue
        if int(pid.group(1)) in SKIP_IDS:
            continue
        body = _inner_div(block, "tgme_widget_message_text")
        if not body:
            continue
        date = re.search(r'<time datetime="([^"]+)"', block)
        photo = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", block)
        clean = _clean_html(body)
        text = _plain(clean)
        title = _title(text)
        first, sep, rest_html = clean.partition("<br>")
        body_html = re.sub(r"^(?:\s*<br>)+", "", rest_html).strip() if sep and _title(_plain(first)) == title else clean
        p = Post(
            id=int(pid.group(1)),
            date=datetime.fromisoformat(date.group(1)) if date else datetime.now(timezone.utc),
            html=clean,
            body_html=body_html,
            text=text,
            title=title,
            description=_description(text, title),
            photo_url=photo.group(1) if photo else None,
        )
        p.sections = [k for k, (_, tag, _) in SECTIONS.items() if tag.lower() in text.lower()]
        posts.append(p)
    return posts


def fetch_all(client: httpx.Client) -> list[Post]:
    seen: dict[int, Post] = {}
    url = f"https://t.me/s/{CHANNEL}"
    for _ in range(MAX_PAGES_TO_FETCH):
        r = client.get(url)
        r.raise_for_status()
        for p in parse_page(r.text):
            seen.setdefault(p.id, p)
        prev = re.search(r'<link rel="prev" href="([^"]+)"', r.text)
        if not prev:
            break
        url = "https://t.me" + html.unescape(prev.group(1))
        time.sleep(0.5)
    return sorted(seen.values(), key=lambda p: p.id, reverse=True)


def fetch_images(client: httpx.Client, posts: list[Post]) -> None:
    """Картинки — к себе: ссылки CDN Telegram подписаны и могут протухнуть.

    Имя файла — хэш URL: обложку в посте заменили → URL другой → скачаем заново.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    (OUT / "img").mkdir(parents=True, exist_ok=True)
    for p in posts:
        if not p.photo_url:
            continue
        h = hashlib.sha1(p.photo_url.split("?")[0].encode()).hexdigest()[:12]
        name = f"{p.id}-{h}.jpg"
        cached = CACHE / name
        if not cached.exists():
            # CDN Telegram изредка отвечает 500 на конкретный файл — пара повторов спасает.
            for attempt in range(3):
                try:
                    r = client.get(p.photo_url)
                    r.raise_for_status()
                    cached.write_bytes(r.content)
                    break
                except httpx.HTTPError as e:
                    if attempt == 2:
                        print(f"img {p.id}: {e}", file=sys.stderr)
                    time.sleep(2 * (attempt + 1))
            if not cached.exists():
                continue
        shutil.copyfile(cached, OUT / "img" / name)
        p.image = f"/img/{name}"


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


def ru_date(d: datetime) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def render(posts: list[Post]) -> None:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["ru_date"] = ru_date
    common = {
        "site_url": SITE_URL, "base": BASE, "site_name": SITE_NAME, "site_desc": SITE_DESC,
        "channel": CHANNEL, "year": datetime.now(timezone.utc).year,
        # в меню — только разделы, где уже есть посты (иначе ссылка в 404)
        "sections": {k: v for k, v in SECTIONS.items() if any(k in p.sections for p in posts)},
    }

    def write(path: str, tpl: str, **ctx) -> None:
        f = OUT / path.lstrip("/") / "index.html" if not path.endswith(".html") else OUT / path.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(env.get_template(tpl).render(**common, **ctx), encoding="utf-8")

    def listing(base: str, items: list[Post], heading: str, intro: str, section: str | None) -> None:
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        for n in range(1, pages + 1):
            chunk = items[(n - 1) * PER_PAGE:n * PER_PAGE]
            path = base if n == 1 else f"{base}page/{n}/"
            write(path, "list.html", posts=chunk, heading=heading, intro=intro, section=section,
                  page=n, pages=pages, list_base=base, canonical=SITE_URL + path, post=None)

    listing("/", posts, SITE_NAME, SITE_DESC, None)
    for key, (name, _, flag) in SECTIONS.items():
        items = [p for p in posts if key in p.sections]
        if items:
            intro = (f"Все материалы канала о крипте: {name}." if key not in ("likbez", "digest")
                     else ("Короткие разборы: как не потерять деньги на P2P, сети USDT, сид-фраза, "
                           "признаки пирамиды." if key == "likbez" else "Главное за неделю одним постом."))
            listing(f"/{key}/", items, name, intro, key)

    for i, p in enumerate(posts):
        related = [q for q in posts if q.id != p.id and set(q.sections) & set(p.sections)][:4] or \
                  [q for q in posts if q.id != p.id][:4]
        ld = {
            "@context": "https://schema.org", "@type": "NewsArticle",
            "headline": p.title[:110], "description": p.description,
            "datePublished": p.date.isoformat(), "dateModified": p.date.isoformat(),
            "mainEntityOfPage": SITE_URL + p.url, "inLanguage": "ru",
            "image": [SITE_URL + p.image] if p.image else [SITE_URL + "/static/logo.png"],
            "publisher": {"@type": "Organization", "name": SITE_NAME,
                          "logo": {"@type": "ImageObject", "url": SITE_URL + "/static/logo.png"}},
        }
        write(p.url, "post.html", post=p, related=related, canonical=SITE_URL + p.url,
              ld_json=json.dumps(ld, ensure_ascii=False))

    write("/404.html", "404.html", canonical=SITE_URL + "/404.html")

    # sitemap, rss, robots
    urls = [("/", posts[0].date if posts else None)]
    urls += [(f"/{k}/", None) for k in SECTIONS if any(k in p.sections for p in posts)]
    urls += [(p.url, p.date) for p in posts]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u, d in urls:
        lm = f"<lastmod>{d.date().isoformat()}</lastmod>" if d else ""
        sm.append(f"<url><loc>{xml_escape(SITE_URL + u)}</loc>{lm}</url>")
    sm.append("</urlset>")
    (OUT / "sitemap.xml").write_text("\n".join(sm), encoding="utf-8")

    items = []
    for p in posts[:50]:
        items.append(
            f"<item><title>{xml_escape(p.title)}</title><link>{SITE_URL}{p.url}</link>"
            f"<guid>{SITE_URL}{p.url}</guid><pubDate>{p.date.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>{xml_escape(p.description)}</description></item>")
    (OUT / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{xml_escape(SITE_NAME)}</title><link>{SITE_URL}/</link>"
        f"<description>{xml_escape(SITE_DESC)}</description><language>ru</language>"
        + "".join(items) + "</channel></rss>", encoding="utf-8")
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copytree(ROOT / "static", OUT / "static")
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
        posts = fetch_all(client)
        if not posts:
            sys.exit("Не нашёл ни одного поста — t.me/s недоступен или сменилась вёрстка.")
        fetch_images(client, posts)
    render(posts)
    cname = os.environ.get("CNAME", "").strip()
    if cname:
        (OUT / "CNAME").write_text(cname + "\n", encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    print(f"ok: {len(posts)} постов, {sum(1 for p in posts if p.image)} с картинкой → {OUT}")


if __name__ == "__main__":
    main()
