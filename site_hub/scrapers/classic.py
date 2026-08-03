"""
独角数卡 Classic 适配器

适用网站：store.dimosky.com, shop.efuncard.com, kk.aimizy.com,
          auto-subscribe.com, card4399.com, ultrapifa.xqe1.asia
          以及 Myuki 主题的表格布局站点如 gemini91.shop

这些网站使用服务端渲染 + JS 动态加载商品数据。
支持两种布局：
  - 标准布局：分类标签 → 商品卡片网格
  - Myuki 布局：左侧分类列表 → 右侧商品表格
"""
import re
import json
import asyncio
from typing import Any, Dict, List
from bs4 import BeautifulSoup, FeatureNotFound
try:
    from playwright.async_api import async_playwright, Page, Browser
except ImportError:  # API/data-only installations can still start without browser extras.
    async_playwright = None
    Page = Browser = Any
from site_hub.scrapers.models import ScrapeResult, ScrapedProduct
from site_hub.scrapers.base import BaseScraper
from site_hub.scrapers.compat_config import BROWSER_HEADLESS, BROWSER_TIMEOUT
from site_hub.scrapers.network import sanitize_log_value


class ClassicScraper(BaseScraper):
    scraper_type = "classic"

    async def scrape(self, site_url: str, scraper_config: Dict = None) -> ScrapeResult:
        t = self._timer_start()
        products = []
        categories = []

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=BROWSER_HEADLESS)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                page = await context.new_page()

                try:
                    # 加载首页 - 使用 domcontentloaded 避免有持续网络请求的站点卡住
                    await page.goto(site_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)

                    # 智能等待：并行检测所有商品选择器，总共最多等15秒
                    matched = await page.evaluate("""
                        () => new Promise(resolve => {
                            const selectors = [
                                "[class*='col-lg-3'][class*='col-md-6']",
                                ".goods-title", ".goods-item", ".product-card",
                                ".commodity-list tr", "a.chip", ".switch-category"
                            ];
                            // 先检查是否已经存在
                            for (const sel of selectors) {
                                if (document.querySelector(sel)) return resolve(sel);
                            }
                            // MutationObserver 监听 DOM 变化
                            const observer = new MutationObserver(() => {
                                for (const sel of selectors) {
                                    if (document.querySelector(sel)) {
                                        observer.disconnect();
                                        return resolve(sel);
                                    }
                                }
                            });
                            observer.observe(document.body, { childList: true, subtree: true });
                            // 15秒超时
                            setTimeout(() => { observer.disconnect(); resolve(null); }, 15000);
                        })
                    """)
                    if matched:
                        await page.wait_for_timeout(2000)  # 再等2秒让内容渲染完
                        print(f"[Classic] 内容已加载 (匹配: {matched})")
                    else:
                        await page.wait_for_timeout(3000)
                        print("[Classic] 未匹配已知选择器，等待后继续")

                    # 关闭可能的弹窗
                    await self._close_popups(page)

                    # 检测页面布局类型
                    layout = await self._detect_layout(page)
                    print(f"[Classic] 检测到布局类型: {layout}")

                    if layout == "myuki_table":
                        products, categories = await self._scrape_myuki_layout(page, site_url)
                    else:
                        products, categories = await self._scrape_standard_layout(page, site_url)

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

    async def _close_popups(self, page: Page):
        """关闭页面上的弹窗"""
        popup_selectors = [
            "#myuki-gcard_close",         # Myuki 主题弹窗
            "button:has-text('关闭窗口')",
            "button:has-text('我知道了')",
            "button:has-text('关闭')",
            ".modal .close",
            ".popup-close",
        ]
        for sel in popup_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(500)
            except:
                pass

    async def _detect_layout(self, page: Page) -> str:
        """检测页面布局类型"""
        # 检查 Myuki 表格布局的特征
        myuki_indicators = [
            ".commodity-list",          # 商品列表表格
            ".category-list",           # 分类列表
            "td.commodity-click",       # 购买按钮
            "#myuki-gcard",             # Myuki 弹窗
        ]
        for sel in myuki_indicators:
            el = await page.query_selector(sel)
            if el:
                return "myuki_table"

        # 检查左侧分类 + 右侧选择商品的布局（Myuki 主题的另一个标志）
        has_side_categories = await page.query_selector("text=商品分类")
        has_select_products = await page.query_selector("text=选择商品")
        if has_side_categories and has_select_products:
            return "myuki_table"

        return "standard"

    # ============ Myuki 表格布局 ============

    async def _scrape_myuki_layout(self, page: Page, site_url: str):
        """Myuki 主题的表格布局抓取"""
        products = []
        categories = []
        base_url = site_url.rstrip("/")

        # 使用 JS 直接提取数据（更可靠）
        data = await page.evaluate(r"""
            () => {
                const result = { categories: [], products: [] };

                // 提取分类 - 查找左侧所有分类链接
                const catContainer = document.querySelector('.category-list')
                    || document.querySelector('[class*="category"]');

                if (catContainer) {
                    const catEls = catContainer.querySelectorAll('tr, li, a, .category-item');
                    catEls.forEach(el => {
                        const text = el.innerText?.trim();
                        if (text && text.length > 1 && text !== '商品分类'
                            && !result.categories.includes(text)) {
                            result.categories.push(text);
                        }
                    });
                }

                // 如果还没找到分类，找包含分类关键字的区域
                if (result.categories.length === 0) {
                    const allLinks = document.querySelectorAll('a, td, li');
                    const catSection = document.querySelector('[class*="cat"]')
                        || document.querySelector('.sidebar');
                    if (catSection) {
                        catSection.querySelectorAll('*').forEach(el => {
                            if (el.children.length === 0 || el.tagName === 'A') {
                                const t = el.innerText?.trim();
                                if (t && t.length > 1 && t.length < 20
                                    && !['商品分类', '搜索商品', '选择商品'].includes(t)
                                    && !result.categories.includes(t)) {
                                    result.categories.push(t);
                                }
                            }
                        });
                    }
                }

                return result;
            }
        """)

        categories = data.get("categories", [])
        print(f"[Classic/Myuki] 发现 {len(categories)} 个分类: {categories}")

        # 逐个点击分类取商品
        for cat_name in categories:
            try:
                # 点击分类
                cat_el = await page.query_selector(f"text={cat_name}")
                if not cat_el:
                    # 尝试其他方式定位
                    cat_els = await page.query_selector_all("td, a, li")
                    for el in cat_els:
                        text = (await el.inner_text()).strip()
                        if text == cat_name:
                            cat_el = el
                            break

                if cat_el:
                    await cat_el.click()
                    await page.wait_for_timeout(2000)

                    # 从当前页面提取商品数据
                    cat_products = await self._extract_myuki_products(page, base_url, cat_name)
                    products.extend(cat_products)
                    print(f"[Classic/Myuki] 分类 '{cat_name}': {len(cat_products)} 个商品")

            except Exception as e:
                print(f"[Classic/Myuki] 分类 '{cat_name}' 出错: {e}")
                continue

        return products, categories

    async def _extract_myuki_products(self, page: Page, base_url: str, category_name: str) -> List[ScrapedProduct]:
        """从 Myuki 表格布局提取商品"""
        data = await page.evaluate(r"""
            () => {
                const products = [];
                const pricePattern = /[¥￥]\s*(\d+(?:\.\d+)?)/;

                // 方法1: 查找商品表格行
                const rows = document.querySelectorAll(
                    '.commodity-list tr, table tr, .product-row, .goods-row'
                );

                rows.forEach(row => {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 3) return;

                    const text = row.innerText || '';
                    // 跳过表头
                    if (text.includes('商品名称') && text.includes('单价')) return;
                    if (text.includes('库存') && text.includes('操作') && text.includes('商品名称')) return;

                    // 提取名称 - 通常在第1或第2个td
                    let name = '';
                    for (let i = 0; i < Math.min(cells.length, 3); i++) {
                        const cellText = cells[i].innerText?.trim();
                        if (cellText && cellText.length > 5 && !pricePattern.test(cellText)) {
                            name = cellText;
                            break;
                        }
                    }
                    if (!name) return;

                    // 提取价格
                    const priceMatch = text.match(pricePattern);
                    const price = priceMatch ? parseFloat(priceMatch[1]) : null;

                    // 提取库存 - 找数字（非价格）
                    let stock = -1;
                    for (let i = cells.length - 1; i >= 0; i--) {
                        const cellText = cells[i].innerText?.trim();
                        if (/^\d+$/.test(cellText) && !pricePattern.test(cellText)) {
                            stock = parseInt(cellText);
                            break;
                        }
                    }

                    // 提取链接
                    const link = row.querySelector('a[href]');
                    let buyUrl = '';
                    if (link) buyUrl = link.href;

                    products.push({
                        name: name,
                        price: price,
                        stock: stock,
                        buyUrl: buyUrl
                    });
                });

                // 方法2: 如果表格没找到，尝试找卡片式商品
                if (products.length === 0) {
                    const cards = document.querySelectorAll(
                        '.commodity-item, .product-item, [class*="goods"]'
                    );
                    cards.forEach(card => {
                        const text = card.innerText || '';
                        const nameEl = card.querySelector('h5, h4, h3, .title, .name, p');
                        if (!nameEl) return;
                        const name = nameEl.innerText?.trim();
                        if (!name || name.length < 3) return;

                        const priceMatch = text.match(pricePattern);
                        const link = card.querySelector('a[href]');

                        products.push({
                            name: name,
                            price: priceMatch ? parseFloat(priceMatch[1]) : null,
                            stock: -1,
                            buyUrl: link ? link.href : ''
                        });
                    });
                }

                return products;
            }
        """)

        products = []
        for item in data:
            name = item.get("name", "").strip()
            if not name or len(name) < 3:
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
                sold=0,
                buy_url=buy_url,
                category_name=category_name
            ))

        return products

    # ============ 标准卡片布局 ============

    async def _scrape_standard_layout(self, page: Page, site_url: str):
        """标准独角数卡卡片网格布局"""
        products = []
        categories = []

        # 获取分类标签
        cat_links = await page.query_selector_all(
            "a.chip, .goods-category a, .category-item a, .nav-pills a, "
            ".switch-category, .shop-nav a, .category a"
        )

        # 获取分类名称
        for link in cat_links:
            text = (await link.inner_text()).strip()
            if text and text not in categories:
                categories.append(text)

        if cat_links:
            for i, link in enumerate(cat_links):
                # 先给个占位名：inner_text() 本身就可能抛异常（元素在点击后失效），
                # 这时 except 里引用未绑定的 cat_name 会变成 NameError 盖掉真实错误
                cat_name = f"#{i + 1}"
                try:
                    cat_name = (await link.inner_text()).strip() or cat_name
                    await link.click()
                    await page.wait_for_timeout(1500)
                    cat_products = await self._parse_standard_products(page, site_url, cat_name)
                    products.extend(cat_products)
                except Exception as e:
                    print(f"[Classic] 解析分类 '{cat_name}' 出错: {e}")
                    continue
        else:
            products = await self._parse_standard_products(page, site_url, "全部")

        return products, categories

    async def _parse_standard_products(self, page: Page, site_url: str, category_name: str) -> List[ScrapedProduct]:
        """解析标准布局的商品卡片"""
        products = []
        html = await page.content()
        try:
            soup = BeautifulSoup(html, "lxml")
        except FeatureNotFound:
            soup = BeautifulSoup(html, "html.parser")

        # 商品卡片选择器 - 多种变体
        card_selectors = [
            "div.col-lg-3.col-md-6",
            ".goods-item",
            ".product-card",
            ".card.product",
            ".acg-card",
            "a[href*='/product/']",
            "a[href*='/item/']",
        ]

        cards = []
        for selector in card_selectors:
            cards = soup.select(selector)
            if cards:
                break

        # 备用方案：通过 .goods-title 找父级卡片容器
        if not cards:
            title_els = soup.select(".goods-title")
            if title_els:
                seen = set()
                for t in title_els:
                    parent = t.parent
                    for _ in range(5):
                        if parent and parent.get("class"):
                            classes = " ".join(parent.get("class", []))
                            if "col-" in classes:
                                pid = id(parent)
                                if pid not in seen:
                                    seen.add(pid)
                                    cards.append(parent)
                                break
                        if parent:
                            parent = parent.parent

        # 备用方案：通过属性选择器匹配 col 类
        if not cards:
            cards = soup.select("[class*='col-lg-3'][class*='col-md-6']")

        if not cards:
            for el in soup.find_all(string=re.compile(r'[¥￥]\s*\d+')):
                parent = el.find_parent(["div", "a"], class_=True)
                if parent and parent not in cards:
                    cards.append(parent)

        base_url = site_url.rstrip("/")

        for card in cards:
            try:
                product = self._parse_single_card(card, base_url, category_name)
                if product:
                    if not any(p.name == product.name and p.category_name == product.category_name for p in products):
                        products.append(product)
            except Exception as e:
                print(f"[Classic] 解析商品卡片出错: {e}")
                continue

        # 最终备用：如果 BeautifulSoup 解析失败，用 Playwright JS 直接从 DOM 提取
        if not products:
            js_products = await self._extract_products_via_js(page, base_url, category_name)
            if js_products:
                products = js_products
                print(f"[Classic] 通过 JS 备用方案提取 {len(products)} 个商品")

        return products

    async def _extract_products_via_js(self, page: Page, base_url: str, category_name: str) -> List[ScrapedProduct]:
        """通过 Playwright JS 直接从 DOM 提取商品（备用方案）"""
        data = await page.evaluate(r"""
            () => {
                const products = [];
                const pricePattern = /[¥￥]\s*(\d+(?:\.\d+)?)/;
                // 「库存：50已售：1382」是连写的，\S+ 会把销量一起吞掉，所以数字优先单独匹配
                const stockNumPattern = /库存[：:]\s*(\d+)/;
                const stockWordPattern = /库存[：:]\s*([^\s\d]{1,6}?)(?=已售|销量|$|\s)/;
                const soldPattern = /已售[：:]\s*(\d+)/;

                // 查找所有 .goods-title 元素作为商品锚点
                const titles = document.querySelectorAll('.goods-title');
                titles.forEach(titleEl => {
                    // 向上找卡片容器
                    let card = titleEl.closest('[class*="col-"]') || titleEl.parentElement?.parentElement;
                    if (!card) return;

                    const text = card.innerText || '';
                    const name = titleEl.innerText?.trim();
                    if (!name || name.length < 2) return;

                    const priceMatch = text.match(pricePattern);
                    const stockNum = text.match(stockNumPattern);
                    const stockWord = stockNum ? null : text.match(stockWordPattern);
                    const soldMatch = text.match(soldPattern);
                    const link = card.querySelector('a[href]');

                    products.push({
                        name: name,
                        price: priceMatch ? parseFloat(priceMatch[1]) : null,
                        stock: stockNum ? parseInt(stockNum[1])
                             : (stockWord && ['无', '缺货', '售罄'].includes(stockWord[1]) ? 0 : -1),
                        stockText: stockNum ? stockNum[1] : (stockWord ? stockWord[1] : null),
                        sold: soldMatch ? parseInt(soldMatch[1]) : 0,
                        buyUrl: link ? link.href : ''
                    });
                });

                return products;
            }
        """)

        products = []
        for item in data:
            name = item.get("name", "").strip()
            # 清理标签文字
            for tag in ["自动发货", "推荐", "热销", "新品", "在线发货"]:
                name = name.replace(tag, "").strip()
            if not name or len(name) < 2:
                continue

            buy_url = item.get("buyUrl", "")
            if not buy_url or not buy_url.startswith("http"):
                buy_url = base_url

            products.append(ScrapedProduct(
                name=name,
                price=item.get("price"),
                original_price=f"¥{item['price']}" if item.get("price") else None,
                stock=item.get("stock", -1),
                stock_text=item.get("stockText"),
                sold=item.get("sold", 0),
                buy_url=buy_url,
                category_name=category_name
            ))

        return products

    def _parse_single_card(self, card, base_url: str, category_name: str):
        """解析单个商品卡片"""
        # 商品名称
        name = None
        name_selectors = [".product-title", ".goods-title", "h5", "h4", ".card-title", "p"]
        for sel in name_selectors:
            el = card.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text and len(text) > 2:
                    name = text
                    break
        if not name:
            text = card.get_text(strip=True)
            if text and len(text) > 5:
                lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 3]
                if lines:
                    name = lines[0][:100]

        if not name:
            return None

        # 去掉标签文字
        for tag_text in ["自动发货", "推荐", "热销", "新品", "限时"]:
            name = name.replace(tag_text, "").strip()
        if not name:
            return None

        # 价格
        price = None
        original_price = None
        price_text = card.get_text()
        price_match = re.search(r'[¥￥]\s*(\d+(?:\.\d+)?)', price_text)
        if price_match:
            price = float(price_match.group(1))
            original_price = f"¥{price_match.group(1)}"

        # 库存
        # 卡片文本常是「库存：50已售：1382」这种没有分隔的连写，
        # 用 \S+ 会把销量一起吞掉导致数字解析失败，所以先单独匹配数字。
        stock = -1
        stock_text = None
        num_match = re.search(r'库存[：:]\s*(\d+)', price_text)
        if num_match:
            stock = int(num_match.group(1))
            stock_text = num_match.group(1)
        else:
            word_match = re.search(r'库存[：:]\s*([^\s\d]{1,6}?)(?=已售|销量|$|\s)', price_text)
            if word_match:
                stock_val = word_match.group(1)
                stock_text = stock_val
                if stock_val in ["无", "缺货", "售罄"]:
                    stock = 0

        # 已售
        sold = 0
        sold_match = re.search(r'已售[：:]\s*(\d+)', price_text)
        if sold_match:
            sold = int(sold_match.group(1))

        # 购买链接
        buy_url = None
        link = card.find("a", href=True)
        if link:
            href = link.get("href", "")
            if href.startswith("/"):
                buy_url = base_url + href
            elif href.startswith("http"):
                buy_url = href
        if not buy_url:
            if card.name == "a" and card.get("href"):
                href = card["href"]
                if href.startswith("/"):
                    buy_url = base_url + href
                elif href.startswith("http"):
                    buy_url = href

        # 标签
        tags = []
        for tag_class in [".badge", ".tag", ".label", "span.text-primary", "span.text-success"]:
            tag_els = card.select(tag_class)
            for t in tag_els:
                tt = t.get_text(strip=True)
                if tt and tt not in tags and len(tt) < 10:
                    tags.append(tt)

        return ScrapedProduct(
            name=name,
            price=price,
            original_price=original_price,
            stock=stock,
            stock_text=stock_text,
            sold=sold,
            buy_url=buy_url or base_url,
            tags=tags,
            category_name=category_name
        )
