"""
项目配置文件
所有可调整的参数集中在这里
"""

# ========== 企业微信配置 ==========
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=c0fdbe26-a140-481c-afaf-edc175b570dd"

# ========== 目标城市 ==========
TARGET_CITIES = ["上海", "杭州", "苏州"]

# ========== 岗位关键词 ==========
# 精简为核心关键词，减少无效搜索
JOB_KEYWORDS = [
    "AI产品经理",
    "机器人产品经理",
    "具身智能",
    "解决方案工程师",
    "机器人解决方案",
    "自动驾驶产品",
]

# ========== 行业限定词（用于二次匹配） ==========
INDUSTRY_KEYWORDS = [
    "AI", "人工智能", "具身智能", "机器人", "人形机器人",
    "自动驾驶", "大模型", "AGI", "智能硬件", "深度学习",
    "机器视觉", "SLAM", "运动控制", "强化学习",
]

# ========== 搜索配置 ==========
MIN_SALARY = 25000  # 最低月薪（单位：元）
MAX_PAGES_PER_PLATFORM = 1  # 每个平台只抓第一页（最新岗位，避免超时）
REQUEST_DELAY_MIN = 1  # 请求最小间隔（秒）
REQUEST_DELAY_MAX = 3  # 请求最大间隔（秒）

# ========== 薪资过滤配置 ==========
# 薪资为0（面议/未标注）的岗位是否保留
KEEP_SALARY_UNKNOWN = True  # True=保留面议岗位，不因薪资0而过滤

# ========== 企查查配置 ==========
QICHACHA_CACHE_HOURS = 24  # 公司查询缓存有效期（小时）
QICHACHA_SEARCH_URL = "https://www.qcc.com/web/search"
QICHACHA_MAX_LAWSUITS = 5  # 司法案件超过此数则排除

# ========== 推送配置 ==========
MAX_JOBS_PER_PUSH = 3
DEDUP_DAYS = 90  # 去重天数
PUSH_TIMEOUT_MINUTES = 12  # 超时兜底：超时也把已抓到的发出去

# ========== 日志配置 ==========
LOG_LEVEL = "INFO"  # DEBUG / INFO / WARNING / ERROR
LOG_FILE = "data/logs/run.log"

# ========== 数据文件路径 ==========
DATA_DIR = "data"
SEEN_JOBS_FILE = "data/seen_jobs.json"
COMPANY_CACHE_FILE = "data/company_cache.json"
