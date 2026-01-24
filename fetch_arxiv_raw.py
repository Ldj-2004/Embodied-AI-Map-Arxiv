import os
import time
import json
import re
import requests
import xml.etree.ElementTree as ET
import datetime as dt
import socket  # <--- 请确保导入了这个库
from datetime import timedelta, timezone


# ... (保留原有的 imports: os, time, json, re, requests, xml, datetime) ...

# ================= 0. 网络环境自适应配置 =================
def setup_proxy():
    """
    自动判断运行环境：GitHub Actions 环境强制直连
    """
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print(">>> [环境检测] GitHub Actions 环境：强制清除代理配置，保持直连。")
        # 显式清除可能干扰的环境变量
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        os.environ.pop("all_proxy", None)
        return

    # 2. 本地环境：探测 Clash 端口
    proxy_ip = "127.0.0.1"
    proxy_port = 7897

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)  # 快速检测
    result = sock.connect_ex((proxy_ip, proxy_port))
    sock.close()

    if result == 0:
        print(f">>> [环境检测] 本地 Clash 已开启 ({proxy_port})：正在切换至代理模式...")
        os.environ["http_proxy"] = f"http://{proxy_ip}:{proxy_port}"
        os.environ["https_proxy"] = f"http://{proxy_ip}:{proxy_port}"
    else:
        print(">>> [环境检测] 本地代理未开启或不可达：尝试直连...")


# 在脚本最开始执行配置
setup_proxy()

# ================= 配置区域 =================
# 目标日期 (YYYY-MM-DD)
# TARGET_DATE = "2026-01-23"
# --- 修改后 ---
# 自动获取当前 UTC 日期 (ArXiv OAI 接口使用 UTC 时间戳)
# 如果你发现抓取时间太早导致数据还没出来，可以改为：
# (dt.datetime.now(timezone.utc)).strftime("%Y-%m-%d")
TARGET_DATE = dt.datetime.now(timezone.utc).strftime("%Y-%m-%d")

print(f">>> [配置] 当前抓取目标日期设定为: {TARGET_DATE} (UTC)")

# 输出文件名
OUTPUT_FILE = "raw_papers.json"

# arXiv OAI 接口
OAI_BASE = "https://export.arxiv.org/oai2"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "arxiv": "http://arxiv.org/OAI/arXiv/",
}

# 请求头 (防止被 arXiv 封禁，请保持礼貌)
HEADERS = {
    "User-Agent": "Arxiv-Daily-Fetcher/1.0 (mailto:your-email@example.com)",
    "Accept-Encoding": "gzip, deflate",
}

# 目标分类
TARGET_CATEGORIES = {'cs.CV', 'cs.RO', 'cs.AI'}


# ================= 1. 辅助函数 =================

def normalize_ws(s):
    """标准化空白字符"""
    return re.sub(r"\s+", " ", s.strip()) if s else ""


def strip_version(arxiv_id):
    """去除版本号 (e.g., 2301.12345v1 -> 2301.12345)"""
    return re.sub(r"v\d+$", "", arxiv_id or "")


def clean_html_content(html):
    """
    [核心逻辑] HTML 强力清洗
    去除导航、脚本、目录、参考文献，只保留正文前部用于提取单位。
    """
    if not html: return ""

    # 1. 提取 Body 内容
    body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
    if body_match:
        html = body_match.group(1)

    # 2. 移除干扰标签 (Script, Style, Nav)
    # 移除 <script>, <style>
    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)

    # 移除 <nav> (左侧目录栏，含大量无关文字)
    html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)

    # 移除页脚、引用区域 (带有特定 class 的 div)
    # ltx_TOC: 目录
    # ltx_bibliography: 参考文献 (防止误判引用论文的单位)
    # ltx_page_footer: 页脚
    html = re.sub(r'<div[^>]*class="[^"]*(ltx_TOC|ltx_bibliography|ltx_page_footer)[^"]*"[^>]*>.*?</div>', '', html,
                  flags=re.DOTALL | re.IGNORECASE)

    # 3. 移除注释
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)

    # 4. 提取纯文本 (移除所有剩余 HTML 标签)
    text = re.sub(r'<[^>]+>', ' ', html)

    # 5. 压缩空白
    text = re.sub(r'\s+', ' ', text).strip()

    # 6. 截取前 50,000 字符 (足够覆盖 标题+作者+单位+脚注单位)
    return text[:50000]


def fetch_arxiv_html(arxiv_id):
    """爬取 arXiv HTML5 页面并清洗"""
    clean_id = strip_version(arxiv_id)
    url = f"https://arxiv.org/html/{clean_id}"

    print(f"    -> 正在抓取 HTML: {url} ...", end="", flush=True)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            cleaned_text = clean_html_content(resp.text)
            print(f" [成功] 长度: {len(cleaned_text)}")
            return cleaned_text
        elif resp.status_code == 404:
            print(" [404 Not Found] (可能未生成HTML)")
            return None
        else:
            print(f" [失败: {resp.status_code}]")
            return None
    except Exception as e:
        print(f" [错误: {e}]")
        return None


# ================= 2. XML 解析与列表获取 =================

