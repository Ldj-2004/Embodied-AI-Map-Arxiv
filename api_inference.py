import os
import json
import pandas as pd
import time
from openai import OpenAI
import concurrent.futures
from tqdm import tqdm

# --- 修改后 ---
if os.environ.get('GITHUB_ACTIONS') == 'true':
    print(">>> [环境检测] GitHub Actions 环境：清除代理配置。")
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
else:
    # 仅在本地开发且没在脚本外设置代理时才手动指定
    os.environ['http_proxy'] = 'http://127.0.0.1:7897'
    os.environ['https_proxy'] = 'http://127.0.0.1:7897'

# ================= 配置区域 =================

# --- 修改后 ---
# 优先从环境变量读取，如果读取不到（本地调试）再使用默认值（不推荐，建议本地也设环境变量）
API_BASE = os.environ.get("OPENAI_API_BASE", "http://35.220.164.252:3888/v1/")
API_KEY = os.environ.get("OPENAI_API_KEY")

if not API_KEY:
    print("⚠️ 警告: 未检测到 OPENAI_API_KEY 环境变量。")
    # 如果是本地运行，可以在这里临时写一个备用 Key，但上传前务必删除
    # API_KEY = "sk-xxxx"

# 模型选择：性价比之选
MODEL_NAME = "gpt-4o-mini"

# 并发数 (API通常支持高并发，建议 20-50)
MAX_WORKERS = 50

# 输入输出
INPUT_FILE = "raw_papers.json"
OUTPUT_FILE = "daily_papers.json"
SCHOOL_CSV = "高校.csv"
COMPANY_CSV = "公司.csv"

# 设置一个全局调试开关
DEBUG_SAVE_ABSTRACT = True
DEBUG_MODE = True

# ================= 1. 数据加载与规则构建 (复用逻辑) =================

class DataManager:
    def __init__(self):
        self.inst_map = {}  # 关键词 -> {标准名, 类型}
        self.person_rules = set()  # 大牛名单
        self.load_rules()

    def load_rules(self):
        print("📊 [Init] 正在加载机构与人员规则...")
        # 1. 加载高校
        try:
            if os.path.exists(SCHOOL_CSV):
                df_school = pd.read_csv(SCHOOL_CSV, encoding='utf-8-sig')
                for _, row in df_school.iterrows():
                    if pd.notna(row.get('Institution_Keywords')) and pd.notna(row.get('实验室名')):
                        keywords = str(row['Institution_Keywords']).split(';')
                        lab_name = str(row['实验室名']).strip()
                        for k in keywords:
                            k_clean = k.strip().lower()
                            if k_clean:
                                if k_clean not in self.inst_map: self.inst_map[k_clean] = set()
                                self.inst_map[k_clean].add(lab_name)
                    if pd.notna(row.get('英文名')):
                        people = [p.strip().lower() for p in str(row['英文名']).split(';') if p.strip()]
                        self.person_rules.update(people)
        except Exception as e:
            print(f"⚠️ 加载高校数据失败: {e}")

        # 2. 加载公司
        try:
            if os.path.exists(COMPANY_CSV):
                df_comp = pd.read_csv(COMPANY_CSV, encoding='utf-8-sig')
                for _, row in df_comp.iterrows():
                    if pd.notna(row.get('English_Keywords')) and pd.notna(row.get('公司名')):
                        keywords = str(row['English_Keywords']).split(';')
                        comp_name = str(row['公司名']).strip()
                        for k in keywords:
                            k_clean = k.strip().lower()
                            if k_clean:
                                if k_clean not in self.inst_map: self.inst_map[k_clean] = set()
                                self.inst_map[k_clean].add(comp_name)
        except Exception as e:
            print(f"⚠️ 加载公司数据失败: {e}")

        print(f"✅ 规则加载完毕: 监控 {len(self.inst_map)} 个机构关键词")

    def check_highlight(self, authors_str):
        if not authors_str: return False
        auth_lower = authors_str.lower()
        for p in self.person_rules:
            if p in auth_lower:
                return True
        return False


import requests
import re

def fetch_full_abstract(arxiv_url):
    """从 ArXiv abs 页面抓取完整的摘要"""
    try:
        # 将 /abs/ 替换为 /abs/ (以防万一)
        resp = requests.get(arxiv_url, timeout=15, verify=False)
        if resp.status_code == 200:
            # 使用正则匹配 <blockquote class="abstract mathjax"> ... </blockquote>
            match = re.search(r'<blockquote class="abstract mathjax">.*?<span class="descriptor">Abstract:</span>(.*?)</blockquote>', resp.text, re.DOTALL)
            if match:
                abstract = match.group(1).strip()
                # 去除可能的 HTML 标签或多余换行
                return re.sub(r'<.*?>', '', abstract).replace('\n', ' ')
    except Exception as e:
        print(f"⚠️ 抓取摘要失败 {arxiv_url}: {e}")
    return None

