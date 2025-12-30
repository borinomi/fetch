import asyncio
import os
import socket
from datetime import datetime
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
from playwright.async_api import async_playwright

app = FastAPI()

CDP_PORT = 9222
CDP_HOST = os.getenv("CDP_HOST", socket.gethostbyname("host.docker.internal"))

class FetchRequest(BaseModel):
    command: str

@app.post("/fetch")
async def execute_fetch(request: FetchRequest):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://{CDP_HOST}:{CDP_PORT}")

            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            import re
            referrer_match = re.search(r'"referrer"\s*:\s*"([^"]+)"', request.command)

            if not referrer_match:
                return {
                    "success": False,
                    "error": "referrer not found in command",
                    "timestamp": datetime.now().isoformat()
                }

            renderer_url = referrer_match.group(1)
            await page.goto(renderer_url)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)

            wrapped_command = f"""
            async () => {{
                const response = await {request.command};
                return await response.text();
            }}
            """
            data = await page.evaluate(wrapped_command)
            print(f"Fetch complete! Data length: {len(data)}")
            print("=" * 50)

            return {
                "success": True,
                "data": data,
                "timestamp": datetime.now().isoformat()
            }

    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        print("=" * 50)

        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8010)