def parse_record(record):
    """解析单条 XML 记录"""
    header = record.find("oai:header", NS)
    if header is None or header.get("status") == "deleted":
        return None

    metadata = record.find("oai:metadata", NS)
    if metadata is None: return None
    arx = metadata.find("arxiv:arXiv", NS)
    if arx is None: return None

    # 1. 提取基础信息
    arxiv_id = normalize_ws(arx.findtext("arxiv:id", default="", namespaces=NS))
    categories = arx.findtext("arxiv:categories", default="", namespaces=NS).split()

    # 2. 筛选类别
    if not any(cat in TARGET_CATEGORIES for cat in categories):
        return None

    # 3. 获取日期信息
    title = normalize_ws(arx.findtext("arxiv:title", default="", namespaces=NS))
    abstract = normalize_ws(arx.findtext("arxiv:abstract", default="", namespaces=NS))
    date_created_str = normalize_ws(arx.findtext("arxiv:created", default="", namespaces=NS))

    # ================= [核心修改：深度过滤旧论文] =================
    try:
        # A. 基于 ID 前缀的硬过滤 (ArXiv ID 格式为 YYMM.NNNNN)
        # 例如 2312.09822 对应的是 2023年12月
        target_yymm = TARGET_DATE[2:4] + TARGET_DATE[5:7]  # 从 "2026-01-22" 提取 "2601"
        id_prefix = arxiv_id[:4]  # 从 "2312.09822" 提取 "2312"

        # B. 基于日期对象的软过滤
        target_dt = dt.datetime.strptime(TARGET_DATE, "%Y-%m-%d")
        created_dt = dt.datetime.strptime(date_created_str, "%Y-%m-%d")
        diff_days = (target_dt - created_dt).days

        # 如果 ID 前缀明显属于往年，或者创建时间距离现在超过 10 天
        # (考虑到 ArXiv 可能会有 2-3 天的发布延迟，10天是一个非常安全的阈值)
        if id_prefix < target_yymm or diff_days > 10:
            print(f"  >>> [发现旧论文，不予收录] ID: {arxiv_id}, 原始发布于: {date_created_str}")
            return None

    except Exception as e:
        print(f"  [日期过滤警告] {arxiv_id} 解析失败: {e}")
    # ============================================================

    # 4. 解析作者列表
    authors_list = []
    for a in arx.findall("arxiv:authors/arxiv:author", NS):
        keyname = normalize_ws(a.findtext("arxiv:keyname", default="", namespaces=NS))
        forenames = normalize_ws(a.findtext("arxiv:forenames", default="", namespaces=NS))
        authors_list.append(f"{forenames} {keyname}")

    authors_str = ", ".join(authors_list[:5])

    return {
        "id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "date": date_created_str,
        "categories": categories,
        "authors_display": authors_str,
        "link": f"https://arxiv.org/abs/{strip_version(arxiv_id)}",
        "html_content": None
    }


def fetch_list_from_oai(target_date):
    """从 OAI 接口获取指定日期的所有论文列表"""
    print(f"=== 开始从 OAI 获取 {target_date} 的论文列表 ===")

    # OAI 的时间范围是闭区间，所以 from 和 until 都设为当天
    params = {
        "verb": "ListRecords",
        "metadataPrefix": "arXiv",
        "from": target_date,
        "until": target_date
    }

    all_records = []

    while True:
        try:
            resp = requests.get(OAI_BASE, params=params, headers=HEADERS, timeout=60)
            if resp.status_code == 503:
                print("  [503] 服务器繁忙，等待 20秒...")
                time.sleep(20)
                continue

            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            # 检查错误
            err = root.find("oai:error", NS)
            if err is not None:
                if err.get("code") == "noRecordsMatch":
                    print("  [提示] 当天没有论文记录。")
                else:
                    print(f"  [OAI Error] {err.text}")
                break

            # 解析记录
            records = root.findall(".//oai:record", NS)
            print(f"  > 下载批次: 包含 {len(records)} 条原始记录")

            for rec in records:
                paper_obj = parse_record(rec)
                if paper_obj:
                    all_records.append(paper_obj)

            # 翻页 Token
            token_node = root.find(".//oai:resumptionToken", NS)
            token = token_node.text if token_node is not None else None

            if token:
                print(f"  > 发现翻页 Token，继续获取下一页...")
                params = {"verb": "ListRecords", "resumptionToken": token}
                time.sleep(3)  # 礼貌等待
            else:
                break

        except Exception as e:
            print(f"  [网络错误] {e}")
            time.sleep(5)
            # 简单重试机制，或者直接跳出
            break

    print(f"=== 列表获取完成，共筛选出 {len(all_records)} 篇目标领域论文 ===")
    return all_records


# ================= 3. 主程序 =================

def main():
    # 1. 获取列表
    papers = fetch_list_from_oai(TARGET_DATE)

    if not papers:
        print("未获取到论文，程序结束。")
        return

    print("\n=== 开始爬取 HTML 全文 (用于提取作者单位) ===")

    # 2. 遍历列表，补充 HTML 内容
    processed_papers = []

    for i, paper in enumerate(papers):
        print(f"[{i + 1}/{len(papers)}] {paper['id']} : {paper['title'][:40]}...")

        # 爬取清洗后的全文
        html_text = fetch_arxiv_html(paper['id'])

        # 无论是否成功爬取 HTML，保留 Metadata
        # (如果 HTML 是 None，后续 LLM 只能基于 Title/Abstract 判断，精度下降但不会 Crash)
        paper['html_content'] = html_text
        processed_papers.append(paper)

        # 礼貌延时，防止 IP 被封
        time.sleep(2)

    # 3. 保存到本地 JSON
    print(f"\n=== 全部完成，正在保存到 {OUTPUT_FILE} ===")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(processed_papers, f, ensure_ascii=False, indent=2)

    print(f"文件大小: {os.path.getsize(OUTPUT_FILE) / 1024:.2f} KB")
    print("您可以将此文件上传到 GitHub 或发送到服务器进行下一步分析。")


if __name__ == "__main__":
    main()