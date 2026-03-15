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
from playwright_stealth import Stealth

CDP_PORT = 9222
CDP_HOST = os.getenv("CDP_HOST", socket.gethostbyname("host.docker.internal"))

class FetchRequest(BaseModel):
    command: str

class GotoRequest(BaseModel):
    url: str

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
    browser = await playwright.chromium.connect_over_cdp(f"http://{CDP_HOST}:{CDP_PORT}")
    context = browser.contexts[0]
    stealth = Stealth()
    await stealth.apply_stealth_async(context)
    page = await context.new_page()

    app.state.playwright = playwright
    app.state.browser = browser
    app.state.context = context
    app.state.page = page
    app.state.lock = asyncio.Lock()

    try:
        yield
    finally:
        try:
            if not app.state.page.is_closed():
                await app.state.page.close()
        except Exception:
            pass
        await playwright.stop()

app = FastAPI(lifespan=lifespan)

async def ensure_page():
    page = app.state.page
    if page.is_closed():
        context = app.state.context
        page = await context.new_page()
        app.state.page = page
    return page

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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8010)
