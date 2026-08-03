"""
自定义 SPA 商城适配器

适用网站：使用自定义框架开发的 SPA 商城
示例：academicgate.org/store

这类网站特点：
  - 单页应用（SPA），商品由 JS 动态渲染
  - 商品按 section 分组（h2/h3 作为分类标题）
  - 每个商品卡片包含：名称、价格（¥xx）、库存、销量
  - 使用 URL hash 切换分类
"""
import re
import json
from typing import Any, Dict, List
try:
    from playwright.async_api import async_playwright, Page
except ImportError:
    async_playwright = None
    Page = Any
from site_hub.scrapers.models import ScrapeResult, ScrapedProduct
from site_hub.scrapers.base import BaseScraper
from site_hub.scrapers.compat_config import BROWSER_HEADLESS, BROWSER_TIMEOUT
from site_hub.scrapers.network import sanitize_log_value


# 整串就是个价格的名称（如 "￥130.00"、"130 CNY"）说明名称提取失败了
PRICE_ONLY_RE = re.compile(
    r"^(?:PRICE\s*[:：]?\s*)?[¥￥]?\s*\d+(?:\.\d+)?\s*(?:CNY|RMB|元)?$",
    re.IGNORECASE,
)


class CustomSpaScraper(BaseScraper):
    scraper_type = "custom_spa"

    async def scrape(self, site_url: str, scraper_config: Dict = None) -> ScrapeResult:
        t = self._timer_start()
        products = []
        categories = []

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=BROWSER_HEADLESS)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1400, "height": 900}
                )
                page = await context.new_page()

                try:
                    await page.goto(site_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)

                    # 等待页面内容加载（SPA 需要额外时间）
                    # 等待出现价格符号或"库存"关键字
                    try:
                        await page.wait_for_function(
                            "() => document.body.innerText.includes('¥') || document.body.innerText.includes('库存') || document.body.innerText.includes('PRICE') || document.body.innerText.includes('CNY')",
                            timeout=20000
                        )
                        await page.wait_for_timeout(3000)  # 再等3秒让所有内容渲染完
                        print("[CustomSPA] 商品内容已加载")
                    except:
                        await page.wait_for_timeout(8000)
                        print("[CustomSPA] 等待超时，继续尝试解析")

                    # 滚动页面确保懒加载内容加载
                    await self._scroll_page(page)

                    # 通过 JS 提取所有商品数据
                    products, categories = await self._extract_products(page, site_url)

                finally:
                    await browser.close()

            return ScrapeResult(
                success=True,
                products=products,
                categories=categories,
                duration_seconds=self._timer_elapsed(t)
            )

        except Exception as e:
            return ScrapeResult(
                success=False,
                error_message=sanitize_log_value(e),
                duration_seconds=self._timer_elapsed(t)
            )

    async def _scroll_page(self, page: Page):
        """滚动整个页面以触发懒加载"""
        try:
            await page.evaluate("""
                async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    // 多次滚动确保所有内容加载
                    for (let pass = 0; pass < 3; pass++) {
                        const height = document.body.scrollHeight;
                        for (let y = 0; y < height; y += 400) {
                            window.scrollTo(0, y);
                            await delay(200);
                        }
                        window.scrollTo(0, document.body.scrollHeight);
                        await delay(1000);
                    }
                    window.scrollTo(0, 0);
                }
            """)
            await page.wait_for_timeout(2000)
        except:
            pass

    async def _extract_products(self, page: Page, site_url: str):
        """通过 JS 从 DOM 提取商品信息"""
        base_url = site_url.rstrip("/").split("#")[0]

        data = await page.evaluate(r"""
            () => {
                const result = { products: [], categories: [] };
                // 支持 ¥xxx / ￥xxx / PRICE xxx CNY / xxx CNY 多种价格格式
                const pricePatterns = [
                    /[¥￥]\s*(\d+(?:\.\d+)?)/,
                    /PRICE\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:CNY|RMB|元)?/i,
                    /(\d+(?:\.\d+)?)\s*CNY/i
                ];
                const stockPattern = /库存\s*[:：]?\s*(\d+)/;
                const soldPatterns = [
                    /销[量售]\s*[:：]?\s*(\d+)/,
                    /(\d+)\+?\s*已售/
                ];

                function extractPrice(text) {
                    for (const pat of pricePatterns) {
                        const m = text.match(pat);
                        if (m) return parseFloat(m[1]);
                    }
                    return null;
                }
                function hasPrice(text) {
                    return pricePatterns.some(p => p.test(text));
                }
                // 整段文本就是个价格（如 "￥130.00"、"130 CNY"）——不能当商品名
                function isPriceOnly(text) {
                    const t = (text || '').trim();
                    if (!t) return true;
                    return /^[¥￥]?\s*\d+(\.\d+)?\s*(CNY|RMB|元)?$/i.test(t)
                        || /^PRICE\s*[:：]?\s*[¥￥]?\s*\d+(\.\d+)?\s*(CNY|RMB|元)?$/i.test(t);
                }

                // ======= 策略1: 查找特定类名的商品卡片 =======
                let cards = document.querySelectorAll(
                    '.apple-product-body, .apple-product-card, ' +
                    '.df-product-card, [class*="product-card"]'
                );

                // ======= 策略2: 通用 - 查找包含价格+商品标识的适当大小元素 =======
                if (cards.length === 0) {
                    const candidates = [];
                    document.querySelectorAll('*').forEach(el => {
                        const text = el.innerText || '';
                        const html = el.innerHTML || '';
                        // 必须包含价格 + 至少一个商品标识（库存/已售/立即购买/buy）
                        const hasMarker = text.includes('库存') || text.includes('已售')
                            || text.includes('立即购买') || /buy|cart/i.test(text);
                        if (hasPrice(text) && hasMarker
                            && html.length > 100 && html.length < 5000
                            && el.children.length > 2
                            && text.length < 600) {
                            // 排除已被父级包含的子元素
                            const dominated = candidates.some(c => c.contains(el));
                            if (!dominated) {
                                // 移除被当前元素包含的已有候选
                                for (let i = candidates.length - 1; i >= 0; i--) {
                                    if (el.contains(candidates[i])) candidates.splice(i, 1);
                                }
                                candidates.push(el);
                            }
                        }
                    });
                    cards = candidates;
                }

                // ======= 解析页面级别的分类导航（上方 tab） =======
                const navCategories = [];
                document.querySelectorAll(
                    '.df-category-card, [class*="category-item"], [class*="category-tab"], ' +
                    '[class*="tab-item"], .df-category-item'
                ).forEach(tab => {
                    const t = tab.textContent.trim();
                    if (t && t.length > 1 && t.length < 20 && t !== '精选商品' && t !== '全部') {
                        navCategories.push(t);
                    }
                });

                // 通过商品名称匹配导航分类
                function matchCategory(name) {
                    for (const cat of navCategories) {
                        if (name.toLowerCase().includes(cat.toLowerCase())) return cat;
                    }
                    return null;
                }

                // ======= 解析每个卡片 =======
                const seenNames = new Set();

                cards.forEach(card => {
                    const cardText = card.innerText || '';
                    if (!hasPrice(cardText)) return;

                    // --- 提取分类 ---
                    let category = '全部';

                    // 策略1: 从 badge/tag 元素提取
                    const badgeEl = card.querySelector(
                        '[class*="badge"], [class*="tag"], [class*="label"], ' +
                        '[class*="category"], [class*="type"], span[class*="chip"]'
                    );
                    if (badgeEl) {
                        const bt = badgeEl.textContent.trim();
                        if (bt && bt.length > 1 && bt.length < 20
                            && !['自动发货', '推荐', '热销', '新品', '成品号直发', 'API Key直发', '网页号直发', 'PRICE', '官方'].includes(bt)) {
                            category = bt;
                        }
                    }

                    // 策略2: 从商品名称匹配导航分类（名称里包含分类名）
                    // 这比从文本行提取更可靠，先提取名称再匹配

                    // --- 提取商品名 ---
                    let name = '';
                    // 1. 从 h3/h4/h5/strong/标题类元素提取
                    const nameEl = card.querySelector(
                        'h3, h4, h5, strong, b, ' +
                        '[class*="title"], [class*="name"], [class*="heading"]'
                    );
                    if (nameEl) {
                        const nt = nameEl.textContent.trim();
                        // 确保不是分类名、标签或纯价格
                        if (nt && nt.length > 3 && nt !== category
                            && !['自动发货', '成品号直发', 'API Key直发', '网页号直发', 'PRICE', '官方'].includes(nt)
                            && !isPriceOnly(nt)) {
                            name = nt;
                        }
                    }
                    // 2. 备用：从文本行寻找
                    if (!name) {
                        const lines = cardText.split('\n').map(l => l.trim()).filter(l =>
                            l.length > 3 && l.length < 60
                            && !hasPrice(l)
                            && l !== category
                            && !l.includes('库存') && !l.includes('销量')
                            && !l.includes('立即') && !l.includes('数量')
                            && !l.includes('订阅时长') && !l.includes('卡密')
                            && !/^PRICE$/i.test(l) && !isPriceOnly(l)
                            && !['自动发货', '成品号直发', 'API Key直发', '网页号直发', '官方'].includes(l)
                        );
                        if (lines.length > 0) name = lines[0];
                    }

                    // 最后兜底：宁可丢掉这张卡片，也不要把价格存成商品名
                    if (!name || name.length < 2 || isPriceOnly(name)) return;

                    // 策略2: 从商品名匹配导航分类
                    if (category === '全部' && navCategories.length > 0) {
                        const matched = matchCategory(name);
                        if (matched) category = matched;
                    }

                    // 去重
                    const key = name + '|' + category;
                    if (seenNames.has(key)) return;
                    seenNames.add(key);

                    // --- 提取价格 ---
                    const price = extractPrice(cardText);

                    // --- 提取库存 ---
                    const sm = cardText.match(stockPattern);
                    const stock = sm ? parseInt(sm[1]) : -1;

                    // --- 提取销量 ---
                    let sold = 0;
                    for (const sp of soldPatterns) {
                        const sdm = cardText.match(sp);
                        if (sdm) { sold = parseInt(sdm[1]); break; }
                    }

                    // --- 购买链接 ---
                    const link = card.querySelector('a[href]');
                    const buyUrl = link ? link.href : '';

                    // 记录分类
                    if (!result.categories.includes(category)) {
                        result.categories.push(category);
                    }

                    result.products.push({
                        name, price, stock, sold, buyUrl, category
                    });
                });

                return result;
            }
        """)

        products = []
        categories = data.get("categories", [])
        rejected = 0

        for item in data.get("products", []):
            name = item.get("name", "").strip()
            if not name or len(name) < 2:
                continue
            # 与页面内脚本同一层防护：名称提取失败时价格会顶上来，这类行不要入库
            if PRICE_ONLY_RE.match(name):
                rejected += 1
                continue

            buy_url = item.get("buyUrl", "")
            if not buy_url or not buy_url.startswith("http"):
                buy_url = base_url

            products.append(ScrapedProduct(
                name=name,
                price=item.get("price"),
                original_price=f"¥{item['price']}" if item.get("price") else None,
                stock=item.get("stock", -1),
                stock_text=str(item["stock"]) if item.get("stock", -1) >= 0 else None,
                sold=item.get("sold", 0),
                buy_url=buy_url,
                category_name=item.get("category", "全部")
            ))

        print(f"[CustomSPA] 提取到 {len(products)} 个商品, {len(categories)} 个分类")
        if rejected:
            print(f"[CustomSPA] 丢弃 {rejected} 个名称被识别为纯价格的条目（页面结构可能不匹配）")
        return products, categories
