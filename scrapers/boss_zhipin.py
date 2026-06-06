"""
Boss直聘爬虫（Playwright 版）
"""

import logging
import re
import time

from .base import BaseScraper

logger = logging.getLogger(__name__)

CITY_CODE = {"上海": "101020100", "杭州": "101210100", "苏州": "101190400"}


class BossZhipinScraper(BaseScraper):
    name = "boss_zhipin"

    def __init__(self, min_delay=1, max_delay=3):
        super().__init__(min_delay, max_delay)
        self._playwright = None

    def _get_browser(self):
        if self._playwright is None:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            self._playwright = (pw, browser)
        return self._playwright[1]

    def _parse_salary(self, text: str) -> tuple:
        match = re.findall(r"(\d+)\s*[Kk]", text)
        if len(match) >= 2:
            return int(match[0]) * 1000, int(match[1]) * 1000
        if len(match) == 1:
            return int(match[0]) * 1000, int(match[0]) * 1000
        return 0, 0

    def search(self, keyword: str, city: str, page: int = 1) -> list[dict]:
        if city not in CITY_CODE:
            return []

        jobs = []
        try:
            browser = self._get_browser()
            ctx = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
            pg = ctx.new_page()

            for p in range(1, page + 1):
                try:
                    url = f"https://www.zhipin.com/web/geek/job?query={keyword}&city={CITY_CODE[city]}&page={p}"
                    pg.goto(url, wait_until="domcontentloaded", timeout=20000)
                    pg.wait_for_timeout(3000)
                    pg.evaluate("window.scrollTo(0, 500)")

                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(pg.content(), "lxml")

                    for card in soup.select(".job-card-wrapper, li.job-list-item, [class*='job-card']"):
                        try:
                            t = card.select_one(".job-name, .job-title, [class*='job-name']")
                            title = t.get_text(strip=True) if t else ""
                            c = card.select_one(".company-name, [class*='company-name']")
                            company = c.get_text(strip=True) if c else ""
                            s = card.select_one(".salary, .red, [class*='salary']")
                            salary_text = s.get_text(strip=True) if s else ""
                            a = card.select_one(".job-area, [class*='job-area']")
                            area = a.get_text(strip=True) if a else city
                            l = card.select_one("a[href]")
                            link = ""
                            if l:
                                h = l.get("href", "")
                                link = f"https://www.zhipin.com{h}" if h.startswith("/") else h
                            d = card.select_one(".job-info, [class*='info-desc'], .job-desc")
                            desc = d.get_text(strip=True) if d else ""
                            tags = [x.get_text(strip=True) for x in card.select(".tag-list li")]

                            if not title or not company:
                                continue

                            smin, smax = self._parse_salary(salary_text)
                            job_id = f"boss_{hash(title + company + link) & 0x7FFFFFFF:08x}"

                            jobs.append({
                                "job_id": job_id, "title": title, "company": company,
                                "city": city, "district": area.replace(city, "").strip(),
                                "salary_min": smin, "salary_max": smax,
                                "salary_text": salary_text, "description": desc,
                                "tags": tags, "url": link,
                                "platform": "Boss直聘", "pub_date": "",
                            })
                        except Exception:
                            continue
                except Exception as e:
                    logger.warning(f"[boss] p{p} 异常: {e}")
                    break
            ctx.close()
        except Exception as e:
            logger.warning(f"[boss] Playwright: {e}")
        return jobs
