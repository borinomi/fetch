import asyncio
import os
import re
import socket
from contextlib import asynccontextmanager
from datetime import datetime

from pathlib import Path
import uvicorn
import base64
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
    wait_until: str = "load"
    wait_ms: int = 0
    timeout_ms: int = 30000
    wait_for_selector: str | None = None
    auto_scroll: bool = False
    dismiss_banners: bool = False


class ScreenshotRequest(BaseModel):
    url: str
    wait_until: str = "load"
    wait_ms: int = 0
    timeout_ms: int = 30000
    wait_for_selector: str | None = None
    auto_scroll: bool = True       # 스크린샷은 기본 true
    dismiss_banners: bool = True
    full_page: bool = False
    image_type: str = "png"
    quality: int = 80


class MhtmlRequest(BaseModel):
    url: str
    wait_until: str = "load"
    wait_ms: int = 0
    timeout_ms: int = 30000
    wait_for_selector: str | None = None
    auto_scroll: bool = False
    dismiss_banners: bool = False
    inline_computed_styles: bool = False
    return_as: str = "base64"


class PdfRequest(BaseModel):
    url: str
    wait_until: str = "load"
    wait_ms: int = 0
    timeout_ms: int = 30000
    wait_for_selector: str | None = None
    auto_scroll: bool = False
    dismiss_banners: bool = True
    return_as: str = "base64"
    # PDF 전용 옵션
    format: str = "A4"
    landscape: bool = False
    print_background: bool = True
    scale: float = 1.0
    margin_top: str = "0.4in"
    margin_bottom: str = "0.4in"
    margin_left: str = "0.4in"
    margin_right: str = "0.4in"
    prefer_css_page_size: bool = False
    display_header_footer: bool = False
    header_template: str = ""
    footer_template: str = ""
    page_ranges: str = ""

class SingleFileRequest(BaseModel):
    url: str
    wait_until: str = "load"
    wait_ms: int = 0
    timeout_ms: int = 60000
    wait_for_selector: str | None = None
    auto_scroll: bool = False
    dismiss_banners: bool = False
    return_as: str = "base64"
    
    # 콘텐츠 처리 & 압축 (용량 절감 - 시각 영향 없음)
    remove_hidden_elements: bool = True
    remove_unused_styles: bool = True
    remove_unused_fonts: bool = True
    remove_alternative_fonts: bool = True
    remove_alternative_medias: bool = True
    remove_alternative_images: bool = True
    compress_HTML: bool = True
    compress_CSS: bool = True
    group_duplicate_images: bool = True
    group_duplicate_stylesheets: bool = True
    
    # 리소스 차단 (동영상만)
    block_scripts: bool = True
    block_videos: bool = True
    block_audios: bool = True
    block_images: bool = False     # 이미지 유지
    block_fonts: bool = False      # 폰트 유지
    
    save_raw_page: bool = False

class InjectCookieRequest(BaseModel):
    cookie_string: str
    domain: str
    path: str = "/"
    secure: bool = True


# 미리 정의된 페이지 사이즈 (inch 단위)
PAGE_SIZES = {
    "A4":      {"width": 8.27,  "height": 11.69},
    "A3":      {"width": 11.69, "height": 16.54},
    "A5":      {"width": 5.83,  "height": 8.27},
    "Letter":  {"width": 8.5,   "height": 11.0},
    "Legal":   {"width": 8.5,   "height": 14.0},
    "Tabloid": {"width": 11.0,  "height": 17.0},
}

# SingleFile JS 스크립트 경로 (Docker 안에서)
SINGLEFILE_SCRIPT_PATH = os.getenv(
    "SINGLEFILE_SCRIPT_PATH",
    "/app/singlefile-injected.js"
)

# 스크립트 내용을 한 번만 읽어서 메모리에 캐시
_SINGLEFILE_SCRIPT_CACHE: str | None = None

