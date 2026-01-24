import json
import os
import datetime
from datetime import timedelta

# ================= 配置区域 =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DAILY_FILE = os.path.join(BASE_DIR, "daily_papers.json")
DB_FILE = os.path.join(BASE_DIR, "history_papers.json")
RETENTION_DAYS = 30  # 保留最近30天


def load_json(filename):
    if not os.path.exists(filename):
        return {}
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ 读取 {filename} 失败: {e}")
        return {}


def save_json(data, filename):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        file_size = os.path.getsize(filename) if os.path.exists(filename) else 0
        print(f"✅ 数据已保存至 {filename} | 文件大小: {file_size / 1024:.2f} KB")
    except Exception as e:
        print(f"❌ 保存 {filename} 失败: {e}")


def parse_date(date_str):
    """处理日期字符串，兼容 YYYY-MM-DD"""
    try:
        # 截取前10位，防止有时分秒干扰
        clean_str = str(date_str)[:10]
        return datetime.datetime.strptime(clean_str, "%Y-%m-%d")
    except ValueError:
        return datetime.datetime.now()  # 解析失败则默认为今天，防止报错


def main():
    print(">>> [Database Manager] 开始执行数据合并与维护...")

    # 1. 加载数据
    daily_data = load_json(DAILY_FILE)
    history_data = load_json(DB_FILE)

    if not daily_data:
        print("ℹ️ 今日无新数据 (daily_papers.json 为空或不存在)")
    else:
        print(f"📂 读取到今日新数据，涉及 {len(daily_data)} 个机构")

    # 2. 合并与去重
    # 数据结构假设: { "Tsinghua University": [ {paper1}, {paper2} ], ... }

    updated_count = 0

    for lab_name, new_papers in daily_data.items():
        if lab_name not in history_data:
            history_data[lab_name] = []

        # 获取该机构现有的 URL 列表，用于去重
        existing_urls = {p['url'] for p in history_data[lab_name]}

        for paper in new_papers:
            if paper['url'] not in existing_urls:
                # 插入到列表头部（最新的在前）
                history_data[lab_name].insert(0, paper)
                existing_urls.add(paper['url'])
                updated_count += 1

    print(f"➕ 新增入库论文: {updated_count} 篇")

    # 3. 维护：清理超过 30 天的数据
    print(f"🧹 执行过期数据清理 (保留最近 {RETENTION_DAYS} 天)...")

    # --- 修改后 ---
    # GitHub Actions 环境统一使用 UTC 时间进行计算，避免时区漂移带来的清理误差
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff_date = now_utc - timedelta(days=RETENTION_DAYS)

    print(f"⏰ [清理基准] 当前系统时间: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🧹 [清理基准] 论文保留截止日期: {cutoff_date.strftime('%Y-%m-%d')}")

    pruned_count = 0
    total_remaining = 0

    labs_to_remove = []

    for lab_name in history_data:
        original_len = len(history_data[lab_name])

        # 列表推导式过滤：保留 日期 >= cutoff_date 的论文
        # 注意：paper['date'] 来自 ArXiv，格式通常是 "2026-01-22"
        history_data[lab_name] = [
            p for p in history_data[lab_name]
            if parse_date(p.get('date', '')) >= cutoff_date
        ]

        current_len = len(history_data[lab_name])
        pruned_count += (original_len - current_len)
        total_remaining += current_len

        # 如果该机构没论文了，标记删除 key
        if current_len == 0:
            labs_to_remove.append(lab_name)

    # 清理空机构
    for lab in labs_to_remove:
        del history_data[lab]

    print(f"➖ 已移除过期论文: {pruned_count} 篇")
    print(f"📊 当前数据库总量: {total_remaining} 篇 (覆盖 {len(history_data)} 个机构)")

    # 4. 保存结果
    save_json(history_data, DB_FILE)


if __name__ == "__main__":
    main()