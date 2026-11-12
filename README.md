# ربات تلگرام و بله دانلود و آپلود لینک

ربات‌های `sirgsm_dl_bot` در تلگرام و `sirgsm_download_bot` در بله با Python، aiogram 3، yt-dlp، FFmpeg و SQLite.

## امکانات

- لینک مستقیم فایل و سایت‌های پشتیبانی‌شده توسط yt-dlp، از جمله YouTube، Instagram، TikTok، Aparat، X، SoundCloud و Vimeo
- انتخاب کیفیت‌های موجود تا 1080p با حجم تقریبی
- خروجی MP3 با bitrate برابر 128 یا 320 و metadata/کاور در صورت ارائه توسط سایت
- thumbnail ویدیو و درج زیرنویس فارسی/انگلیسی در صورت وجود
- نمایش درصد، حجم، سرعت و زمان تقریبی دانلود؛ نمایش مرحله پردازش و فشرده‌سازی
- لغو دانلود در صف، هنگام دانلود یا فشرده‌سازی
- صف دانلود با نمایش نوبت تقریبی و کنترل کارهای هم‌زمان
- کش `file_id` بر اساس لینک، کیفیت، نوع خروجی و زیرنویس؛ ارسال فوری دانلودهای تکراری
- فشرده‌سازی ویدیو با FFmpeg برای نزدیک شدن به سقف حجم
- پشتیبانی از پلی‌لیست با تأیید کاربر و محدودیت تعداد موارد
- سهمیه‌ی روزانه بر اساس تعداد و حجم، به وقت تهران
- دستور کاربر `/quota` برای دیدن سهمیه‌ی باقی‌مانده
- مسدودسازی لینک مستقیم به IP خصوصی/محلی و کنترل redirect و حجم
- حذف فایلهای موقت و لینکهای منقضی
- معرفی اسپانسر `sirgsm.ir` و سازنده `Navid Haghbayan` در `/about`

## مدیریت

### پنل تلگرام

شناسه‌ی عددی ادمین را در `.env` قرار دهید:

```env
ADMIN_IDS=123456789
```

دستورهای ادمین:

```text
/admin
/block USER_ID
/unblock USER_ID
/setquota USER_ID COUNT MB
/broadcast متن پیام
```

### پنل وب

پنل وب آمار، کاربران، Jobهای اخیر و مسدود/آزاد کردن کاربر را نمایش می‌دهد. آدرس آن `/admin` و روش ورود HTTP Basic Auth است:

- Username: `admin`
- Password: مقدار `ADMIN_PANEL_TOKEN`

پنل وب و لینک موقت فقط با HTTPS فعال شوند. ابتدا رکورد A زیردامنه مثل `dl.sirgsm.ir` را به IP سرور وصل کنید، سپس:

```env
PUBLIC_DOMAIN=dl.sirgsm.ir
ADMIN_PANEL_TOKEN=یک_رمز_تصادفی_طولانی
```

اجرا با Caddy و HTTPS خودکار:

```bash
docker compose -f compose.yaml -f compose.web.yaml up -d --build
```

پورتهای 80 و 443 باید روی سرور آزاد باشند. آدرس پنل:

```text
https://dl.sirgsm.ir/admin
```

## فایلهای بزرگ

### ۱. Local Telegram Bot API — آپلود تا حدود ۲ گیگابایت

از `my.telegram.org` مقدار `API_ID` و `API_HASH` بگیرید و در `.env` قرار دهید:

```env
API_ID=123456
API_HASH=your_api_hash
MAX_FILE_SIZE_MB=1900
```

سپس:

```bash
docker compose -f compose.yaml -f compose.local-api.yaml up -d --build
```

### ۲. لینک موقت self-hosted

با فعال کردن `compose.web.yaml`، فایل بزرگ‌تر از سقف آپلود در مسیر امن ذخیره و لینک تصادفی با انقضای پیش‌فرض یک ساعت ساخته می‌شود:

```env
FILE_LINK_TTL_SECONDS=3600
```

### اجرای هر دو روش

```bash
docker compose -f compose.yaml -f compose.local-api.yaml -f compose.web.yaml up -d --build
```

برای فایلهای تا ۲ گیگابایت و پردازش سنگین، سرور قوی‌تر و فضای دیسک بیشتر توصیه می‌شود. صرفاً تغییر عدد حجم بدون Local API، محدودیت Bot API معمولی را حذف نمی‌کند.

## نسخه‌ی بله

بله API سازگار با بخش عمده‌ای از Telegram Bot API ارائه می‌کند. سرویس بله همان موتور دانلود، انتخاب کیفیت، MP3، زیرنویس، پلی‌لیست، سهمیه، مدیریت و لینک موقت را استفاده می‌کند، اما دیتابیس، کاربران، سهمیه‌ها و `file_id` cache آن کاملاً از تلگرام جداست.