def get_singlefile_script() -> str:
    """SingleFile JS 번들을 읽어서 캐시 후 반환."""
    global _SINGLEFILE_SCRIPT_CACHE
    if _SINGLEFILE_SCRIPT_CACHE is None:
        path = Path(SINGLEFILE_SCRIPT_PATH)
        if not path.exists():
            raise FileNotFoundError(
                f"SingleFile script not found at {path}. "
                "single-file-cli npm 패키지가 설치되어 있는지 확인하세요."
            )
        _SINGLEFILE_SCRIPT_CACHE = path.read_text(encoding="utf-8")
    return _SINGLEFILE_SCRIPT_CACHE

def _inch(value: str) -> float:
    """'0.4in', '10mm', '1cm', '20px' 같은 단위 문자열을 inch float으로 변환."""
    s = value.strip().lower()
    try:
        if s.endswith("in"):
            return float(s[:-2])
        if s.endswith("mm"):
            return float(s[:-2]) / 25.4
        if s.endswith("cm"):
            return float(s[:-2]) / 2.54
        if s.endswith("px"):
            return float(s[:-2]) / 96.0  # 96 DPI 기준
        return float(s)
    except ValueError:
        return 0.4  # 기본값


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
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const distance = 300;
            const interval = 200;
            const maxIdle = 3;
            
            let lastHeight = 0;
            let idleCount = 0;
            
            while (true) {
                window.scrollBy(0, distance);
                await sleep(interval);
                
                const scrollHeight = document.documentElement.scrollHeight;
                const scrolled = window.scrollY + window.innerHeight;
                
                if (scrolled >= scrollHeight - 10) {
                    if (scrollHeight === lastHeight) {
                        idleCount++;
                        if (idleCount >= maxIdle) break;
                    } else {
                        idleCount = 0;
                    }
                    lastHeight = scrollHeight;
                    await sleep(300);
                }
            }
            
            window.scrollTo(0, 0);
            await sleep(300);
        }
    """)

async def inline_computed_styles(page):
    """
    페이지 안의 모든 요소(shadow DOM 포함)의 computed style을
    인라인 style 속성으로 박아넣는다. MHTML이 외부/cross-origin/동적
    stylesheet를 일부 놓치는 문제를 해결.
    """
    await page.evaluate("""
        () => {
            // 인라인화할 핵심 속성들 (시각적 영향 큰 것만)
            const PROPS = [
                'display', 'position', 'top', 'right', 'bottom', 'left',
                'width', 'height', 'min-width', 'min-height', 'max-width', 'max-height',
                'margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
                'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
                'flex', 'flex-direction', 'flex-wrap', 'flex-grow', 'flex-shrink', 'flex-basis',
                'align-items', 'align-self', 'justify-content', 'justify-self', 'gap',
                'grid-template-columns', 'grid-template-rows', 'grid-column', 'grid-row',
                'color', 'background', 'background-color', 'background-image',
                'background-size', 'background-position', 'background-repeat',
                'font-family', 'font-size', 'font-weight', 'font-style', 'line-height',
                'text-align', 'text-decoration', 'letter-spacing', 'white-space',
                'border', 'border-radius', 'border-color', 'border-style', 'border-width',
                'box-shadow', 'opacity', 'transform', 'visibility', 'overflow',
                'fill', 'stroke', 'stroke-width'
            ];
            
            function inline(root) {
                const elements = root.querySelectorAll('*');
                elements.forEach(el => {
                    // SVG/HTML 양쪽 모두 처리
                    try {
                        const computed = window.getComputedStyle(el);
                        PROPS.forEach(prop => {
                            const val = computed.getPropertyValue(prop);
                            if (val) {
                                el.style.setProperty(prop, val);
                            }
                        });
                    } catch (e) { /* ignore */ }
                    
                    // shadow root 재귀
                    if (el.shadowRoot) {
                        inline(el.shadowRoot);
                    }
                });
            }
            
            inline(document);
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
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout_ms)

            if request.wait_for_selector:
                await page.wait_for_selector(request.wait_for_selector, timeout=request.timeout_ms)

            if request.auto_scroll:
                await auto_scroll(page)
                await asyncio.sleep(1)

            if request.dismiss_banners:
                await hide_banners(page)

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
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/screenshot")
async def screenshot(request: ScreenshotRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout_ms)

            if request.wait_for_selector:
                await page.wait_for_selector(request.wait_for_selector, timeout=request.timeout_ms)

            if request.auto_scroll:
                await auto_scroll(page)
                await asyncio.sleep(1)

            if request.dismiss_banners:
                await hide_banners(page)

            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)

            screenshot_kwargs = {"full_page": request.full_page, "type": request.image_type}
            if request.image_type == "jpeg":
                screenshot_kwargs["quality"] = request.quality

            img_bytes = await page.screenshot(**screenshot_kwargs)
            media_type = "image/png" if request.image_type == "png" else "image/jpeg"
            return Response(content=img_bytes, media_type=media_type)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}

