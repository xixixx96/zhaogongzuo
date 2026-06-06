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
    KEEP_SALARY_UNKNOWN,
    MAX_JOBS_PER_PUSH,
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
#    融资轮次 30% + 薪资水平 30% + 行业匹配 30% + 司法扣分 5% + 投资机构 5%
#  B轮=30 > C轮=28 > 上市=25 > A轮=15，40K+薪资满分
#  方向：具身智能 > 车企解决方案 > AI/大模型（排除自动驾驶）
#  城市限定：上海、杭州、苏州
#  失信/被执行公司直接跳过，不进入候选
# ============================================================

# 顶级VC + 产业资本名录
TOP_INVESTORS = [
    "红杉", "高瓴", "idg", "经纬", "蓝驰", "源码", "五源", "高榕", "真格",
    "启明", "ggv", "dst", "淡马锡", "软银", "今日资本", "创新工场",
    "顺为", "云锋", "baillie", "accel", "鼎晖", "深创投", "元禾",
    # 产业资本
    "腾讯", "阿里巴巴", "阿里", "百度", "字节", "美团", "小米", "华为",
    "比亚迪", "宁德时代", "上汽", "丰田", "奔驰", "博世", "菜鸟",
    "联想创投", "复星", "国投", "国开",
]


def _has_top_investors(investors_str: str) -> bool:
    if not investors_str:
        return False
    s = investors_str.lower()
    return any(i.lower() in s for i in TOP_INVESTORS)


def _gen_push_reason(job: dict, status: dict | None, score: int) -> str:
    """生成推送理由"""
    parts = []

    # 行业方向
    title = job.get("title", "")
    if any(kw in title for kw in ["具身智能", "人形机器人", "embodied"]):
        parts.append("具身智能核心岗位")
    elif any(kw in title for kw in ["解决方案工程师", "解决方案架构师", "方案工程师"]):
        parts.append("解决方案岗位，匹配你背景")
    elif any(kw in title for kw in ["产品经理", "产品总监"]):
        parts.append("AI产品岗位")

    # 融资信息
    if status:
        funding = status.get("funding", "")
        funding_amount = status.get("funding_amount", "")
        if funding and funding_amount:
            parts.append(f"{funding}（{funding_amount}）")
        elif funding:
            parts.append(f"{funding}")

        # 知名投资机构
        investors = status.get("investors", "")
        if investors and _has_top_investors(investors):
            parts.append(f"🏆 顶级机构背书：{investors}")

    # 薪资优势
    salary_max = job.get("salary_max", 0)
    if salary_max >= 40000:
        parts.append("高薪岗位（40K+）")
    elif salary_max >= 30000:
        parts.append("薪资有竞争力")

    # 公司稳定性
    if status:
        lawsuits = status.get("lawsuits", 0)
        if lawsuits == 0:
            parts.append("零司法风险")
        elif lawsuits <= 2:
            parts.append("司法风险极低")

    reason = "；".join(parts) if parts else "综合评分优秀"
    return reason


def _calc_company_score(job: dict, status: dict | None) -> int:
    """
    综合评分 0-100
    融资 30% + 薪资 30% + 行业 30% + 司法扣分 5% + 投资机构 5%
    """
    score = 0
    title = job.get("title", "")
    desc = job.get("description", "")
    text = f"{title} {desc}".lower()

    # ===== 1. 融资轮次评分 (满分 30) =====
    if status:
        funding_raw = status.get("funding", "").strip().lower()

        if "b" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 30      # B轮：产品验证+资金充足+高增长
        elif "c" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 28      # C轮
        elif "已上市" in funding_raw:
            score += 25      # 上市：最不会倒，增长空间略低
        elif "d" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 25      # D轮
        elif "e" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 23
        elif "pre-ipo" in funding_raw:
            score += 22
        elif "a" in funding_raw and ("轮" in funding_raw or "+" in funding_raw):
            score += 15      # A轮：早期但已验证
        elif any(k in funding_raw for k in ["天使", "种子", "pre-a"]):
            score += 5
        else:
            score += 10
    else:
        score += 10

    # ===== 2. 薪资水平评分 (满分 30) =====
    salary_max = job.get("salary_max", 0)
    if salary_max >= 40000:
        score += 30
    elif salary_max >= 35000:
        score += 25
    elif salary_max >= 30000:
        score += 20
    elif salary_max >= 25000:
        score += 15
    else:
        score += 5

    # ===== 3. 行业匹配度评分 (满分 30) =====
    match_score = 0

    # 3a. 核心领域-具身智能（10分）
    core_kw = ["具身智能", "人形机器人", "embodied", "具身"]
    for kw in core_kw:
        if kw in text:
            match_score += 10
            break

    # 3b. AI/机器人广度（7分）
    ai_kw = ["大模型", "agi", "机器人", "人工智能", "深度学习", "智能硬件"]
    for kw in ai_kw:
        if kw in text:
            match_score += 7
            break

    # 3c. 岗位类型匹配（7分）
    job_kw = [
        "解决方案工程师", "解决方案架构师", "方案工程师",
        "产品经理", "产品总监", "产品负责人",
    ]
    for kw in job_kw:
        if kw in title:
            match_score += 7
            break

    # 3d. 技术关键词（6分）
    tech_kw = ["slam", "运动控制", "机器视觉", "强化学习", "transformer", "llm", "感知算法", "规划控制"]
    for kw in tech_kw:
        if kw in text:
            match_score += 6
            break

    score += min(30, match_score)

    # ===== 4. 司法扣分 (上限 5) =====
    if status:
        lawsuits = status.get("lawsuits", 0)
        deduction = 0
        if status.get("abnormal"):
            deduction += 3
        if lawsuits >= 5:
            deduction += 3
        elif lawsuits >= 3:
            deduction += 2
        elif lawsuits >= 1:
            deduction += 1
        score -= min(5, deduction)

    # ===== 5. 知名投资机构加分 (上限 5) =====
    if status:
        investors = status.get("investors", "")
        if _has_top_investors(investors):
            # 多个顶级机构多加
            count = sum(1 for i in TOP_INVESTORS if i.lower() in investors.lower())
            if count >= 3:
                score += 5
            elif count >= 2:
                score += 4
            elif count >= 1:
                score += 2

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