# ================= 2. API 调用核心 =================

# --- 修改后 ---
if not API_KEY:
    raise ValueError("❌ 错误: 必须设置 OPENAI_API_KEY 才能运行。在 GitHub Actions 中请设置 Secrets。")

client = OpenAI(base_url=API_BASE, api_key=API_KEY)


def call_llm(system_prompt, user_prompt, max_tokens=5): # 修改默认参数
    """通用 API 调用函数"""
    retries = 3
    for i in range(retries):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3, # 稍微提高随机性利于评分区分
                max_tokens=max_tokens, # 使用传入的参数
                timeout=20
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if i == retries - 1: return "NO"
            time.sleep(1)


# ================= 3. 两阶段推理逻辑 =================

def filter_by_topic(raw_papers):
    """Stage 1: 混合话题筛选 (关键词白名单 + 宽松 LLM)"""
    print(f"\n🧠 [Stage 1] 话题筛选 (Hybrid High-Recall Mode)...")

    # === 策略 A: 关键词白名单 (直接放行，不消耗 Token) ===
    # 这些词出现任何一个，绝对是具身智能/机器人相关，无需 AI 犹豫
    WHITELIST_KEYWORDS = [
        "robot", "manipulat", "embodied", "humanoid", "locomotion",
        "navigation", "actuator", "sensorimotor", "teleoperation",
        "end-to-end control", "sim-to-real", "policy learning", "robotic",
        "dexterous", "gripper", "quadruped", "bipedal", "mobile agent", "vision-language-action"
    ]

    # === 策略 B: 宽松的 LLM 判别 (针对 VLA, World Model 等边缘地带) ===
    system_prompt = """You are a research paper filter.
Target: Papers relevant to Embodied AI, Robotics, OR their foundation technologies.

ACCEPT if the paper is about:
1. Robotics (Hardware, Control, Planning).
2. Embodied AI / Agents in environments.
3. Computer Vision (3D, Depth, Scene Understanding, Tracking).
4. AI Foundation Models (LLM/VLM) *IF* they imply reasoning, planning, or spatial understanding.
5. Reinforcement Learning.

REJECT only if completely unrelated (e.g., pure cryptography, pure database optimization, biology).

Output: "YES" or "NO"."""

    def process_one(paper):
        text_content = (paper['title'] + " " + paper['abstract']).lower()

        # 1. 白名单检查 (极速通道)
        for kw in WHITELIST_KEYWORDS:
            if kw in text_content:
                # 这是一个强相关论文，直接保留，不需要问 LLM
                return paper

        # 2. LLM 检查 (兜底通道)
        # 针对那些没写 "robot" 但写了 "world model" 或 "agent" 的论文
        user_prompt = f"Title: {paper['title']}\nAbstract: {paper['abstract'][:1500]}\nIs this relevant to AI/Robotics?"
        res = call_llm(system_prompt, user_prompt)

        if "YES" in res:
            return paper
        else:
            # 调试打印，看看谁被杀掉了 (可选)
            # print(f"  [丢弃] {paper['title'][:30]}...")
            return None

    relevant_papers = []

    # 使用并发处理
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(tqdm(executor.map(process_one, raw_papers), total=len(raw_papers), unit="paper", disable=os.environ.get('GITHUB_ACTIONS') == 'true'))

    relevant_papers = [p for p in results if p]

    print(f"✅ 话题筛选完成: {len(raw_papers)} -> {len(relevant_papers)} 篇 (含白名单直通)")
    return relevant_papers


