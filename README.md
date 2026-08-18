# Bot-tread — ربات سفارش زمان‌بندی‌شده برای ایزی‌تریدر مفید

ربات ساده‌ای که در تاریخ/ساعت مشخص، سفارش خرید یا فروش با حجم معین برای نمادهای
مشخص در حساب کارگزاری مفید (EasyTrader) ثبت می‌کند. یک‌بار اجرا برای هر سفارش —
نه استراتژی، نه تحلیل، فقط اجرای زمان‌بندی‌شده.

## ⚠️ نکات مهم قبل از استفاده

- **API رسمی و مستندسازی‌شده‌ای از مفید در دسترس نبود.** لایه‌ی اتصال به کارگزاری
  (`bot/broker/mofid.py`) روی endpointهای واقعی وب‌اپ `m.easytrader.ir` کار می‌کنه که
  باید با گرفتن ترافیک شبکه از مرورگر (DevTools → Network) استخراج بشن. تا وقتی
  این کار انجام نشه، فایل مربوطه هیچ درخواست واقعی‌ای نمی‌زنه.
- استفاده‌ی خودکار از یک اپ ریتیل (به‌جای API رسمی معاملات الگوریتمی) ممکنه با
  قوانین استفاده‌ی کارگزاری مفید در تناقض باشه و در بدترین حالت باعث مسدود شدن
  حساب بشه. مسئولیت این ریسک با کاربره.
- پیش‌فرض ربات روی `DRY_RUN=true` است — یعنی فقط لاگ می‌زنه که «قرار بود این سفارش
  ثبت بشه» و درخواست واقعی نمی‌فرسته. فقط بعد از تست کامل و اطمینان، `DRY_RUN=false`
  کن.

## تکمیل اتصال به مفید

1. در مرورگر وارد `login.emofid.com` بشو، Developer Tools (F12) → تب Network رو باز کن.
2. یک‌بار لاگین کن، درخواست لاگین رو با «Copy as cURL» کپی کن.
3. یک سفارش آزمایشی (حجم خیلی کم) ثبت کن، درخواست ثبت سفارش رو هم کپی کن.
4. با این دو درخواست، `LOGIN_PATH`/`ORDER_PATH` و بدنه‌ی درخواست‌ها رو در
   `bot/broker/mofid.py` تکمیل کن (یا این اطلاعات رو در اختیار توسعه‌دهنده بذار).

## نصب

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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
