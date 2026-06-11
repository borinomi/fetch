import asyncio
import os
import re
import socket
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright
from fastapi.responses import Response

try:
    from playwright_stealth import stealth_async
    STEALTH_MODE = "legacy"
except ImportError:
    from playwright_stealth import Stealth
    STEALTH_MODE = "context"

CDP_PORT = 9222
CDP_HOST = os.getenv("CDP_HOST", socket.gethostbyname("host.docker.internal"))

class FetchRequest(BaseModel):
    command: str

class GotoRequest(BaseModel):
    url: str

class RenderRequest(BaseModel):
    url: str
    wait_until: str = "networkidle"
    wait_ms: int = 0
    timeout_ms: int = 30000

class ScreenshotRequest(BaseModel):
    url: str
    wait_until: str = "networkidle"
    wait_ms: int = 0
    timeout_ms: int = 30000
    full_page: bool = False
    image_type: str = "png"   # "png" or "jpeg"
    quality: int = 80          # jpeg일 때만 사용 (1-100)
    dismiss_banners: bool = True

def now_iso():
    return datetime.now().isoformat()

def extract_referrer(command: str):
    patterns = [
        r'["\']referrer["\']\s*:\s*["\']([^"\']+)["\']',
        r'["\']referer["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

async def create_stealth_page(context):
    if STEALTH_MODE == "legacy":
        page = await context.new_page()
        await stealth_async(page)
        return page
    stealth = Stealth()
    await stealth.apply_stealth_async(context)
    return await context.new_page()

async def run_fetch(page, command: str):
    return await page.evaluate(
        """
        async (command) => {
            const response = await eval(command);
            return await response.text();
        }
        """,
        command
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    playwright = await async_playwright().start()
    app.state.playwright = playwright
    app.state.browser = None
    app.state.context = None
    app.state.page = None
    app.state.lock = asyncio.Lock()

    try:
        yield
    finally:
        try:
            page = getattr(app.state, "page", None)
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
        await playwright.stop()

app = FastAPI(lifespan=lifespan)

async def connect_browser(force=False):
    if not force:
        page = getattr(app.state, "page", None)
        if page is not None:
            try:
                if not page.is_closed():
                    await page.evaluate("1")
                    return page
            except Exception:
                pass

    browser = await app.state.playwright.chromium.connect_over_cdp(f"http://{CDP_HOST}:{CDP_PORT}")

    if not browser.contexts:
        raise RuntimeError("browser context not found")

    context = browser.contexts[0]
    page = await create_stealth_page(context)

    app.state.browser = browser
    app.state.context = context
    app.state.page = page

    return page

async def ensure_page():
    return await connect_browser(force=False)

async def hide_banners(page):
    await page.add_style_tag(content="""
        [class*="cookie"],
        [class*="consent"],
        [id*="cookie"],
        [id*="consent"],
        [class*="gdpr"],
        [aria-label*="cookie" i],
        [aria-label*="consent" i],
        div[role="dialog"][aria-modal="true"] {
            display: none !important;
        }
        html, body {
            overflow: auto !important;
        }
    """)

async def auto_scroll(page):
    await page.evaluate("""
        async () => {
            await new Promise((resolve) => {
                let totalHeight = 0;
                const distance = 300;
                const timer = setInterval(() => {
                    const scrollHeight = document.body.scrollHeight;
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    if (totalHeight >= scrollHeight) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                    }
                }, 100);
            });
        }
    """)

@app.post("/connect")
async def connect_only():
    try:
        async with app.state.lock:
            page = await connect_browser(force=True)
            return {
                "success": True,
                "url": page.url,
                "timestamp": now_iso()
            }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "timestamp": now_iso()
        }

@app.post("/goto")
async def goto_only(request: GotoRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(request.url, wait_until="domcontentloaded")
            return {
                "success": True,
                "url": page.url,
                "timestamp": now_iso()
            }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "timestamp": now_iso()
        }

@app.post("/fetch")
async def execute_fetch(request: FetchRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            data = await run_fetch(page, request.command)
            print(f"Fetch complete! Data length: {len(data)}")
            print("=" * 50)
            return {
                "success": True,
                "data": data,
                "timestamp": now_iso()
            }
    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        print("=" * 50)
        return {
            "success": False,
            "error": str(e),
            "timestamp": now_iso()
        }

@app.post("/fetchgoto")
async def execute_fetch_goto(request: FetchRequest):
    try:
        referrer_url = extract_referrer(request.command)

        if not referrer_url:
            return {
                "success": False,
                "error": "referrer not found in command",
                "timestamp": now_iso()
            }

        async with app.state.lock:
            page = await ensure_page()
            await page.goto(referrer_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            data = await run_fetch(page, request.command)
            print(f"Fetch complete! Data length: {len(data)}")
            print("=" * 50)
            return {
                "success": True,
                "data": data,
                "timestamp": now_iso()
            }

    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        print("=" * 50)
        return {
            "success": False,
            "error": str(e),
            "timestamp": now_iso()
        }

@app.post("/render")
async def render_html(request: RenderRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout_ms, )
            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)
            html = await page.content()
            return {
                "success": True,
                "url": page.url,
                "html": html,
                "length": len(html),
                "timestamp": now_iso(),
            }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "timestamp": now_iso(),
        }

@app.post("/screenshot")
async def screenshot(request: ScreenshotRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(
                request.url,
                wait_until=request.wait_until,
                timeout=request.timeout_ms,
            )

            await auto_scroll(page)
            await asyncio.sleep(1)

            if request.dismiss_banners:
                await hide_banners(page)

            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)

            screenshot_kwargs = {
                "full_page": request.full_page,
                "type": request.image_type,
            }
            if request.image_type == "jpeg":
                screenshot_kwargs["quality"] = request.quality

            img_bytes = await page.screenshot(**screenshot_kwargs)

            media_type = "image/png" if request.image_type == "png" else "image/jpeg"
            return Response(content=img_bytes, media_type=media_type)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "timestamp": now_iso(),
        }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8010)
