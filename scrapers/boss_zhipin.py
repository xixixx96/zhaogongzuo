"""
Boss直聘爬虫（API版）
使用 Boss 直聘公开搜索 API，不需要登录。
"""

import json
import logging
import re
import time

from .base import BaseScraper

logger = logging.getLogger(__name__)

# Boss直聘城市编码
CITY_CODE = {
    "上海": "101020100",
    "杭州": "101210100",
    "苏州": "101190400",
}


class BossZhipinScraper(BaseScraper):
    name = "boss_zhipin"

    def __init__(self, min_delay=1, max_delay=3):
        super().__init__(min_delay, max_delay)
        self._playwright = None

    def _get_browser(self):
        if self._playwright is None:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            self._playwright = (pw, browser)
        return self._playwright[1]

    def _parse_salary(self, salary_text: str) -> tuple:
        match = re.findall(r"(\d+)\s*[Kk]", salary_text)
        if len(match) >= 2:
            return int(match[0]) * 1000, int(match[1]) * 1000
        if len(match) == 1:
            return int(match[0]) * 1000, int(match[0]) * 1000
        return 0, 0

    def search(self, keyword: str, city: str, page: int = 1) -> list[dict]:
        if city not in CITY_CODE:
            return []

        city_code = CITY_CODE[city]
        jobs = []

        try:
            browser = self._get_browser()
            ctx = browser.new_context(locale="zh-CN")
            page_obj = ctx.new_page()

            for p in range(1, page + 1):
                try:
                    url = f"https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}&page={p}"
                    page_obj.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page_obj.wait_for_timeout(2000)  # 等JS渲染

                    html = page_obj.content()
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, "lxml")

                    cards = soup.select(".job-card-wrapper, li.job-list-item, [class*='job-card']")
                    for card in cards:
                        title_el = card.select_one(".job-name, .job-title, [class*='job-name']")
                        title = title_el.get_text(strip=True) if title_el else ""
                        company_el = card.select_one(".company-name, [class*='company-name']")
                        company = company_el.get_text(strip=True) if company_el else ""
                        salary_el = card.select_one(".salary, .red, [class*='salary']")
                        salary_text = salary_el.get_text(strip=True) if salary_el else ""
                        area_el = card.select_one(".job-area, [class*='job-area']")
                        area = area_el.get_text(strip=True) if area_el else city
                        link_el = card.select_one("a[href]")
                        link = ""
                        if link_el:
                            href = link_el.get("href", "")
                            link = f"https://www.zhipin.com{href}" if href.startswith("/") else href
                        desc_el = card.select_one(".job-info, [class*='info-desc']")
                        desc = desc_el.get_text(strip=True) if desc_el else ""
                        tags_el = card.select(".tag-list li")
                        tags = [t.get_text(strip=True) for t in tags_el] if tags_el else []

                        if not title or not company:
                            continue

                        smin, smax = self._parse_salary(salary_text)
                        job_id = f"boss_{hash(title + company + link) & 0x7FFFFFFF:08x}"

                        jobs.append({
                            "job_id": job_id, "title": title, "company": company,
                            "city": city, "district": area.replace(city, "").strip(),
                            "salary_min": smin, "salary_max": smax,
                            "salary_text": salary_text,
                            "description": desc, "tags": tags,
                            "url": link, "platform": "Boss直聘", "pub_date": "",
                        })

                except Exception as e:
                    logger.debug(f"[boss] p{p} 异常: {e}")
                    break

            ctx.close()

        except Exception as e:
            logger.warning(f"[boss] Playwright 启动失败: {e}")

        return jobs
