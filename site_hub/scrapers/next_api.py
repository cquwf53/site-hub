"""
Dujiao-Next Vue SPA 适配器

适用网站：go365-card.com, aihuoxingai.com, spark-zone.org

这些网站使用 Vue.js SPA 前端，可能暴露 API 端点。
优先通过 API 获取数据，降级到 Playwright 渲染后直接 JS 提取数据。
"""
import re
import json
import asyncio
import logging
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
import httpx
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None
from site_hub.scrapers.models import ScrapeResult, ScrapedProduct
from site_hub.scrapers.base import BaseScraper
from site_hub.scrapers.compat_config import BROWSER_HEADLESS, BROWSER_TIMEOUT
from site_hub.scrapers.network import (
    API_CONNECT_TIMEOUT_SECONDS,
    API_MAX_CAPTURED_RESPONSES,
    API_MAX_REDIRECTS,
    API_MAX_RESPONSE_BYTES,
    API_READ_TIMEOUT_SECONDS,
    API_TOTAL_TIMEOUT_SECONDS,
    redact_url,
    sanitize_log_value,
)


logger = logging.getLogger(__name__)


# 常见的 Dujiao-Next API 路径
API_PATHS = [
    "/api/v1/public/products",
    "/api/v1/public/commodities",
    "/api/v1/guest/products",
    "/api/v1/guest/commodities",
    "/api/public/products",
    "/api/guest/products",
    "/api/products",
    "/api/v1/products",
    "/api/v1/commodities",
]


