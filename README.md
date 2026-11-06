# crypto-news-ca-site

Сайт-копия Telegram-канала [@crypto_news_ca](https://t.me/crypto_news_ca) для поиска Google и Яндекса.

`build.py` читает публичную ленту `t.me/s/crypto_news_ca`, скачивает картинки и собирает статический
сайт в `_site/`: главная, страница на каждый пост, разделы по странам, `sitemap.xml`, `feed.xml`, `robots.txt`.
GitHub Actions (`.github/workflows/pages.yml`) пересобирает его раз в 30 минут и выкладывает на GitHub Pages.

Локально: `pip install -r requirements.txt && SITE_URL=http://localhost:8000 python build.py`.
