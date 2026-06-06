"""
Boss直聘爬虫（混合版）
Playwright 不可用时自动回退 requests
"""

import json
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

    def _parse_salary(self, text: str) -> tuple:
        match = re.findall(r"(\d+)\s*[Kk]", text)
        if len(match) >= 2:
            return int(match[0]) * 1000, int(match[1]) * 1000
        if len(match) == 1:
            return int(match[0]) * 1000, int(match[0]) * 1000
        return 0, 0

    def _try_requests_api(self, keyword: str, city: str, page: int) -> list[dict]:
        """回退方式：请求 Boss 直聘公开 API"""
        jobs = []
        for p in range(1, page + 1):
            try:
                url = (
                    f"https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
                    f"?query={keyword}&city={CITY_CODE[city]}&page={p}&pageSize=30"
                )
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.zhipin.com/",
                    "Accept": "application/json",
                }
                resp = self.session.get(url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    break
                data = resp.json()
                if data.get("code") != 0:
                    break
                items = data.get("zpData", {}).get("jobList", [])
                if not items:
                    break
                for item in items:
                    title = item.get("jobName", "")
                    company = item.get("brandName", "")
                    if not title or not company:
                        continue
                    salary_text = item.get("salaryDesc", "")
                    smin, smax = self._parse_salary(salary_text)
                    job_id = item.get("encryptJobId", "")
                    link = f"https://www.zhipin.com/job_detail/{job_id}.html" if job_id else ""
                    jobs.append({
                        "job_id": f"boss_{job_id}", "title": title, "company": company,
                        "city": city, "district": item.get("areaDistrict", ""),
                        "salary_min": smin, "salary_max": smax,
                        "salary_text": salary_text,
                        "description": str(item.get("jobTagList", "")),
                        "tags": item.get("skills", []) or [],
                        "url": link, "platform": "Boss直聘", "pub_date": "",
                    })
                self.delay()
            except Exception:
                break
        return jobs

    def search(self, keyword: str, city: str, page: int = 1) -> list[dict]:
        if city not in CITY_CODE:
            return []

        # 先试 requests（速度快）
        jobs = self._try_requests_api(keyword, city, page)
        logger.debug(f"[boss] API 模式 {city}-{keyword}: {len(jobs)} 个")
        return jobs
