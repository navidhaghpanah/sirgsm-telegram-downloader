from __future__ import annotations

import html
import secrets

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings
from app.database import Database
from app.downloader import human_size

security = HTTPBasic(auto_error=False)


def create_web_app(settings: Settings, database: Database) -> FastAPI:
    app = FastAPI(title="Telegram Link Uploader Admin", docs_url=None, redoc_url=None)

    def authorize(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
        valid = credentials and secrets.compare_digest(credentials.username, "admin") and secrets.compare_digest(
            credentials.password, settings.admin_panel_token
        )
        if not valid:
            raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
        return "admin"

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/files/{token}")
    async def download_shared_file(token: str) -> FileResponse:
        shared = await database.get_shared_file(token)
        if not shared:
            raise HTTPException(status_code=404, detail="Link expired or not found")
        root = settings.file_storage_dir.resolve()
        path = shared.path.resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path, filename=shared.filename, media_type="application/octet-stream")

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_panel(_: str = Depends(authorize)) -> str:
        stats = await database.stats()
        users = await database.list_users(50)
        jobs = await database.recent_jobs(30)
        user_rows = "".join(
            f"<tr><td>{row['user_id']}</td><td>{html.escape(row['first_name'] or '')}</td>"
            f"<td>{'🚫' if row['blocked'] else '✅'}</td><td>{row['daily_count']}</td>"
            f"<td>{human_size(row['total_bytes'])}</td>"
            f"<td><form method='post' action='admin/{'unblock' if row['blocked'] else 'block'}/{row['user_id']}'>"
            f"<button>{'آزاد کردن' if row['blocked'] else 'مسدود کردن'}</button></form></td></tr>"
            for row in users
        )
        job_rows = "".join(
            f"<tr><td>{html.escape(row['id'][:10])}</td><td>{row['user_id']}</td>"
            f"<td>{html.escape(row['quality'] or '-')}</td><td>{html.escape(row['status'])}</td>"
            f"<td>{human_size(row['size'] or 0)}</td></tr>"
            for row in jobs
        )
        return f"""<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>مدیریت ربات</title>
<style>body{{font-family:Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
h1,h2{{color:#7dd3fc}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:#1e293b;padding:16px 22px;border-radius:12px}}
table{{border-collapse:collapse;width:100%;background:#1e293b;margin:12px 0 28px}}td,th{{padding:10px;border-bottom:1px solid #334155;text-align:right}}
button{{padding:6px 12px;cursor:pointer}}code{{direction:ltr}}</style></head><body>
<h1>پنل مدیریت ربات</h1><div class='cards'>
<div class='card'>کاربران<br><b>{stats['users']}</b></div><div class='card'>دانلودها<br><b>{stats['downloads']}</b></div>
<div class='card'>ترافیک<br><b>{human_size(stats['bytes'])}</b></div><div class='card'>کش<br><b>{stats['cached']}</b></div>
<div class='card'>مسدودها<br><b>{stats['blocked']}</b></div></div>
<h2>کاربران اخیر</h2><table><tr><th>شناسه</th><th>نام</th><th>وضعیت</th><th>امروز</th><th>کل ترافیک</th><th>عملیات</th></tr>{user_rows}</table>
<h2>Jobهای اخیر</h2><table><tr><th>Job</th><th>کاربر</th><th>کیفیت</th><th>وضعیت</th><th>حجم</th></tr>{job_rows}</table>
</body></html>"""

    @app.post("/admin/block/{user_id}", response_class=HTMLResponse)
    async def block_user(user_id: int, _: str = Depends(authorize)) -> HTMLResponse:
        await database.set_blocked(user_id, True)
        return HTMLResponse("<script>location='admin'</script>")

    @app.post("/admin/unblock/{user_id}", response_class=HTMLResponse)
    async def unblock_user(user_id: int, _: str = Depends(authorize)) -> HTMLResponse:
        await database.set_blocked(user_id, False)
        return HTMLResponse("<script>location='admin'</script>")

    return app
