import os
import time
import json
import re
import requests
import xml.etree.ElementTree as ET
import datetime as dt
import socket
from datetime import timedelta, timezone


# ================= 0. 网络环境自适应配置 =================
def setup_proxy():
    """GitHub Actions 强制直连；本地如果检测到 Clash 7897 则自动使用代理。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(">>> [环境检测] GitHub Actions 环境：强制清除代理配置，保持直连。")
        for key in (
            "http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        ):
            os.environ.pop(key, None)
        return

    proxy_ip = "127.0.0.1"
    proxy_port = 7897
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        result = sock.connect_ex((proxy_ip, proxy_port))
    finally:
        sock.close()

    if result == 0:
        print(f">>> [环境检测] 本地 Clash 已开启 ({proxy_port})：正在切换至代理模式...")
        os.environ["http_proxy"] = f"http://{proxy_ip}:{proxy_port}"
        os.environ["https_proxy"] = f"http://{proxy_ip}:{proxy_port}"
    else:
        print(">>> [环境检测] 本地代理未开启或不可达：尝试直连...")


setup_proxy()

# ================= 配置区域 =================
# 目标语义：抓“论文的 submission date”，而不是 OAI metadata datestamp。
#
# 例如北京时间 2026-09-01 运行：
#   优先抓取 2026-08-31 00:00--23:59 GMT 提交并已进入 arXiv API 索引的论文。
#
# 这里故意使用“北京时间昨天”，符合你的网站“每天展示上一自然日新论文”的使用方式。
BEIJING_TZ = timezone(timedelta(hours=8))
NOW_BEIJING = dt.datetime.now(BEIJING_TZ)
PREFERRED_DATE = NOW_BEIJING.date() - timedelta(days=1)

LOOKBACK_DAYS = 14
OUTPUT_FILE = "raw_papers.json"

# arXiv Atom API：submittedDate 才是按提交日期筛选论文的正确字段。
ARXIV_API_BASE = "https://export.arxiv.org/api/query"

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

HEADERS = {
    "User-Agent": "Embodied-AI-Map-Arxiv/1.0",
    "Accept": "application/atom+xml,application/xml,text/xml,*/*",
}

TARGET_CATEGORIES = {"cs.CV", "cs.RO", "cs.AI"}

# API 每次最多取 500 条；正常一天三个分类远低于上限，但仍支持分页。
API_PAGE_SIZE = 500
API_REQUEST_RETRIES = 4
API_RETRY_BASE_SECONDS = 10
API_PAGE_SLEEP_SECONDS = 3

# HTML 页面失败只影响单位/主图提取，不应该让整批论文丢失。
HTML_REQUEST_RETRIES = 2
HTML_REQUEST_TIMEOUT = 30
HTML_SLEEP_SECONDS = 2


# ================= 1. 辅助函数 =================
def normalize_ws(s):
    return re.sub(r"\s+", " ", s.strip()) if s else ""


def strip_version(arxiv_id):
    return re.sub(r"v\d+$", "", arxiv_id or "")


def save_raw(data):
    """始终覆盖 raw 文件，禁止沿用 checkout 中的旧缓存。"""
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def reset_raw_file():
    # 这是第一道保险：脚本一启动就清掉仓库 checkout 下来的历史 raw。
    save_raw([])
    print(f">>> [缓存保护] 已重置 {OUTPUT_FILE}，不会沿用仓库中的旧 raw 数据。")


def extract_best_image_url(html_content, arxiv_id):
    """从 arXiv HTML 中提取较合适的主图。"""
    if not html_content:
        return None

    clean_id = strip_version(arxiv_id)
    base_url = f"https://arxiv.org/html/{clean_id}/"

    figures = re.findall(r"<figure[^>]*>(.*?)</figure>", html_content, re.DOTALL | re.IGNORECASE)
    keywords = ["overview", "pipeline", "framework", "architecture", "methodology", "teaser"]

    best_img_src = None
    for fig in figures:
        if any(kw in fig.lower() for kw in keywords):
            img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', fig, re.IGNORECASE)
            if img_match:
                best_img_src = img_match.group(1)
                break

    if not best_img_src:
        all_imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
        filtered_imgs = [
            img for img in all_imgs
            if not any(x in img.lower() for x in ["icon", "logo", "github", "button", "external"])
        ]
        if len(filtered_imgs) >= 2:
            best_img_src = filtered_imgs[1]
        elif len(filtered_imgs) == 1:
            best_img_src = filtered_imgs[0]

    if not best_img_src:
        return None
    if best_img_src.startswith(("http://", "https://")):
        return best_img_src
    return base_url + best_img_src


def clean_html_content(html):
    """去除导航、脚本、目录、参考文献等，只保留正文前部用于作者单位匹配。"""
    if not html:
        return ""

    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    if body_match:
        html = body_match.group(1)

    html = re.sub(
        r"<(script|style|noscript)[^>]*>.*?</\1>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(
        r'<div[^>]*class="[^"]*(ltx_TOC|ltx_bibliography|ltx_page_footer)[^"]*"[^>]*>.*?</div>',
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:50000]


def fetch_arxiv_html(arxiv_id):
    """抓取 arXiv HTML5 页面。失败时返回 None，但不终止整批任务。"""
    clean_id = strip_version(arxiv_id)
    url = f"https://arxiv.org/html/{clean_id}"
    print(f"    -> 正在抓取 HTML: {url} ...", end="", flush=True)

    for attempt in range(1, HTML_REQUEST_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=HTML_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                raw_html = resp.text
                image_url = extract_best_image_url(raw_html, arxiv_id)
                cleaned_text = clean_html_content(raw_html)
                print(f" [成功] 长度: {len(cleaned_text)}, 图片: {'有' if image_url else '无'}")
                return cleaned_text, image_url
            if resp.status_code == 404:
                print(" [404 Not Found]")
                return None, None

            print(f" [HTTP {resp.status_code}, 尝试 {attempt}/{HTML_REQUEST_RETRIES}]", end="", flush=True)
        except requests.RequestException as e:
            print(f" [网络异常: {e}, 尝试 {attempt}/{HTML_REQUEST_RETRIES}]", end="", flush=True)

        if attempt < HTML_REQUEST_RETRIES:
            time.sleep(3)

    print(" [放弃 HTML，保留 OAI Metadata]")
    return None, None


# ================= 2. arXiv Atom API：按 submittedDate 获取 =================
def _parse_atom_entry(entry, source_submission_date):
    """把 arXiv Atom <entry> 转成现有 pipeline 使用的数据结构。"""
    id_url = normalize_ws(entry.findtext("atom:id", default="", namespaces=ATOM_NS))
    arxiv_id = id_url.rstrip("/").split("/")[-1]
    arxiv_id = strip_version(arxiv_id)

    title = normalize_ws(entry.findtext("atom:title", default="", namespaces=ATOM_NS))
    abstract = normalize_ws(entry.findtext("atom:summary", default="", namespaces=ATOM_NS))
    published = normalize_ws(entry.findtext("atom:published", default="", namespaces=ATOM_NS))

    # Atom published 格式通常为 2026-08-31T12:34:56Z。
    # 页面仍沿用 YYYY-MM-DD，保证 update_database / dashboard 兼容。
    date_created_str = published[:10] if published else source_submission_date

    categories = []
    for node in entry.findall("atom:category", ATOM_NS):
        term = normalize_ws(node.attrib.get("term", ""))
        if term:
            categories.append(term)

    # 再做一次本地分类保护，避免 API 布尔查询/索引异常带入无关类别。
    if not any(cat in TARGET_CATEGORIES for cat in categories):
        return None

    authors = []
    for author in entry.findall("atom:author", ATOM_NS):
        name = normalize_ws(author.findtext("atom:name", default="", namespaces=ATOM_NS))
        if name:
            authors.append(name)

    return {
        "id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "date": date_created_str,
        "categories": categories,
        "authors_display": ", ".join(authors[:5]),
        "link": f"https://arxiv.org/abs/{arxiv_id}",
        "html_content": None,

        # 新字段：真实语义是“本次按 submission date 查询的日期”。
        "source_submission_date": source_submission_date,

        # 兼容上一版 api_inference.py / generate_dashboard.py。
        # 虽然字段名还叫 source_oai_date，但现在值来自 submittedDate，
        # 不再代表 OAI metadata datestamp。
        "source_oai_date": source_submission_date,
    }


def _request_arxiv_api(params):
    """请求 arXiv Atom API。网络失败必须让 Action 失败，不能回退到旧数据。"""
    last_error = None

    for attempt in range(1, API_REQUEST_RETRIES + 1):
        try:
            resp = requests.get(
                ARXIV_API_BASE,
                params=params,
                headers=HEADERS,
                timeout=60,
            )

            if resp.status_code == 503:
                retry_after = resp.headers.get("Retry-After")
                wait_s = (
                    int(retry_after)
                    if retry_after and retry_after.isdigit()
                    else API_RETRY_BASE_SECONDS * attempt
                )
                print(
                    f"  [503] arXiv API 繁忙，{wait_s}s 后重试 "
                    f"({attempt}/{API_REQUEST_RETRIES})..."
                )
                time.sleep(wait_s)
                continue

            resp.raise_for_status()
            return ET.fromstring(resp.content)

        except (requests.RequestException, ET.ParseError) as e:
            last_error = e
            if attempt < API_REQUEST_RETRIES:
                wait_s = API_RETRY_BASE_SECONDS * attempt
                print(
                    f"  [API 请求失败] {e}；{wait_s}s 后重试 "
                    f"({attempt}/{API_REQUEST_RETRIES})..."
                )
                time.sleep(wait_s)

    raise RuntimeError(
        "arXiv API 连续请求失败，终止本次 Action，避免发布错误数据。"
        f"最后错误: {last_error}"
    )


def build_submitted_date_query(target_date):
    """
    arXiv API 的 submittedDate 使用 GMT，格式：
      submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]

    同时只抓 cs.CV / cs.RO / cs.AI。
    """
    ymd = target_date.strftime("%Y%m%d")
    date_expr = f"submittedDate:[{ymd}0000 TO {ymd}2359]"
    category_expr = "(cat:cs.CV OR cat:cs.RO OR cat:cs.AI)"
    return f"{category_expr} AND {date_expr}"


def fetch_list_by_submission_date(target_date):
    """
    精确获取某个 submission date 的目标分类论文。

    返回:
      (papers, total_results)

    注意：这里不再使用 OAI-PMH from/until。
    OAI datestamp 表示 metadata 创建/修改时间，不等同于 submission date。
    """
    target_str = target_date.strftime("%Y-%m-%d")
    search_query = build_submitted_date_query(target_date)

    print(f"\n=== 按 submittedDate 获取 {target_str} 的论文 ===")
    print(f"  > query: {search_query}")

    papers = []
    seen_ids = set()
    start = 0
    total_results = None

    while True:
        params = {
            "search_query": search_query,
            "start": start,
            "max_results": API_PAGE_SIZE,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }

        root = _request_arxiv_api(params)

        if total_results is None:
            total_text = root.findtext(
                "opensearch:totalResults",
                default="0",
                namespaces=ATOM_NS,
            )
            try:
                total_results = int(total_text)
            except (TypeError, ValueError):
                total_results = 0

            print(f"  > API totalResults = {total_results}")

        entries = root.findall("atom:entry", ATOM_NS)
        print(f"  > 本页返回 {len(entries)} 条，start={start}")

        if not entries:
            break

        for entry in entries:
            paper = _parse_atom_entry(entry, target_str)
            if not paper:
                continue

            if paper["id"] in seen_ids:
                continue
            seen_ids.add(paper["id"])
            papers.append(paper)

        start += len(entries)

        if total_results is not None and start >= total_results:
            break
        if len(entries) < API_PAGE_SIZE:
            break

        time.sleep(API_PAGE_SLEEP_SECONDS)

    print(
        f"=== {target_str} submittedDate 查询完成："
        f"API={total_results or 0}，去重后={len(papers)} 篇 ==="
    )
    return papers, (total_results or 0)


def find_latest_submission_batch():
    """
    日期策略：

    1. 永远先查询“北京时间昨天”。
       例如北京时间 2026-09-01 -> 必须先查询 2026-08-31。

    2. 如果首选日期是工作日（周一~周五）：
       cs.CV/cs.RO/cs.AI 三个大类正常情况下不可能一篇都没有。
       如果 API 返回 0，更可能是 arXiv 搜索索引尚未完成更新。
       此时直接让 Action 失败，绝不回退到 2~3 天前的旧论文。

    3. 只有首选日期本身是周末时，才允许向前回溯寻找最近有论文的日期。
    """
    print(
        f">>> [日期策略] 当前北京时间: {NOW_BEIJING.strftime('%Y-%m-%d %H:%M:%S')}；"
        f"首选 submission date: {PREFERRED_DATE}"
    )

    # ---------- 首选日期必须单独检查 ----------
    papers, total_results = fetch_list_by_submission_date(PREFERRED_DATE)

    if total_results > 0 and papers:
        print(f">>> [日期命中] 使用首选 submission date: {PREFERRED_DATE}")
        return PREFERRED_DATE.strftime("%Y-%m-%d"), papers, total_results

    # weekday(): Monday=0 ... Sunday=6
    if PREFERRED_DATE.weekday() < 5:
        raise RuntimeError(
            f"首选工作日 {PREFERRED_DATE} 的 arXiv API submittedDate 查询返回 0 篇。"
            "这通常表示当天的新论文索引尚未完成，而不是应该回退到更早日期。"
            "为避免把旧论文冒充为最新论文，本次 Action 主动终止。"
        )

    # ---------- 只有周末才允许回溯 ----------
    print(
        f">>> [周末回溯] {PREFERRED_DATE} 是周末且没有目标论文，"
        "开始向前寻找最近有论文的 submission date。"
    )

    for offset in range(1, LOOKBACK_DAYS + 1):
        candidate = PREFERRED_DATE - timedelta(days=offset)
        papers, total_results = fetch_list_by_submission_date(candidate)

        if total_results > 0 and papers:
            print(
                f">>> [日期回溯] 使用最近有论文的 submission date: {candidate} "
                f"(从 {PREFERRED_DATE} 回溯 {offset} 天)"
            )
            return candidate.strftime("%Y-%m-%d"), papers, total_results

        print(f"  [无目标论文] {candidate}，继续向前检查...")

    print(
        f"⚠️ 从 {PREFERRED_DATE} 起向前 {LOOKBACK_DAYS} 天"
        "均没有目标分类论文，将输出空 raw。"
    )
    return None, [], 0


# ================= 3. 主程序 =================
def main():
    # 第一件事仍然是清空 raw，彻底杜绝 checkout 旧缓存。
    reset_raw_file()

    batch_date, papers, total_results = find_latest_submission_batch()

    if batch_date is None:
        print("ℹ️ 没有可用 submission batch，raw_papers.json 保持为空列表。")
        return

    if not papers:
        save_raw([])
        print(
            f"ℹ️ submission date {batch_date} 没有目标分类论文；"
            f"已明确写入空 {OUTPUT_FILE}。"
        )
        return

    print(
        f"\n=== 使用 submission date {batch_date}，"
        f"开始爬取 {len(papers)} 篇 HTML 全文 ==="
    )

    processed_papers = []

    for i, paper in enumerate(papers):
        print(f"[{i + 1}/{len(papers)}] {paper['id']} : {paper['title'][:60]}...")
        html_text, teaser_image = fetch_arxiv_html(paper["id"])
        paper["html_content"] = html_text
        paper["teaser_image"] = teaser_image
        processed_papers.append(paper)
        time.sleep(HTML_SLEEP_SECONDS)

    save_raw(processed_papers)

    file_size = os.path.getsize(OUTPUT_FILE) / 1024
    print(
        f"\n✅ 抓取完成：submission_date={batch_date}, "
        f"papers={len(processed_papers)}"
    )
    print(f"✅ 已覆盖保存 {OUTPUT_FILE} | {file_size:.2f} KB")


if __name__ == "__main__":
    main()