def verify_affiliations(relevant_papers, dm):
    """Stage 2: 机构归属验证 (规则+LLM 混合双打版)"""
    print(f"\n🔍 [Stage 2] 机构归属验证 (Hybrid Rules + LLM)...")

    # Prompt 简化，只作为兜底
    system_prompt = """Check if the Candidate appears in the Author Affiliation section.
Output:
[YES] <Candidate>
[NO] <Candidate>
"""

    tasks = []

    # 调试目标
    DEBUG_TITLE_KEY = "Collision-Free"

    paper_verified_labs = {}

    # 统计计数
    count_rule_pass = 0
    count_llm_check = 0

    for paper in relevant_papers:
        html_text = paper.get('html_content', '')
        if not html_text: continue
        html_lower = html_text.lower()

        # 1. 预处理：构建关键词映射
        candidate_kws = set()
        kw_to_labs_map = {}

        for kw, target_labs in dm.inst_map.items():
            if kw in html_lower:
                candidate_kws.add(kw)
                kw_to_labs_map[kw] = target_labs

        if not candidate_kws: continue

        # =========================================================
        # 核心逻辑变更：规则优先，LLM 辅助
        # =========================================================

        confirmed_labs_for_this_paper = set()
        llm_check_list = []  # 需要交给 LLM 确认的（在 Header 深处的）

        # 定义 Header 的核心区域 (前 800 字符通常包含所有核心单位)
        # 如果关键词直接出现在这里，我们直接收录，不问 LLM 了 (省钱 + 防漏)
        header_head = html_lower[:800]

        is_debug = DEBUG_MODE and DEBUG_TITLE_KEY in paper['title']
        if is_debug:
            print(f"\n🐞 [DEBUG] 论文: {paper['title']}")
            print(f"   命中关键词: {list(candidate_kws)}")

        for kw in candidate_kws:
            # 规则 1: 强匹配 (如果关键词在前 800 字符，直接由于)
            if kw in header_head:
                labs = kw_to_labs_map[kw]
                confirmed_labs_for_this_paper.update(labs)
                count_rule_pass += 1
                if is_debug: print(f"   ✅ [规则通过] {kw} (在开头出现)")
            else:
                # 规则 2: 如果在后面，加入待查列表
                llm_check_list.append(kw)

        # 如果还有需要 LLM 确认的，生成 Task
        if llm_check_list:
            count_llm_check += 1
            candidates_str = "\n".join([f"- {c}" for c in llm_check_list])
            context = html_text[:5000]  # 给 LLM 看长一点

            user_prompt = f"""
Paper: {paper['title']}
Candidates:
{candidates_str}

Text:
{context}

Check each candidate. Return [YES] or [NO]."""

            tasks.append({
                "paper": paper,
                "candidates": llm_check_list,  # 只传需要确认的
                "kw_map": kw_to_labs_map,
                "current_labs": confirmed_labs_for_this_paper,  # 已通过规则确认的
                "prompt": user_prompt
            })
        else:
            # 如果所有候选都在规则 1 就通过了，直接保存
            if confirmed_labs_for_this_paper:
                url = paper['link']
                if url not in paper_verified_labs:
                    paper_verified_labs[url] = {"paper": paper, "labs": set()}
                paper_verified_labs[url]["labs"].update(confirmed_labs_for_this_paper)

    # 处理 LLM 任务
    if tasks:
        print(f"⚡ {count_rule_pass} 项规则直通，剩余 {len(tasks)} 个请求需 LLM 确认...")

        def process_task(task):
            is_debug = DEBUG_MODE and DEBUG_TITLE_KEY in task['paper']['title']
            confirmed = task['current_labs']  # 继承规则确认的

            try:
                res_str = call_llm(system_prompt, task['prompt'], max_tokens=300)

                if is_debug:
                    print(f"   📝 LLM 回复:\n{res_str}")

                for line in res_str.split('\n'):
                    clean_line = line.strip()
                    if clean_line.startswith("[YES]"):
                        extracted_kw = clean_line.replace("[YES]", "").strip().lower()  # 关键：强制转小写

                        # 修复大小写匹配问题
                        for cand_kw in task['candidates']:
                            # 全小写对比
                            if cand_kw in extracted_kw or extracted_kw in cand_kw:
                                labs = task['kw_map'][cand_kw]
                                confirmed.update(labs)
                                if is_debug: print(f"   ✅ [LLM确认] {cand_kw}")

            except Exception as e:
                print(f"Error: {e}")

            if confirmed:
                return {"paper": task['paper'], "confirmed": confirmed}
            return None

        # 执行
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            results = list(tqdm(executor.map(process_task, tasks), total=len(tasks), disable=os.environ.get('GITHUB_ACTIONS') == 'true'))

        for res in results:
            if res:
                url = res['paper']['link']
                if url not in paper_verified_labs:
                    paper_verified_labs[url] = {"paper": res['paper'], "labs": set()}
                paper_verified_labs[url]["labs"].update(res['confirmed'])

    print(f"✅ 最终保留: {len(paper_verified_labs)} 篇")
    return paper_verified_labs




