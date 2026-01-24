import json
import os
import pandas as pd

# ================= 配置 =================
JSON_FILE = "daily_papers.json"
SCHOOL_CSV = "高校.csv"


# 终端颜色代码
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_bar(count, max_count, width=20):
    """打印文本进度条"""
    if max_count == 0: return ""
    bar_len = int((count / max_count) * width)
    return "█" * bar_len


def load_school_mapping():
    """读取CSV建立 实验室->学校 的映射"""
    mapping = {}
    if not os.path.exists(SCHOOL_CSV):
        return mapping

    try:
        df = pd.read_csv(SCHOOL_CSV, encoding='utf-8-sig')
        for _, row in df.iterrows():
            if pd.notna(row['实验室名']) and pd.notna(row['学校']):
                lab = str(row['实验室名']).strip()
                school = str(row['学校']).strip()
                mapping[lab] = school
    except Exception as e:
        print(f"{Colors.YELLOW}⚠️ 警告: 读取高校CSV失败，将不显示归属学校 ({e}){Colors.ENDC}")
    return mapping


def get_display_width(s):
    """计算字符串显示宽度 (中文占2格)"""
    width = 0
    for char in s:
        if '\u4e00' <= char <= '\u9fff':
            width += 2
        else:
            width += 1
    return width


def pad_string(s, width):
    """智能填充字符串以对齐"""
    display_len = get_display_width(s)
    pad_len = width - display_len
    if pad_len < 0: pad_len = 0
    return s + " " * pad_len


def main():
    # 1. 基础检查
    if not os.path.exists(JSON_FILE):
        print(f"{Colors.RED}❌ 错误: 找不到文件 {JSON_FILE}{Colors.ENDC}")
        return

    try:
        with open(JSON_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"{Colors.RED}❌ JSON 解析失败: {e}{Colors.ENDC}")
        return

    if not data:
        print(f"{Colors.YELLOW}⚠️  警告: JSON 文件为空{Colors.ENDC}")
        return

    # 2. 加载学校映射
    school_map = load_school_mapping()

    # 3. 统计逻辑
    stats = []
    unique_urls = set()
    total_attributions = 0

    for inst_name, papers in data.items():
        count = len(papers)
        if count > 0:
            # 查找归属学校
            school_name = school_map.get(inst_name, "")

            # 构建显示名称： 实验室 [学校] 或 公司名
            if school_name:
                display_name = f"{inst_name} [{school_name}]"
            else:
                display_name = inst_name  # 公司或未匹配到的实验室

            stats.append({
                "display_name": display_name,
                "raw_name": inst_name,
                "count": count
            })
            total_attributions += count
            for p in papers:
                unique_urls.add(p.get('url', ''))

    # 按数量倒序
    stats.sort(key=lambda x: x['count'], reverse=True)

    # 4. 输出总览
    print(f"\n{Colors.HEADER}{'=' * 70}")
    print(f" 📊  DAILY PAPERS 统计概览 (已添加归属学校)")
    print(f"{'=' * 70}{Colors.ENDC}")

    print(f"📅 涉及机构数 : {Colors.BOLD}{len(stats)}{Colors.ENDC}")
    print(f"🔗 总归属次数 : {Colors.BOLD}{total_attributions}{Colors.ENDC}")
    print(f"📄 实际论文数 : {Colors.GREEN}{Colors.BOLD}{len(unique_urls)}{Colors.ENDC} (去重后)")
    print(f"{'-' * 70}\n")

    # 5. 输出详细列表
    if not stats:
        print("暂无数据。")
        return

    max_val = stats[0]['count']

    # 表头
    header_name = pad_string("机构名称 [所属学校]", 50)
    print(f"{Colors.CYAN}{header_name} | {'数量':<4} | {'分布'}{Colors.ENDC}")
    print("-" * 70)

    for item in stats:
        name_str = item['display_name']

        # 截断过长名称防止爆行
        if get_display_width(name_str) > 48:
            name_str = name_str[:45] + "..."

        padded_name = pad_string(name_str, 50)
        count = item['count']
        bar = print_bar(count, max_val)

        # 高亮
        color = Colors.GREEN if count >= 3 else Colors.ENDC

        print(f"{padded_name} | {color}{count:<4}{Colors.ENDC} | {Colors.YELLOW}{bar}{Colors.ENDC}")

    print(f"{'-' * 70}\n")


if __name__ == "__main__":
    main()