SINGLE_PLATFORM_TIMEOUT = 120  # 单平台最多120秒


def _scrape_with_timeout(mod_path: str, cls_name: str) -> list[dict]:
    """带超时的单平台抓取"""
    name = cls_name.replace("Scraper", "")
    logger.info(f"[{name}] 开始...")
    t0 = time.time()
    try:
        import importlib
        mod = importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
        scraper = cls(min_delay=0.3, max_delay=1.0)
        results = scraper.search_all(TARGET_CITIES, JOB_KEYWORDS)
        logger.info(f"[{name}] 完成: {len(results)} 个 ({time.time()-t0:.0f}s)")
        return results
    except Exception as e:
        logger.error(f"[{name}] 异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []


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
        raw = []
        try:
            with ThreadPoolExecutor(max_workers=1) as p:
                fut = p.submit(_scrape_with_timeout, mod_path, cls_name)
                raw = fut.result(timeout=SINGLE_PLATFORM_TIMEOUT)
        except OSError:
            # win32 特殊异常
            pass
        except Exception as e:
            reason = "超时" if "timeout" in str(e).lower() else str(e)[:80]
            logger.warning(f"  {platform_name} 跳过: {reason}")

        logger.info(f"  抓到 {len(raw)} 个原始岗位")

        for job in raw:
            try:
                # 去重（30天窗口 + 本次运行内去重）
                jid = f"{platform_name}:{job.get('job_id', '')}"
                if jid in seen_job_ids or is_seen(job, SEEN_JOBS_FILE, DEDUP_DAYS):
                    continue
                seen_job_ids.add(jid)

                # 薪资过滤（面议岗位放行）
                salary = max(job.get("salary_max", 0), job.get("salary_min", 0))
                if salary < MIN_SALARY and not (KEEP_SALARY_UNKNOWN and salary == 0):
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

                status = check_company(company)  # check_company 自带缓存

                # 硬排除：失信 / 被执行 直接跳过
                if status:
                    if status.get("dishonesty"):
                        excluded_count += 1
                        logger.info(f"  ❌ {company}: 失信被执行人，直接排除")
                        continue
                    if status.get("zhixing"):
                        excluded_count += 1
                        logger.info(f"  ❌ {company}: 有被执行记录，直接排除")
                        continue
                    if status.get("excluded"):
                        excluded_count += 1
                        logger.info(f"  ❌ {company}: {status.get('reason', '财务风险')}")
                        continue

                # 计算评分
                score = _calc_company_score(job, status)

                job["company_status"] = status
                job["_score"] = score
                candidates.append(job)
            except Exception as e:
                logger.warning(f"  处理岗位异常: {e}")

    logger.info(f"{'='*40}")
    logger.info(f"Phase 1 完成: {len(candidates)} 个候选 | {excluded_count} 个被财务排除 | {len(seen_job_ids)} 个原始岗位")

    if not candidates:
        logger.warning("所有平台均未抓到符合条件的数据 → 可能是爬虫被拦截或关键词不匹配")
        logger.warning(f"已尝试 {len(SCRAPERS)} 个平台, {len(TARGET_CITIES)} 个城市, {len(JOB_KEYWORDS)} 个关键词")

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

    # 优先从 top_tier 选 MAX_JOBS_PER_PUSH 个，不够再从中层补
    picks = top_tier[:MAX_JOBS_PER_PUSH]
    if len(picks) < MAX_JOBS_PER_PUSH:
        picks += mid_tier[:MAX_JOBS_PER_PUSH - len(picks)]
    if len(picks) < MAX_JOBS_PER_PUSH:
        picks += low_tier[:MAX_JOBS_PER_PUSH - len(picks)]

    # 最终去重：确保同公司在一次推送里只出现一次
    final_picks = []
    picked_companies = set()
    for job in picks:
        c = job.get("company", "").strip()
        if c not in picked_companies:
            picked_companies.add(c)
            final_picks.append(job)
        if len(final_picks) >= MAX_JOBS_PER_PUSH:
            break

    # 为精选岗位生成推送理由
    for job in final_picks:
        job["_reason"] = _gen_push_reason(job, job.get("company_status"), job["_score"])

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
            raw = _scrape_with_timeout(mod_path, cls_name)
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
