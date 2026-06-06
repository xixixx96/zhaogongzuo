"""
Boss直聘爬虫（轻量版）
使用 requests 直接抓取 Boss 直聘搜索页面。
注意：Boss直聘反爬较强，GitHub Actions 环境可能被拦截，
单平台失效不影响整体推送。
"""

import json
import logging
import re
from urllib.parse import quote

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

    def __init__(self, min_delay=5, max_delay=10):
        super().__init__(min_delay, max_delay)

    def _parse_salary(self, salary_text: str) -> tuple:
        """解析薪资文本 -> (min, max)"""
        match = re.findall(r"(\d+)\s*[Kk]", salary_text)
        if len(match) >= 2:
            return int(match[0]) * 1000, int(match[1]) * 1000
        if len(match) == 1:
            return int(match[0]) * 1000, int(match[0]) * 1000
        return 0, 0

    def search(self, keyword: str, city: str, page: int = 1) -> list[dict]:
        """
        搜索 Boss 直聘，使用网页解析方案
        """
        if city not in CITY_CODE:
            logger.warning(f"Boss直聘不支持城市: {city}")
            return []

        city_code = CITY_CODE[city]
        jobs = []

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.zhipin.com/web/geek/job",
            "Cookie": "",  # Boss 直聘对未登录用户也会返回部分数据
        }

        for p in range(1, page + 1):
            self.delay()
            try:
                url = f"https://www.zhipin.com/web/geek/job?query={quote(keyword)}&city={city_code}&page={p}"
                logger.info(f"[{self.name}] 请求: {city}-{keyword} 第{p}页")

                resp = self.session.get(url, headers=headers, timeout=15)

                if resp.status_code == 403:
                    logger.warning(f"[{self.name}] 被反爬拦截 (403)，跳过后续页面")
                    break
                if resp.status_code != 200:
                    logger.warning(f"[{self.name}] 返回 {resp.status_code}")
                    continue

                html = resp.text
                soup = self.soup(html)

                # Boss 直聘页面结构（可能随改版变化）
                cards = []
                # 尝试多种选择器
                for selector in [
                    ".job-card-wrapper",
                    ".job-list-box li",
                    "[class*='job-card']",
                    "li.job-list-item",
                ]:
                    cards = soup.select(selector)
                    if cards:
                        break

                if not cards:
                    # 尝试从页面中的 __NEXT_DATA__ 或内嵌 JSON 提取
                    json_data = self._extract_json_data(html)
                    if json_data:
                        page_jobs = self._parse_json_data(json_data, city)
                        jobs.extend(page_jobs)
                        logger.info(f"[{self.name}] 从 JSON 数据提取到 {len(page_jobs)} 个岗位")
                        if not page_jobs:
                            break
                        continue
                    else:
                        logger.info(f"[{self.name}] 第{p}页无结果或页面结构变化")
                        break

                for card in cards:
                    try:
                        title_el = card.select_one(".job-name, .job-title, [class*='job-name']")
                        title = title_el.get_text(strip=True) if title_el else ""

                        company_el = card.select_one(".company-name, .company-text, [class*='company-name']")
                        company = company_el.get_text(strip=True) if company_el else ""

                        salary_el = card.select_one(".salary, .red, [class*='salary']")
                        salary_text = salary_el.get_text(strip=True) if salary_el else ""

                        area_el = card.select_one(".job-area, [class*='job-area']")
                        area_text = area_el.get_text(strip=True) if area_el else city

                        link_el = card.select_one("a[href]")
                        link = ""
                        if link_el:
                            href = link_el.get("href", "")
                            if href.startswith("/"):
                                link = f"https://www.zhipin.com{href}"
                            elif href.startswith("http"):
                                link = href

                        desc_el = card.select_one(".job-info, .info-desc, [class*='info-desc']")
                        description = desc_el.get_text(strip=True) if desc_el else ""

                        tags_el = card.select(".tag-list li, .job-tag")
                        tags = [t.get_text(strip=True) for t in tags_el] if tags_el else []

                        if not title or not company:
                            continue

                        salary_min, salary_max = self._parse_salary(salary_text)
                        job_id = f"boss_{hash(title + company + link) & 0x7FFFFFFF:08x}"

                        jobs.append({
                            "job_id": job_id,
                            "title": title,
                            "company": company,
                            "city": city,
                            "district": area_text.replace(city, "").strip(),
                            "salary_min": salary_min,
                            "salary_max": salary_max,
                            "salary_text": salary_text,
                            "description": description,
                            "tags": tags,
                            "url": link,
                            "platform": "Boss直聘",
                            "pub_date": "",
                        })
                    except Exception as e:
                        logger.debug(f"解析 Boss直聘 卡片异常: {e}")
                        continue

                logger.info(f"[{self.name}] 第{p}页: 提取到 {len(jobs)} 个岗位")

            except Exception as e:
                logger.warning(f"[{self.name}] 第{p}页请求失败: {e}")
                break

        return jobs

    def _extract_json_data(self, html: str) -> dict | None:
        """尝试从页面中提取内嵌的 JSON 数据"""
        try:
            # 查找 window.__NEXT_DATA__ 或类似的 SSR 数据
            match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            # 查找 window.__INITIAL_STATE__
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL)
            if match:
                return json.loads(match.group(1))
        except Exception:
            pass
        return None

    def _parse_json_data(self, data: dict, city: str) -> list[dict]:
        """从 JSON 数据中提取岗位（取决于页面结构）"""
        jobs = []
        try:
            # 尝试常见的 JSON 路径
            items = []
            for path in [
                ["props", "pageProps", "jobList"],
                ["props", "pageProps", "geekJobList"],
                ["jobList"],
            ]:
                d = data
                for key in path:
                    d = d.get(key, {}) if isinstance(d, dict) else {}
                if isinstance(d, list):
                    items = d
                    break

            for item in items:
                title = item.get("jobName", "") or item.get("title", "")
                company = item.get("brandName", "") or item.get("companyName", "")
                if not title or not company:
                    continue

                salary_text = item.get("salaryDesc", "") or item.get("salary", "")
                salary_min, salary_max = self._parse_salary(salary_text)

                job_id = item.get("encryptJobId", "") or item.get("jobId", "")
                if not job_id:
                    job_id = f"boss_json_{hash(title + company) & 0x7FFFFFFF:08x}"

                link = f"https://www.zhipin.com/job_detail/{job_id}.html" if job_id else ""

                jobs.append({
                    "job_id": f"boss_{job_id}",
                    "title": title,
                    "company": company,
                    "city": city,
                    "district": item.get("areaDistrict", ""),
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "salary_text": salary_text,
                    "description": item.get("jobDesc", ""),
                    "tags": item.get("skills", []) or item.get("tags", []),
                    "url": link,
                    "platform": "Boss直聘",
                    "pub_date": "",
                })
        except Exception as e:
            logger.debug(f"JSON 数据解析失败: {e}")

        return jobs
