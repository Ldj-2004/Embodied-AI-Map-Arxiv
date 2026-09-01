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
# GitHub Actions 在北京时间 12:25（UTC 04:25）运行。
# 工作日优先抓当天；周末/无记录日自动向前回溯最近一次 arXiv OAI 有记录的日期。
TODAY_UTC = dt.datetime.now(timezone.utc).date()
LOOKBACK_DAYS = 14
MAX_CREATED_AGE_DAYS = 10

OUTPUT_FILE = "raw_papers.json"
OAI_BASE = "https://export.arxiv.org/oai2"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "arxiv": "http://arxiv.org/OAI/arXiv/",
}

HEADERS = {
    # 建议替换成你自己的项目名/联系邮箱；比伪装浏览器 UA 更符合 arXiv 的礼貌抓取习惯。
    "User-Agent": "Embodied-AI-Map-Arxiv/1.0",
    "Accept": "text/xml,application/xml,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

TARGET_CATEGORIES = {"cs.CV", "cs.RO", "cs.AI"}

# OAI 网络请求失败时：绝不当作“今天 0 篇”继续执行，而是让 GitHub Action 失败。
OAI_REQUEST_RETRIES = 4
OAI_RETRY_BASE_SECONDS = 10

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


# ================= 2. XML 解析与 OAI 获取 =================
def parse_record(record, source_oai_date):
    """解析一条 OAI record，并过滤明显不是近期新提交的旧论文更新。"""
    header = record.find("oai:header", NS)
    if header is None or header.get("status") == "deleted":
        return None

    metadata = record.find("oai:metadata", NS)
    if metadata is None:
        return None
    arx = metadata.find("arxiv:arXiv", NS)
    if arx is None:
        return None

    arxiv_id = normalize_ws(arx.findtext("arxiv:id", default="", namespaces=NS))
    categories = arx.findtext("arxiv:categories", default="", namespaces=NS).split()
    if not any(cat in TARGET_CATEGORIES for cat in categories):
        return None

    title = normalize_ws(arx.findtext("arxiv:title", default="", namespaces=NS))
    abstract = normalize_ws(arx.findtext("arxiv:abstract", default="", namespaces=NS))
    date_created_str = normalize_ws(arx.findtext("arxiv:created", default="", namespaces=NS))

    # 原代码用 arXiv ID 的 YYMM 前缀比较，会在每月 1 号错误过滤上月末论文。
    # 这里只比较真实 created 日期与当前 OAI 批次日期。
    try:
        batch_dt = dt.datetime.strptime(source_oai_date, "%Y-%m-%d").date()
        created_dt = dt.datetime.strptime(date_created_str, "%Y-%m-%d").date()
        diff_days = (batch_dt - created_dt).days
        if diff_days < 0 or diff_days > MAX_CREATED_AGE_DAYS:
            print(
                f"  >>> [过滤旧/异常论文] ID: {arxiv_id}, created={date_created_str}, "
                f"OAI batch={source_oai_date}, diff={diff_days}d"
            )
            return None
    except ValueError as e:
        print(f"  [日期解析警告] {arxiv_id}: {e}，为安全起见跳过该记录。")
        return None

    authors_list = []
    for a in arx.findall("arxiv:authors/arxiv:author", NS):
        keyname = normalize_ws(a.findtext("arxiv:keyname", default="", namespaces=NS))
        forenames = normalize_ws(a.findtext("arxiv:forenames", default="", namespaces=NS))
        authors_list.append(f"{forenames} {keyname}".strip())

    return {
        "id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "date": date_created_str,
        "categories": categories,
        "authors_display": ", ".join(authors_list[:5]),
        "link": f"https://arxiv.org/abs/{strip_version(arxiv_id)}",
        "html_content": None,
        # 第二道保险：Inference 会检查这个批次字段，旧 legacy raw 没有它就拒绝处理。
        "source_oai_date": source_oai_date,
    }


def _request_oai(params):
    """OAI 请求：网络/协议错误最终抛异常，不得伪装成 noRecordsMatch。"""
    last_error = None
    for attempt in range(1, OAI_REQUEST_RETRIES + 1):
        try:
            resp = requests.get(OAI_BASE, params=params, headers=HEADERS, timeout=60)
            if resp.status_code == 503:
                retry_after = resp.headers.get("Retry-After")
                wait_s = int(retry_after) if retry_after and retry_after.isdigit() else OAI_RETRY_BASE_SECONDS * attempt
                print(f"  [503] arXiv OAI 繁忙，{wait_s}s 后重试 ({attempt}/{OAI_REQUEST_RETRIES})...")
                time.sleep(wait_s)
                continue

            resp.raise_for_status()
            return ET.fromstring(resp.content)
        except (requests.RequestException, ET.ParseError) as e:
            last_error = e
            if attempt < OAI_REQUEST_RETRIES:
                wait_s = OAI_RETRY_BASE_SECONDS * attempt
                print(f"  [OAI 请求失败] {e}；{wait_s}s 后重试 ({attempt}/{OAI_REQUEST_RETRIES})...")
                time.sleep(wait_s)

    raise RuntimeError(f"arXiv OAI 连续请求失败，终止本次 Action，避免发布错误数据。最后错误: {last_error}")


def fetch_list_from_oai(target_date):
    """
    获取某个 OAI datestamp 的记录。

    返回: (status, papers, raw_record_count)
      status='ok'         : OAI 有记录（即使目标分类最终筛成 0 篇）
      status='no_records' : OAI 明确返回 noRecordsMatch，可安全回溯前一天
    其他 OAI 错误直接抛异常。
    """
    print(f"\n=== 开始从 OAI 获取 {target_date} 的论文列表 ===")

    params = {
        "verb": "ListRecords",
        "metadataPrefix": "arXiv",
        "from": target_date,
        "until": target_date,
    }

    all_records = []
    raw_record_count = 0

    while True:
        root = _request_oai(params)

        err = root.find("oai:error", NS)
        if err is not None:
            code = err.get("code")
            if code == "noRecordsMatch":
                print(f"  [提示] {target_date} 没有 OAI 记录。")
                return "no_records", [], 0
            raise RuntimeError(f"arXiv OAI 返回错误 {code}: {err.text}")

        records = root.findall(".//oai:record", NS)
        raw_record_count += len(records)
        print(f"  > 下载批次: 包含 {len(records)} 条原始记录")

        for rec in records:
            paper_obj = parse_record(rec, target_date)
            if paper_obj:
                all_records.append(paper_obj)

        token_node = root.find(".//oai:resumptionToken", NS)
        token = token_node.text.strip() if token_node is not None and token_node.text else None
        if token:
            print("  > 发现翻页 Token，继续获取下一页...")
            params = {"verb": "ListRecords", "resumptionToken": token}
            time.sleep(3)
        else:
            break

    print(
        f"=== {target_date} 获取完成：原始 {raw_record_count} 条，"
        f"筛选后 {len(all_records)} 篇目标领域论文 ==="
    )
    return "ok", all_records, raw_record_count


def find_latest_available_batch():
    """从今天开始向前寻找最近一个 OAI 明确存在记录的日期。"""
    print(
        f">>> [配置] 当前 UTC 日期: {TODAY_UTC}；"
        f"最多向前回溯 {LOOKBACK_DAYS} 天寻找最近有效 arXiv OAI 批次。"
    )

    for offset in range(LOOKBACK_DAYS + 1):
        candidate = TODAY_UTC - timedelta(days=offset)
        candidate_str = candidate.strftime("%Y-%m-%d")
        status, papers, raw_count = fetch_list_from_oai(candidate_str)
        if status == "ok":
            if offset > 0:
                print(f">>> [日期回溯] 今天无记录，改用最近有效批次: {candidate_str} (回溯 {offset} 天)")
            return candidate_str, papers, raw_count

    # 连续 LOOKBACK_DAYS 天都明确 noRecordsMatch，属于异常但不是网络错误。
    # 写空文件，避免任何旧缓存被使用，然后正常结束。
    print(f"⚠️ 最近 {LOOKBACK_DAYS + 1} 天均无 OAI 记录，将输出空 raw。")
    return None, [], 0


# ================= 3. 主程序 =================
def main():
    # 最重要的一步：任何网络请求之前先抹掉 checkout 下来的旧 raw。
    reset_raw_file()

    batch_date, papers, raw_count = find_latest_available_batch()

    if batch_date is None:
        print("ℹ️ 没有可用 OAI 批次，raw_papers.json 保持为空列表。")
        return

    if not papers:
        # 这里和“网络失败”不同：OAI 批次存在，只是 cs.CV/cs.RO/cs.AI + 新论文过滤后为 0。
        save_raw([])
        print(
            f"ℹ️ OAI 批次 {batch_date} 存在 {raw_count} 条记录，但目标领域筛选后为 0；"
            f"已明确写入空 {OUTPUT_FILE}。"
        )
        return

    print(f"\n=== 使用 OAI 批次 {batch_date}，开始爬取 {len(papers)} 篇 HTML 全文 ===")
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
    print(f"\n✅ 抓取完成：batch={batch_date}, papers={len(processed_papers)}")
    print(f"✅ 已覆盖保存 {OUTPUT_FILE} | {file_size:.2f} KB")


if __name__ == "__main__":
    main()