class NextApiScraper(BaseScraper):
    scraper_type = "next_api"

    async def scrape(self, site_url: str, scraper_config: Dict = None) -> ScrapeResult:
        t = self._timer_start()
        base_url = site_url.rstrip("/")
        config = scraper_config or {}
        configured_headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
        api_headers = {
            str(name): str(value)
            for name, value in configured_headers.items()
            if str(name).casefold() in {"authorization", "x-api-key", "accept-language"}
            and value not in (None, "")
        }

        # 如果配置中有已知的 API 路径，优先使用
        known_api = config.get("api_path")
        if known_api:
            result = await self._try_api(base_url, known_api, api_headers)
            if result and result.success:
                result.duration_seconds = self._timer_elapsed(t)
                return result

        # 并发探测所有常见 API 路径，取第一个成功的
        result = await self._probe_api_paths(base_url, api_headers)
        if result:
            result.duration_seconds = self._timer_elapsed(t)
            return result

        # API 都失败了，降级到 Playwright
        logger.info("[NextAPI] API unavailable; falling back to Playwright: %s", redact_url(base_url))
        return await self._scrape_with_playwright(base_url, t)

    async def _probe_api_paths(
        self, base_url: str, configured_headers: dict[str, str] | None = None,
    ) -> Optional[ScrapeResult]:
        """并发探测所有已知 API 路径，返回第一个成功的结果"""
        tasks = [
            asyncio.create_task(self._try_api(base_url, path, configured_headers))
            for path in API_PATHS
        ]
        result = None
        try:
            for coro in asyncio.as_completed(tasks):
                r = await coro
                if r and r.success and r.products:
                    result = r
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return result

    async def _try_api(
        self, base_url: str, api_path: str,
        configured_headers: dict[str, str] | None = None,
    ) -> Optional[ScrapeResult]:
        """尝试通过 API 获取商品数据"""
        url = base_url + api_path
        try:
            timeout = httpx.Timeout(
                connect=API_CONNECT_TIMEOUT_SECONDS,
                read=API_READ_TIMEOUT_SECONDS,
                write=API_READ_TIMEOUT_SECONDS,
                pool=API_CONNECT_TIMEOUT_SECONDS,
            )
            async with asyncio.timeout(API_TOTAL_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    max_redirects=API_MAX_REDIRECTS,
                ) as client:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                        "Referer": base_url + "/products",
                    }
                    headers.update(configured_headers or {})
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code != 200:
                            return None

                        declared_length = resp.headers.get("content-length")
                        if declared_length:
                            try:
                                if int(declared_length) > API_MAX_RESPONSE_BYTES:
                                    logger.warning(
                                        "[NextAPI] API response rejected (declared body too large): %s",
                                        redact_url(url),
                                    )
                                    return None
                            except ValueError:
                                pass

                        body = bytearray()
                        async for chunk in resp.aiter_bytes():
                            if len(body) + len(chunk) > API_MAX_RESPONSE_BYTES:
                                logger.warning(
                                    "[NextAPI] API response rejected (body too large): %s",
                                    redact_url(url),
                                )
                                return None
                            body.extend(chunk)

                        try:
                            data = json.loads(body)
                        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                            return None

                # 解析不同格式的 API 响应
                products_data = self._extract_products_list(data)

                if not products_data:
                    return None

                products = self._parse_api_products(products_data, base_url)
                if not products:
                    return None

                categories = list(set(p.category_name for p in products if p.category_name))

                logger.info(
                    "[NextAPI] fetched %d products from API: %s",
                    len(products),
                    redact_url(url),
                )
                return ScrapeResult(
                    success=True,
                    products=products,
                    categories=categories,
                    duration_seconds=0
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "[NextAPI] API request failed %s: %s",
                redact_url(url),
                sanitize_log_value(e),
            )
            return None

    def _extract_products_list(self, data) -> Optional[list]:
        """从各种嵌套结构中提取商品列表"""
        if isinstance(data, list):
            # 检查列表项是否是商品对象
            if data and isinstance(data[0], dict):
                return data
            return None

        if not isinstance(data, dict):
            return None

        # 递归搜索，深度有限
        for key in ["data", "products", "items", "list", "result", "goods", "commodity"]:
            if key in data:
                val = data[key]
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return val
                elif isinstance(val, dict):
                    # 再深一层
                    for k2 in ["products", "items", "list", "data", "records"]:
                        if k2 in val and isinstance(val[k2], list):
                            if val[k2] and isinstance(val[k2][0], dict):
                                return val[k2]

        return None

    def _parse_api_products(self, items: list, base_url: str) -> List[ScrapedProduct]:
        """解析 API 返回的商品数据"""
        products = []
        for item in items:
            if not isinstance(item, dict):
                continue

            # 商品名称
            name = None
            for key in ["name", "title", "gd_name", "product_name", "goods_name"]:
                if key in item and item[key]:
                    val = item[key]
                    if isinstance(val, str):
                        name = val
                    elif isinstance(val, dict):
                        # i18n 格式
                        name = val.get("zh-CN") or val.get("en-US")
                        if not name:
                            for v in val.values():
                                if isinstance(v, str) and v.strip():
                                    name = v.strip()
                                    break
                    if name:
                        break
            if not name:
                continue

            # 价格
            price = None
            for key in ["price", "price_amount", "actual_price", "sell_price", "retail_price"]:
                if key in item and item[key] is not None:
                    try:
                        price = float(item[key])
                        break
                    except (ValueError, TypeError):
                        continue

            # 库存
            stock = -1
            stock_text = None
            for key in ["in_stock", "stock", "auto_stock_available", "inventory", "quantity"]:
                if key in item and item[key] is not None:
                    try:
                        stock = int(item[key])
                        stock_text = str(stock)
                    except (ValueError, TypeError):
                        stock_text = str(item[key])
                    break
            # stock_status 文本映射
            if stock == -1 and "stock_status" in item:
                status_map = {"in_stock": "充足", "low_stock": "少量", "sold_out": "售罄"}
                stock_text = status_map.get(item["stock_status"], item["stock_status"])

            # 已售
            sold = 0
            for key in ["sales_volume", "sold", "sell_count", "sold_count", "buy_count"]:
                if key in item:
                    try:
                        sold = int(item[key])
                    except (ValueError, TypeError):
                        pass
                    break

            # 分类
            category_name = None
            cat = item.get("category") or item.get("group")
            if isinstance(cat, dict):
                # 可能是 {"name": "xxx"} 或 i18n 格式 {"en-US": "xxx", "zh-CN": "xxx"}
                raw_name = cat.get("name") or cat.get("title")
                if isinstance(raw_name, str):
                    category_name = raw_name
                elif isinstance(raw_name, dict):
                    # name 本身是 i18n 字典
                    category_name = raw_name.get("zh-CN") or raw_name.get("en-US")
                    if not category_name:
                        for v in raw_name.values():
                            if isinstance(v, str) and v.strip():
                                category_name = v.strip()
                                break
                if not category_name:
                    # 整个 cat 是 i18n 格式: 优先取 zh-CN
                    category_name = cat.get("zh-CN") or cat.get("en-US")
                if not category_name:
                    # 取任意非空字符串值
                    for v in cat.values():
                        if isinstance(v, str) and v.strip():
                            category_name = v.strip()
                            break
            elif isinstance(cat, str):
                category_name = cat
            if not category_name:
                cat_name_raw = item.get("category_name") or item.get("group_name")
                if isinstance(cat_name_raw, dict):
                    category_name = cat_name_raw.get("zh-CN") or cat_name_raw.get("en-US")
                    if not category_name:
                        for v in cat_name_raw.values():
                            if isinstance(v, str) and v.strip():
                                category_name = v.strip()
                                break
                elif isinstance(cat_name_raw, str):
                    category_name = cat_name_raw

            # 购买链接
            buy_url = None
            product_id = item.get("id") or item.get("product_id")
            if product_id:
                buy_url = f"{base_url}/products/{product_id}"
            detail_url = item.get("url") or item.get("detail_url")
            if detail_url:
                if isinstance(detail_url, str):
                    if detail_url.startswith("/"):
                        buy_url = base_url + detail_url
                    else:
                        buy_url = detail_url

            # 标签
            tags = []
            if item.get("type") == 1 or item.get("auto_send"):
                tags.append("自动发货")
            if item.get("is_recommend") or item.get("recommend"):
                tags.append("推荐")

            # 描述 - 确保是字符串
            desc = item.get("description") or item.get("desc") or item.get("gd_description")
            if desc and not isinstance(desc, str):
                desc = str(desc)

            products.append(ScrapedProduct(
                name=name,
                price=price,
                original_price=f"¥{price}" if price else None,
                stock=stock,
                stock_text=stock_text,
                sold=sold,
                description=desc[:200] if desc else None,
                buy_url=buy_url or base_url,
                tags=tags,
                category_name=category_name or "全部"
            ))

        return products

    async def _scrape_with_playwright(self, base_url: str, start_time: float) -> ScrapeResult:
        """使用 Playwright 降级抓取 - 通过 JS 直接提取页面数据"""
        products = []
        categories = []

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=BROWSER_HEADLESS)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )

                # 拦截 API 请求以捕获商品数据
                api_responses = []

                async def handle_response(response):
                    url = response.url
                    if any(kw in url for kw in ["/product", "/commodit", "/goods", "/api/"]):
                        try:
                            if len(api_responses) >= API_MAX_CAPTURED_RESPONSES:
                                return
                            if "json" in response.headers.get("content-type", ""):
                                declared_length = response.headers.get("content-length")
                                if declared_length:
                                    try:
                                        if int(declared_length) > API_MAX_RESPONSE_BYTES:
                                            return
                                    except ValueError:
                                        pass
                                raw_body = await asyncio.wait_for(
                                    response.body(), timeout=API_READ_TIMEOUT_SECONDS
                                )
                                if len(raw_body) > API_MAX_RESPONSE_BYTES:
                                    return
                                body = json.loads(raw_body)
                                api_responses.append({"url": redact_url(url), "data": body})
                        except asyncio.CancelledError:
                            raise
                        except (UnicodeDecodeError, json.JSONDecodeError, TimeoutError, ValueError):
                            pass

                page = await context.new_page()
                page.on("response", handle_response)

                try:
                    products_url = base_url + "/products"
                    await page.goto(products_url, wait_until="networkidle", timeout=BROWSER_TIMEOUT)
                    await page.wait_for_timeout(4000)

                    # 关闭弹窗
                    try:
                        close_btns = await page.query_selector_all(
                            "button:has-text('我知道了'), button:has-text('我已知晓'), "
                            "button:has-text('我已阅读'), button:has-text('关闭'), "
                            "button:has-text('OK'), button:has-text('确定'), "
                            ".el-dialog__close, .close-btn"
                        )
                        for btn in close_btns:
                            try:
                                await btn.click()
                                await page.wait_for_timeout(500)
                            except:
                                pass
                    except:
                        pass

                    # 先检查拦截到的 API 响应
                    for resp_data in api_responses:
                        product_list = self._extract_products_list(resp_data["data"])
                        if product_list:
                            parsed = self._parse_api_products(product_list, base_url)
                            if parsed:
                                products = parsed
                                logger.info(
                                    "[NextAPI] extracted %d products from intercepted API response",
                                    len(products),
                                )
                                break

                    # 如果 API 拦截没有数据，从 DOM 提取
                    if not products:
                        products = await self._extract_from_dom(page, base_url)

                finally:
                    await browser.close()

            categories = list(set(p.category_name for p in products if p.category_name))

            return ScrapeResult(
                success=True,
                products=products,
                categories=categories,
                duration_seconds=self._timer_elapsed(start_time)
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            return ScrapeResult(
                success=False,
                error_message=f"Playwright fallback failed: {sanitize_log_value(e)}",
                duration_seconds=self._timer_elapsed(start_time)
            )

    async def _extract_from_dom(self, page, base_url: str) -> List[ScrapedProduct]:
        """通过 JavaScript 直接从渲染后的 DOM 提取商品数据"""
        products = []

        # 使用 JS 直接在页面中提取数据
        data = await page.evaluate(r"""
            () => {
                const products = [];

                // 找所有包含价格的卡片元素
                const allElements = document.querySelectorAll('*');
                const pricePattern = /[¥￥]\s*(\d+(?:\.\d+)?)/;
                const stockPattern = /库存[：:]\s*(\d+)/;
                const soldPattern = /已售[：:]\s*(\d+)/;

                // 方法1: 直接查找商品卡片
                const cardSelectors = [
                    '.el-card', '.product-card', '.goods-card',
                    '.product-item', '.goods-item', '.card-item',
                    '[class*="product"]', '[class*="goods"]',
                    '.el-col .el-card'
                ];

                let cards = [];
                for (const sel of cardSelectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > 0) {
                        cards = found;
                        break;
                    }
                }

                // 方法2: 如果没找到，查找包含价格样式的块
                if (cards.length === 0) {
                    const priceEls = document.querySelectorAll(
                        '[class*="price"], [style*="color: red"], [style*="color: rgb(255"]'
                    );
                    const parentSet = new Set();
                    priceEls.forEach(el => {
                        let parent = el.closest('.el-card, .card, [class*="item"], [class*="product"]');
                        if (!parent) parent = el.parentElement?.parentElement;
                        if (parent) parentSet.add(parent);
                    });
                    cards = Array.from(parentSet);
                }

                cards.forEach(card => {
                    const text = card.innerText || '';
                    const priceMatch = text.match(pricePattern);
                    if (!priceMatch) return;

                    // 提取名称 - 找最长的非价格文本行
                    const lines = text.split('\\n')
                        .map(l => l.trim())
                        .filter(l => l.length > 2 && !pricePattern.test(l)
                            && !stockPattern.test(l) && !soldPattern.test(l)
                            && !['自动发货', '推荐', '热销', '库存', '已售'].some(kw => l === kw));

                    let name = lines.length > 0 ? lines.reduce((a, b) => a.length > b.length ? a : b) : null;
                    if (!name || name.length < 3) return;

                    // 清理名称
                    name = name.replace(/^(自动发货|推荐|热销)\s*/, '').trim();
                    if (!name) return;

                    const price = parseFloat(priceMatch[1]);
                    const stockMatch = text.match(stockPattern);
                    const soldMatch = text.match(soldPattern);

                    // 提取链接
                    const link = card.querySelector('a[href]');
                    let buyUrl = '';
                    if (link) buyUrl = link.href;

                    products.push({
                        name: name,
                        price: price,
                        stock: stockMatch ? parseInt(stockMatch[1]) : -1,
                        stockText: stockMatch ? stockMatch[1] : null,
                        sold: soldMatch ? parseInt(soldMatch[1]) : 0,
                        buyUrl: buyUrl
                    });
                });

                return products;
            }
        """)

        for item in data:
            buy_url = item.get("buyUrl", "")
            if not buy_url or not buy_url.startswith("http"):
                buy_url = base_url

            products.append(ScrapedProduct(
                name=item["name"],
                price=item.get("price"),
                original_price=f"¥{item['price']}" if item.get("price") else None,
                stock=item.get("stock", -1),
                stock_text=item.get("stockText"),
                sold=item.get("sold", 0),
                buy_url=buy_url,
                category_name="全部"
            ))

        if products:
            logger.info("[NextAPI] extracted %d products from DOM", len(products))

        return products
