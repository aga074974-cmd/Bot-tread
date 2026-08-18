# Bot-tread — ربات سفارش زمان‌بندی‌شده برای ایزی‌تریدر مفید

ربات ساده‌ای که در تاریخ/ساعت مشخص، سفارش خرید یا فروش با حجم معین برای نمادهای
مشخص در حساب کارگزاری مفید (EasyTrader) ثبت می‌کند. یک‌بار اجرا برای هر سفارش —
نه استراتژی، نه تحلیل، فقط اجرای زمان‌بندی‌شده.

## ⚠️ نکات مهم قبل از استفاده

- **API رسمی و مستندسازی‌شده‌ای از مفید در دسترس نبود.** بک‌اند واقعی ایزی‌تریدر
  (`api-mts.orbis.easytrader.ir`) از OAuth2/PKCE (از طریق `login.emofid.com`) برای
  ورود استفاده می‌کنه که شبیه‌سازیش با درخواست HTTP ساده سخته. به همین خاطر ربات
  به‌جای زدن درخواست خام، از **خودکارسازی مرورگر (Playwright)** استفاده می‌کنه:
  دقیقاً همون کاری که با انگشت روی سایت انجام می‌دی رو ربات با کد تکرار می‌کنه
  (`bot/broker/mofid_playwright.py`).
- استفاده‌ی خودکار از یک اپ ریتیل (به‌جای API رسمی معاملات الگوریتمی) ممکنه با
  قوانین استفاده‌ی کارگزاری مفید در تناقض باشه و در بدترین حالت باعث مسدود شدن
  حساب بشه. مسئولیت این ریسک با کاربره.
- پیش‌فرض ربات روی `DRY_RUN=true` است — یعنی فقط لاگ می‌زنه که «قرار بود این سفارش
  ثبت بشه» و درخواست واقعی نمی‌فرسته. فقط بعد از تست کامل و اطمینان، `DRY_RUN=false`
  کن.

## تکمیل اتصال به مفید

فرم ورود و مسیر جستجوی نماد → باز کردن فرم خرید/فروش → پر کردن تعداد/قیمت →
دکمه‌ی ارسال، همه از روی اسکرین‌شات واقعی در `bot/broker/mofid_playwright.py`
پیاده شده‌ن. دو نکته هنوز تأیید نشده (چون فقط از روی عکس قابل تشخیص نیست):

- اینکه فیلدهای «تعداد» و «قیمت» واقعاً placeholder هستن یا label جدا.
- متن دقیق پیام موفقیت بعد از ثبت سفارش (`SUCCESS_TEXT` فعلاً یه حدسه).

**قبل از هر استفاده‌ی واقعی، این تست رو انجام بده:**

1. `.env` رو با `DRY_RUN=true` اجرا کن (این حالت واقعاً لاگین می‌کنه و فرم رو پر
   می‌کنه، ولی دکمه‌ی نهایی ارسال/تایید رو نمی‌زنه).
2. بعد از اجرا، پوشه‌ی `logs/screenshots/` رو نگاه کن — عکس‌های
   `..._form_filled` باید نشون بدن تعداد و قیمت درست پر شده.
3. اگه یه مرحله با خطا متوقف شد (مثلاً فیلد پیدا نشد)، آخرین عکس ذخیره‌شده
   دقیقاً نشون میده کجا گیر کرده؛ اون عکس رو بفرست تا selector رو اصلاح کنم.
4. فقط بعد از دیدن عکس‌های درست، `DRY_RUN=false` کن.

## نصب

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env      # و مقادیرش رو پر کن
cp orders.example.yaml orders.yaml   # و سفارش‌هات رو تعریف کن
```

## اجرا (تست محلی)

```bash
python main.py --orders orders.yaml
```

تا وقتی `DRY_RUN=true` باشه، فقط در لاگ می‌بینی که ربات چه سفارشی رو در چه زمانی
"می‌خواست" ثبت کنه.

## دیپلوی روی VPS (systemd)

```bash
sudo useradd -r -s /usr/sbin/nologin mofidbot
sudo mkdir -p /opt/mofid-bot
sudo cp -r . /opt/mofid-bot
cd /opt/mofid-bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo .venv/bin/playwright install --with-deps chromium
sudo cp .env.example .env   # مقادیر واقعی رو بذار
sudo cp orders.example.yaml orders.yaml  # سفارش‌های واقعی
sudo mkdir -p logs && sudo chown -R mofidbot:mofidbot /opt/mofid-bot

sudo cp systemd/mofid-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mofid-bot
sudo journalctl -u mofid-bot -f
```

سرور VPS رو روی ساعت درست (وقت تهران یا NTP هماهنگ) تنظیم کن، چون دقت زمان‌بندی
سفارش به ساعت سیستم وابسته‌ست.

## ساختار سفارش‌ها (`orders.yaml`)

```yaml
orders:
  - symbol: "دارونو"
    side: buy          # یا sell
    quantity: 80
    order_type: market # یا limit
    # price: 32800     # فقط برای limit لازمه
    at: "2026-08-19 11:00"   # به وقت تهران
```

هر سفارش دقیقاً یک‌بار در همون تاریخ/ساعت اجرا می‌شه. اگه ربات به هر دلیلی
(ری‌استارت سرور و…) بیشتر از `GRACE_PERIOD_SECONDS` از زمان مقرر عقب بیفته،
اون سفارش با وضعیت skipped رد می‌شه و ثبت نمی‌شه (برای جلوگیری از سفارش‌های
دیرهنگام و ناخواسته).

## معماری

- `bot/models.py` — مدل سفارش
- `bot/config.py` — بارگذاری تنظیمات (`.env`) و سفارش‌ها (`orders.yaml`)
- `bot/broker/base.py` — اینترفیس عمومی کارگزاری (قابل تعویض برای کارگزاری دیگه)
- `bot/broker/mofid.py` — پیاده‌سازی مخصوص مفید (نیاز به تکمیل endpointها)
- `bot/scheduler.py` — منتظر می‌مونه تا زمان هر سفارش برسه و با retry ثبتش می‌کنه
- `main.py` — نقطه‌ی ورود