مستندات بله در حال حاضر سقف 50 مگابایت را برای `sendDocument`، `sendVideo` و `sendAudio` اعلام می‌کند؛ سقف امن سرویس 49 MiB است. Local Bot API تلگرام این محدودیت بله را تغییر نمی‌دهد. فایل بزرگ‌تر، در صورت امکان فشرده و در غیر این صورت با لینک HTTPS یک‌ساعته تحویل می‌شود. بله متدهای Description پروفایل را هنوز پیاده‌سازی نکرده است، بنابراین معرفی کامل در `/start` و `/about` قرار دارد.

```bash
cp .env.bale.example .env.bale
# توکن واقعی بله و ADMIN_IDS را فقط در .env.bale سرور قرار دهید.
docker compose -f compose.yaml -f compose.bale.yaml up -d --build bale-bot
docker compose logs -f bale-bot
```

در استقرار فعلی که Local API و Nginx تلگرام نیز موجود است، overlayهای قبلی را هم در فرمان Compose نگه دارید. وب بله فقط روی `127.0.0.1:8081` باز می‌شود و Nginx مسیر `/bale/` را به آن proxy می‌کند. بنابراین لینک‌های بله با `https://dl.sirgsm.ir/bale/files/...` و پنل وب آن با `https://dl.sirgsm.ir/bale/admin` در دسترس‌اند و توکن‌های دو دیتابیس مخلوط نمی‌شوند.

## راه‌اندازی معمولی

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f bot
```

ربات از Long Polling استفاده می‌کند و برای حالت معمولی دامنه یا پورت ورودی لازم ندارد.

## تنظیمات مهم

| متغیر | پیش‌فرض | توضیح |
|---|---:|---|
| `BOT_TOKEN` | — | توکن ربات تلگرام یا بله |
| `BOT_PLATFORM` | `telegram` | پیام‌رسان: `telegram` یا `bale` |
| `BOT_API_ROOT` | خالی | endpoint سفارشی؛ برای بله `https://tapi.bale.ai` |
| `MAX_FILE_SIZE_MB` | `49` | سقف آپلود پیام‌رسان |
| `MAX_CONCURRENT_JOBS` | `2` | دانلودهای هم‌زمان |
| `YTDLP_MAX_HEIGHT` | `1080` | حداکثر ارتفاع تصویر |
| `YTDLP_ALL_SITES` | `true` | تلاش برای سایتهای generic yt-dlp |
| `AUTO_COMPRESS` | `true` | فشرده‌سازی ویدیوی بزرگ |
| `MAX_PLAYLIST_ITEMS` | `5` | حداکثر موارد هر پلی‌لیست |
| `DAILY_DOWNLOAD_LIMIT` | `10` | تعداد دانلود روزانه هر کاربر |
| `DAILY_SIZE_LIMIT_MB` | `1024` | حجم مجاز روزانه هر کاربر؛ معادل ۱ گیگابایت |
| `ADMIN_IDS` | خالی | ادمین‌های ربات |
| `ALLOWED_USER_IDS` | خالی | محدودسازی به کاربران مشخص؛ خالی یعنی عمومی |
| `FORCE_JOIN_CHANNEL` | خالی | کانال عضویت اجباری؛ ربات باید ادمین کانال باشد |
| `PUBLIC_BASE_URL` | خالی | آدرس HTTPS سرویس برای لینک موقت |
| `FILE_LINK_TTL_SECONDS` | `3600` | عمر لینک موقت |
| `YTDLP_COOKIES_FILE` | خالی | کوکی Netscape برای محتوای مجاز و نیازمند لاگین |

## داده‌ها

دیتابیس SQLite، کش، آمار کاربران و فایلهای لینک موقت داخل volume پایدار `/data` نگهداری می‌شوند. فایلهای موقت دانلود بعد از اتمام Job حذف می‌شوند.

## تست

```bash
python -m unittest discover -v
ruff check --select F app tests
```

## محدودیت‌ها و نکات

- حجم کیفیتها پیش از دانلود تقریبی است؛ حجم واقعی دوباره کنترل می‌شود.
- پشتیبانی از 1700+ سایت به extractorهای yt-dlp و سیاست همان سایتها بستگی دارد؛ هیچ لینک خصوصی یا DRM تضمین نمی‌شود.
- فشرده‌سازی ویدیوهای طولانی روی CPU دوهسته‌ای زمان‌بر است.
- صف در حافظه است و با restart پاک می‌شود؛ کش و آمار SQLite باقی می‌ماند.
- هر کاربر در هر لحظه یک Job فعال دارد تا منابع سرور محفوظ بماند.
- فقط محتوایی دانلود کنید که مجوز استفاده از آن را دارید.
