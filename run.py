#!/usr/bin/env python3
"""
AI/机器人行业招聘推送机器人

每天从 5 个平台抓取岗位，经过：
  1. 薪资过滤 (≥25K)
  2. 行业匹配 (AI/具身智能/机器人)
  3. 去重检查 (30天内不重复)
  4. 财务检查 (企查查 + 预置数据)
  5. 公司评分排序 (B轮及以上优先)
  6. 5选3精选推送
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    WECOM_WEBHOOK_URL,
    TARGET_CITIES,
    JOB_KEYWORDS,
    MIN_SALARY,
    DEDUP_DAYS,
    DATA_DIR,
    SEEN_JOBS_FILE,
    INDUSTRY_KEYWORDS,
)
from modules.company_check import check_company
from modules.storage import is_seen, mark_all_seen
from modules.formatter import format_daily_report, format_test_message
from modules.pusher import WecomPusher

os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(DATA_DIR, "logs", "run.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("bot")


# ============================================================
#  公司评分引擎
#
#  评分权重：
#    融资轮次 30% + 薪资水平 30% + 行业匹配 30% + 司法扣分 (上限5%)
#  B轮 ≧ 已上市 > A轮，40K以上薪资满分
#  城市限定：上海、杭州、苏州
# ============================================================


def _calc_company_score(job: dict, status: dict | None) -> int:
    """
    综合评分 0-100
    融资 30% + 薪资 30% + 行业 30% + 司法扣分(上限5)
    """
    score = 0
    title = job.get("title", "")
    desc = job.get("description", "")
    text = f"{title} {desc}".lower()

    # ===== 1. 融资轮次评分 (满分 30) =====
    # B轮评分最高（成长期确定性最强），上市次之，A轮居三
    if status:
        funding_raw = status.get("funding", "").strip().lower()

        if "b" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 30      # B轮：满分，成长期确定性最强
        elif "c" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 28      # C轮
        elif "已上市" in funding_raw:
            score += 26      # 上市公司：稳定但增长空间可能有限
        elif "d" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 25      # D轮
        elif "e" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 23      # E轮/Pre-IPO
        elif "pre-ipo" in funding_raw:
            score += 22
        elif "a" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 15      # A轮：早期但已有验证
        elif any(k in funding_raw for k in ["天使", "种子", "pre-a"]):
            score += 5       # 极早期，风险高
        else:
            score += 10      # 未知融资
    else:
        score += 10

    # ===== 2. 薪资水平评分 (满分 30) =====
    # ≥40K 即是满分，代表岗位价值高
    salary_max = job.get("salary_max", 0)
    if salary_max >= 40000:
        score += 30           # 40K+ 满分
    elif salary_max >= 35000:
        score += 26
    elif salary_max >= 30000:
        score += 20
    elif salary_max >= 25000:
        score += 12
    else:
        score += 5            # 低于25K给最低分

    # ===== 3. 行业匹配度评分 (满分 30) =====
    match_score = 0

    # 3a. 核心领域匹配（每条 10 分）
    core_kw = ["具身智能", "人形机器人", "embodied", "具身"]
    for kw in core_kw:
        if kw in text:
            match_score += 10
            break

    # 3b. AI/机器人广度（每条 7 分）
    ai_kw = ["大模型", "agi", "自动驾驶", "机器人", "人工智能", "深度学习"]
    for kw in ai_kw:
        if kw in text:
            match_score += 7
            break

    # 3c. 岗位类型匹配（每条 7 分）
    job_kw = ["产品经理", "产品总监", "产品负责人", "解决方案工程师", "解决方案架构师", "方案工程师"]
    for kw in job_kw:
        if kw in title:
            match_score += 7
            break

    # 3d. 技术关键词（每条 6 分）
    tech_kw = ["slam", "运动控制", "机器视觉", "强化学习", "transformer", "llm", "感知算法", "规划控制"]
    for kw in tech_kw:
        if kw in text:
            match_score += 6
            break

    score += min(30, match_score)

    # ===== 4. 司法扣分 (上限 5 分) =====
    if status:
        lawsuits = status.get("lawsuits", 0)
        deduction = 0
        if status.get("dishonesty"):
            deduction += 5
        elif status.get("zhixing"):
            deduction += 4
        elif status.get("abnormal"):
            deduction += 3
        if lawsuits >= 5:
            deduction += 3
        elif lawsuits >= 3:
            deduction += 2
        elif lawsuits >= 1:
            deduction += 1
        score -= min(5, deduction)

    return max(0, min(100, score))


# ============================================================
#  核心逻辑
# ============================================================

SCRAPERS = [
    ("Boss直聘", "scrapers.boss_zhipin", "BossZhipinScraper"),
    ("拉勾网", "scrapers.lagou", "LagouScraper"),
    ("猎聘", "scrapers.liepin", "LiepinScraper"),
    ("前程无忧", "scrapers.job51", "Job51Scraper"),
    ("智联招聘", "scrapers.zhilian", "ZhilianScraper"),
]


def _scrape_platform(mod_path: str, cls_name: str) -> list[dict]:
    import importlib
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    scraper = cls(min_delay=0.5, max_delay=1.5)
    return scraper.search_all(TARGET_CITIES, JOB_KEYWORDS)


def run_daily():
    start = time.time()
    date_str = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"🚀 岗位日报 {date_str}（目标：精选 3 个）")

    # ====== Phase 1: 5个平台全部抓完，收集所有候选 ======
    candidates = []  # 所有候选岗位（已验证通过）
    excluded_count = 0
    seen_job_ids = set()
    seen_companies = set()

    for platform_name, mod_path, cls_name in SCRAPERS:
        logger.info(f"--- {platform_name} ---")
        try:
            raw = _scrape_platform(mod_path, cls_name)
            logger.info(f"  抓到 {len(raw)} 个原始岗位")

            for job in raw:
                # 去重（30天窗口 + 本次运行内去重）
                jid = f"{platform_name}:{job.get('job_id', '')}"
                if jid in seen_job_ids or is_seen(job, SEEN_JOBS_FILE, DEDUP_DAYS):
                    continue
                seen_job_ids.add(jid)

                # 薪资过滤
                salary = max(job.get("salary_max", 0), job.get("salary_min", 0))
                if salary < MIN_SALARY:
                    continue

                # 行业过滤
                title = job.get("title", "")
                text = f"{title} {job.get('description', '')} {job.get('company_info', '')}"
                industry_ok = any(kw in text for kw in INDUSTRY_KEYWORDS)
                if not industry_ok:
                    if not any(t in title for t in ["产品经理", "解决方案", "方案工程师"]):
                        continue

                # 公司财务检查
                company = job.get("company", "").strip()
                if not company:
                    continue

                if company not in seen_companies:
                    status = check_company(company)
                    seen_companies.add(company)
                else:
                    # 同一家公司已查过，用缓存
                    status = None  # 会在下面从缓存读

                # 重新获取状态（确保同一 run 内复用）
                status = check_company(company)  # check_company 自带缓存

                if status and status.get("excluded"):
                    excluded_count += 1
                    logger.info(f"  ❌ {company}: {status.get('reason', '风险')}")
                    continue

                # 计算评分
                score = _calc_company_score(job, status)

                job["company_status"] = status
                job["_score"] = score
                candidates.append(job)

        except Exception as e:
            logger.warning(f"  {platform_name} 异常: {e}")

    logger.info(f"{'='*40}")
    logger.info(f"Phase 1 完成: {len(candidates)} 个候选 | {excluded_count} 个被财务排除")

    # ====== Phase 2: 5选3 精选 ======
    # 规则：
    #   1. 优先选 B 轮及以上（score >= 70）
    #   2. 同一家公司最多推 1 个岗位
    #   3. 按评分排序取 top 3
    #   4. 如果 B 轮以上不够 3 个，降低门槛补足

    # 按评分排序
    candidates.sort(key=lambda j: j["_score"], reverse=True)

    # 去重公司：同一公司只保留评分最高的那个岗位
    company_picks = {}
    for job in candidates:
        company = job.get("company", "").strip()
        if company not in company_picks:
            company_picks[company] = job

    # 按评分重新排序
    unique_jobs = sorted(company_picks.values(), key=lambda j: j["_score"], reverse=True)

    # 分级精选
    top_tier = [j for j in unique_jobs if j["_score"] >= 70]   # B轮及以上
    mid_tier = [j for j in unique_jobs if 50 <= j["_score"] < 70]
    low_tier = [j for j in unique_jobs if j["_score"] < 50]

    # 优先从 top_tier 选 3 个，不够再从中层补
    picks = top_tier[:3]
    if len(picks) < 3:
        picks += mid_tier[:3 - len(picks)]
    if len(picks) < 3:
        picks += low_tier[:3 - len(picks)]

    # 最终去重：确保同公司在一次推送里只出现一次
    final_picks = []
    picked_companies = set()
    for job in picks:
        c = job.get("company", "").strip()
        if c not in picked_companies:
            picked_companies.add(c)
            final_picks.append(job)
        if len(final_picks) >= 3:
            break

    # 打印精选结果
    logger.info(f"Phase 2 精选:")
    for i, job in enumerate(final_picks, 1):
        s = job["_score"]
        funding = (job.get("company_status") or {}).get("funding", "?")
        logger.info(f"  🏆 #{i} [{s}分|{funding}] {job['title']} @ {job['company']} | {job.get('salary_text','')} | {job['city']}")

    # ====== Phase 3: 推送 ======
    elapsed = time.time() - start
    logger.info(f"推送 {len(final_picks)} 个岗位 | 耗时 {elapsed:.0f}s")

    if final_picks:
        markdown = format_daily_report(
            final_picks, date_str=date_str, excluded_count=excluded_count,
            platforms=list(set(j.get("platform", "") for j in final_picks)),
            cities=TARGET_CITIES,
        )
        pusher = WecomPusher(WECOM_WEBHOOK_URL)
        if pusher.send_job_report(markdown):
            mark_all_seen(final_picks, SEEN_JOBS_FILE)
            logger.info("✅ 推送成功")
        else:
            logger.error("❌ 推送失败")
    else:
        WecomPusher(WECOM_WEBHOOK_URL).send_markdown(
            f"## 🤖 AI/机器人岗位日报 | {date_str}\n\n"
            f"> 📍 {' · '.join(TARGET_CITIES)} | ⚠️ 今日无匹配岗位\n"
            f"> 明天下午3点继续 👋"
        )


def run_test():
    ok = WecomPusher(WECOM_WEBHOOK_URL).send_markdown(format_test_message())
    print("✅ 测试发送成功" if ok else "❌ 测试发送失败")


def run_dry():
    print("Dry-run 模式...")
    for name, mod_path, cls_name in SCRAPERS[:2]:
        try:
            raw = _scrape_platform(mod_path, cls_name)
            print(f"\n{name}: {len(raw)} 个原始岗位")
            for j in raw[:3]:
                print(f"  - {j.get('title')} @ {j.get('company')} | {j.get('salary_text','')} | {j.get('city')}")
        except Exception as e:
            print(f"{name}: 失败 - {e}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.test:
        run_test()
    elif args.dry_run:
        run_dry()
    else:
        run_daily()


if __name__ == "__main__":
    main()