@app.post("/mhtml")
async def capture_mhtml(request: MhtmlRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(
                request.url,
                wait_until=request.wait_until,
                timeout=request.timeout_ms,
            )

            if request.wait_for_selector:
                await page.wait_for_selector(
                    request.wait_for_selector,
                    timeout=request.timeout_ms,
                )

            if request.auto_scroll:
                await auto_scroll(page)
                await asyncio.sleep(1)

            if request.dismiss_banners:
                await hide_banners(page)

            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)

            # ★ MHTML 캡처 직전에 computed style 인라인화
            if request.inline_computed_styles:
                await inline_computed_styles(page)

            # CDP로 MHTML 캡처
            client = await page.context.new_cdp_session(page)
            result = await client.send("Page.captureSnapshot", {"format": "mhtml"})
            await client.detach()

            mhtml_text = result["data"]
            mhtml_bytes = mhtml_text.encode("utf-8")

            if request.return_as == "binary":
                return Response(
                    content=mhtml_bytes,
                    media_type="multipart/related",
                    headers={
                        "Content-Disposition": 'attachment; filename="page.mhtml"'
                    },
                )

            if request.return_as == "raw":
                return {
                    "success": True,
                    "url": page.url,
                    "mhtml": mhtml_text,
                    "size_bytes": len(mhtml_bytes),
                    "timestamp": now_iso(),
                }

            return {
                "success": True,
                "url": page.url,
                "mhtml_base64": base64.b64encode(mhtml_bytes).decode("ascii"),
                "size_bytes": len(mhtml_bytes),
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


@app.post("/pdf")
async def capture_pdf(request: PdfRequest):
    """
    페이지를 PDF로 출력. CDP의 Page.printToPDF 사용 (Chromium 전용).
    """
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(
                request.url,
                wait_until=request.wait_until,
                timeout=request.timeout_ms,
            )

            if request.wait_for_selector:
                await page.wait_for_selector(
                    request.wait_for_selector,
                    timeout=request.timeout_ms,
                )

            if request.auto_scroll:
                await auto_scroll(page)
                await asyncio.sleep(1)

            if request.dismiss_banners:
                await hide_banners(page)

            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)

            # 페이지 사이즈 계산
            size = PAGE_SIZES.get(request.format, PAGE_SIZES["A4"])
            width = size["width"]
            height = size["height"]
            if request.landscape:
                width, height = height, width

            # CDP로 PDF 생성
            client = await page.context.new_cdp_session(page)
            result = await client.send("Page.printToPDF", {
                "landscape": request.landscape,
                "printBackground": request.print_background,
                "scale": request.scale,
                "paperWidth": width,
                "paperHeight": height,
                "marginTop": _inch(request.margin_top),
                "marginBottom": _inch(request.margin_bottom),
                "marginLeft": _inch(request.margin_left),
                "marginRight": _inch(request.margin_right),
                "preferCSSPageSize": request.prefer_css_page_size,
                "displayHeaderFooter": request.display_header_footer,
                "headerTemplate": request.header_template,
                "footerTemplate": request.footer_template,
                "pageRanges": request.page_ranges,
            })
            await client.detach()

            # CDP 결과는 base64 문자열
            pdf_base64 = result["data"]
            pdf_bytes = base64.b64decode(pdf_base64)

            if request.return_as == "binary":
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": 'attachment; filename="page.pdf"'
                    },
                )

            return {
                "success": True,
                "url": page.url,
                "pdf_base64": pdf_base64,
                "size_bytes": len(pdf_bytes),
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

@app.post("/singlefile")
async def capture_singlefile(request: SingleFileRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            
            await page.goto(
                request.url,
                wait_until=request.wait_until,
                timeout=request.timeout_ms,
            )
            script_source = get_singlefile_script()
            await page.evaluate(script_source)

            globals_check = await page.evaluate("""
                () => {
                    return {
                        singlefile: typeof singlefile,
                        SingleFile: typeof SingleFile,
                        getPageData: typeof getPageData,
                        window_singlefile: typeof window.singlefile,
                        keys: Object.keys(window).filter(k => k.toLowerCase().includes('single') || k.toLowerCase().includes('file'))
                    };
                }
            """)
            print(f"GLOBALS CHECK: {globals_check}")
            
            if request.wait_for_selector:
                await page.wait_for_selector(
                    request.wait_for_selector,
                    timeout=request.timeout_ms,
                )

            if request.auto_scroll:
                await auto_scroll(page)
                await asyncio.sleep(1)

            if request.dismiss_banners:
                await hide_banners(page)

            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)

            # SingleFile 옵션
            options = {
                "removeHiddenElements": request.remove_hidden_elements,
                "removeUnusedStyles": request.remove_unused_styles,
                "removeUnusedFonts": request.remove_unused_fonts,
                "removeAlternativeFonts": request.remove_alternative_fonts,
                "removeAlternativeMedias": request.remove_alternative_medias,
                "removeAlternativeImages": request.remove_alternative_images,
                "compressHTML": request.compress_HTML,
                "compressCSS": request.compress_CSS,
                "groupDuplicateImages": request.group_duplicate_images,
                "groupDuplicateStylesheets": request.group_duplicate_stylesheets,
                "blockScripts": request.block_scripts,
                "blockVideos": request.block_videos,
                "blockAudios": request.block_audios,
                "blockImages": request.block_images,
                "blockFonts": request.block_fonts,
                "saveRawPage": request.save_raw_page,
            }
            print(f"SingleFile options: {options}")


            page_data = await page.evaluate(
                """
                async (options) => {
                    if (typeof singlefile === 'undefined') {
                        throw new Error('singlefile is not loaded');
                    }
                    const data = await singlefile.getPageData(options);
                    return {
                        content: data.content,
                        title: data.title,
                        filename: data.filename
                    };
                }
                """,
                options,
)

            html_content = page_data["content"]
            html_bytes = html_content.encode("utf-8")

            if request.return_as == "binary":
                filename = page_data.get("filename") or "page.html"
                return Response(
                    content=html_bytes,
                    media_type="text/html",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    },
                )

            if request.return_as == "raw":
                return {
                    "success": True,
                    "url": page.url,
                    "title": page_data.get("title"),
                    "filename": page_data.get("filename"),
                    "html": html_content,
                    "size_bytes": len(html_bytes),
                    "timestamp": now_iso(),
                }

            return {
                "success": True,
                "url": page.url,
                "title": page_data.get("title"),
                "filename": page_data.get("filename"),
                "html_base64": base64.b64encode(html_bytes).decode("ascii"),
                "size_bytes": len(html_bytes),
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

@app.post("/inject_cookie")
async def inject_cookie(request: InjectCookieRequest):
    try:
        cookies = []
        for pair in request.cookie_string.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": request.domain,
                "path": request.path,
                "secure": request.secure,
                "sameSite": "Lax",
            })

        async with app.state.lock:
            page = await ensure_page()
            await page.context.add_cookies(cookies)

            return {
                "success": True,
                "count": len(cookies),
                "domain": request.domain,
                "timestamp": now_iso(),
            }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8010)