def analyze_paper_quality(verified_data):
    """Stage 3: 深度评估 - 逐篇总结 + 统一排序打分"""
    if not verified_data: return {}

    # --- Part A: 逐篇生成总结 (Summary Only) ---
    print(f"\n📝 [Stage 3a] 正在生成 {len(verified_data)} 篇论文的精简总结...")

    # 极简 Prompt，只负责总结，不负责打分
    summary_prompt = "You are a robotics expert. Summarize this paper in ONE dense sentence (max 25 words)."

    def process_summary(item):
        paper = item['paper']
        full_abs = fetch_full_abstract(paper['link'])
        final_abstract = full_abs if full_abs else paper['abstract']
        if DEBUG_SAVE_ABSTRACT: item['abstract_full'] = final_abstract

        # 只生成总结，max_tokens 设小
        user_prompt = f"Title: {paper['title']}\nAbstract: {final_abstract}"
        item['ai_summary'] = call_llm(summary_prompt, user_prompt, max_tokens=60)
        return item

    # 并发处理总结
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        list(tqdm(executor.map(process_summary, verified_data.values()), total=len(verified_data), disable=os.environ.get('GITHUB_ACTIONS') == 'true'))

    # --- Part B: 统一排序打分 (The Secret Sauce) ---
    print(f"⚖️ [Stage 3b] 正在对全员进行竞争性排序打分...")

    # 构造列表发给 LLM
    paper_list_str = ""
    urls = list(verified_data.keys())
    for i, url in enumerate(urls):
        paper_list_str += f"[{i}] {verified_data[url]['paper']['title']}\n"

    ranking_system_prompt = """You are a judge for the "Embodied AI & Robotics" top conference.
Rank the following papers based on their RELEVANCE and CONTRIBUTION to Embodied AI (Physical World Agents).
Scoring Criteria (0-100):
High Score (90+): Real-robot results, sim-to-real transfer, VLA (Vision-Language-Action) for control, world models/planning tied to actions.
Mid Score (80-89): Vision/NLP/ML methods clearly enabling embodied tasks (perception->action, navigation, manipulation) with strong evidence of transferability.
Low Score (<80): General AI methods with unclear or indirect link to physical agents, no action/control loop, or no credible robotics pathway.
Use the full range when appropriate; avoid clustering scores. Assign a UNIQUE score to each paper. NO TIES.
Output format: [Index] Score"""

    # 整个列表只发一次 API 请求！
    rank_res = call_llm(ranking_system_prompt, paper_list_str, max_tokens=500)

    # 解析排序结果 [Index] Score
    for line in rank_res.split('\n'):
        match = re.search(r"\[(\d+)\]\s*([\d\.]+)", line)
        if match:
            idx = int(match.group(1))
            score = float(match.group(2))
            if idx < len(urls):
                verified_data[urls[idx]]['ai_score'] = score

    return verified_data



# ================= 4. 主程序 =================

def main():
    # 检查输入
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 找不到 {INPUT_FILE}，请先运行抓取脚本 fetch_arxiv_raw.py")
        return

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        raw_papers = json.load(f)

    print(f"📂 读取到 {len(raw_papers)} 篇待处理论文")

    # 1. 加载规则
    dm = DataManager()

    # 2. Stage 1: 话题筛选
    embodied_papers = filter_by_topic(raw_papers)

    # 3. Stage 2: 机构验证
    verified_data = verify_affiliations(embodied_papers, dm)

    # ============ [新增] Stage 3: 打分与摘要 ============
    # 只有通过了 Stage 2 的论文才会进入这里
    if verified_data:
        verified_data = analyze_paper_quality(verified_data)
    # ==================================================

    # 4. Stage 3: 输出结果
    print("\n💾 [Final] 生成最终数据库...")
    final_db = {}
    count = 0

    for url, item in verified_data.items():
        paper = item['paper']
        labs = item['labs']

        # 获取 Stage 3 产生的新字段，如果没有则给默认值
        ai_score = item.get('ai_score', 0)
        ai_summary = item.get('ai_summary', paper['abstract'][:100] + '...')

        is_highlight = dm.check_highlight(paper.get('authors_display', ''))

        paper_info = {
            "title": paper['title'],
            "url": paper['link'],
            "date": paper['date'],
            "authors_text": paper.get('authors_display', ''),
            "is_highlight": is_highlight,
            "score": item.get('ai_score', 0),
            "summary": item.get('ai_summary', ""),  # 存入 AI 生成的精简摘要
        }

        # 方便调试：如果开关打开，把完整摘要也存进 daily_papers.json
        if DEBUG_SAVE_ABSTRACT:
            paper_info["debug_abstract"] = item.get('abstract_full', "")

        for lab in labs:
            if lab not in final_db:
                final_db[lab] = []

            # 去重
            if not any(p['url'] == paper_info['url'] for p in final_db[lab]):
                final_db[lab].insert(0, paper_info)
                count += 1

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_db, f, ensure_ascii=False, indent=2)

    print(f"🎉 API 处理完成！")
    print(f"   共收录: {count} 条")
    print(f"   结果已保存: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()