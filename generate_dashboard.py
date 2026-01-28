import pandas as pd
import folium
from folium.plugins import MarkerCluster, HeatMap
import random
import html
import datetime
import json
import os


# ================= 1. 视觉与配置系统 =================

# 修复后的深色主题 (包含所有必要的键)
# 不再是死黑，而是带有金属质感的深蓝灰色
# ================= 1. 视觉与配置系统 =================

THEME = {
    "bg_root": "#020617",  # 极夜黑蓝 (Slate 950)
    "bg_panel": "rgba(15, 23, 42, 0.95)",
    "border": "1px solid rgba(56, 189, 248, 0.2)",
    "text_primary": "#f8fafc",
    "text_secondary": "#94a3b8",

    # 核心配色 - 确保高饱和度，在深色地图上醒目
    "accent_uni": "#00e5ff",  # 高校: 赛博青 (Cyan A400)
    "accent_comp": "#ff9100",  # 公司: 活力橙 (Orange A400)

    # 兼容键
    "accent_cyan": "#00e5ff",
    "accent_purple": "#ff9100",
    "accent_gold": "#ffd740",

    "card_bg": "rgba(30, 41, 59, 0.7)",
    "success": "#00e676",
    "danger": "#ff1744"
}

REGION_CONFIG = {
    "China": {"loc": [35.0, 105.0], "zoom": 4},
    "USA": {"loc": [38.0, -97.0], "zoom": 4},
    "Europe": {"loc": [50.0, 10.0], "zoom": 4},
}


def smart_wrap(text, limit=40):
    """智能换行，保持HTML标签安全"""
    if not text or str(text).lower() == 'nan': return "暂无介绍"
    text = str(text)
    out = ""
    count = 0
    for char in text:
        out += char
        count += 2 if '\u4e00' <= char <= '\u9fff' else 1
        if count >= limit:
            out += "<br>"
            count = 0
    return out


# ================= 2. 数据处理引擎 =================

class DataEngine:
    def __init__(self):
        self.uni_groups = {}
        self.companies = []
        self.heat_data = []
        self.hot_papers = []
        self.history_data = {}
        self.daily_data = {}
        self.lab_to_parent = {}  # [新增] 实验室到学校/公司的映射字典

    def _load_json_db(self, filename):
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error reading {filename}: {e}")
        return {}

    def load_data(self):
        print(">>> 正在加载并处理数据...")

        self.history_data = self._load_json_db('history_papers.json')
        self.daily_data = self._load_json_db('daily_papers.json')

        try:
            df_school = pd.read_csv('高校.csv', encoding='utf-8-sig')
            df_comp = pd.read_csv('公司.csv', encoding='utf-8-sig')
        except Exception as e:
            print(f"Error loading CSV: {e}")
            return

        # --- 处理高校 ---
        for _, row in df_school.iterrows():
            try:
                lat, lon = float(row['纬度']), float(row['经度'])
                lat += random.uniform(-0.0035, 0.0035)
                lon += random.uniform(-0.0035, 0.0035)

                school_name = str(row['学校']).strip()
                lab_name = str(row['实验室名']).strip()
                region = str(row.get('Region', 'China')).strip()

                # [新增] 建立映射关系: TEA Lab -> Tsinghua University
                self.lab_to_parent[lab_name] = school_name

                if school_name not in self.uni_groups:
                    self.uni_groups[school_name] = {
                        "lat": lat, "lon": lon, "region": region, "labs": []
                    }

                real_papers = self.history_data.get(lab_name, [])[:15]

                lab_info = {
                    "name": lab_name,
                    "desc": smart_wrap(str(row.get('简要介绍', '暂无介绍')), 35),
                    "leader": str(row.get('团队重要人物', 'Unknown')),
                    "direction": str(row.get('实验室方向', 'Unknown')),
                    "url": str(row.get('实验室主页', '')),
                    "papers": real_papers
                }
                self.uni_groups[school_name]['labs'].append(lab_info)

                weight = 1.0 if real_papers else 0.4
                self.heat_data.append([lat, lon, weight])

            except ValueError:
                continue

        # --- 处理公司 ---
        for _, row in df_comp.iterrows():
            try:
                lat, lon = float(row['纬度']), float(row['经度'])
                comp_name = str(row['公司名']).strip()

                # [新增] 建立映射关系: Google DeepMind -> Google DeepMind
                self.lab_to_parent[comp_name] = comp_name

                real_papers = self.history_data.get(comp_name, [])[:15]

                comp_node = {
                    "name": comp_name,
                    "region": str(row.get('Region', 'China')).strip(),
                    "lat": lat, "lon": lon,
                    "cat": str(row.get('分类', '')),
                    "time": str(row.get('成立时间', '')),
                    "founder": str(row.get('创始人/团队', '')),
                    "product": str(row.get('代表产品', '')),
                    "desc": smart_wrap(str(row.get('公司简要介绍', '')), 35),
                    "url": str(row.get('公司主页', '#')),
                    "papers": real_papers
                }
                self.companies.append(comp_node)
                weight = 1.0 if real_papers else 0.5
                self.heat_data.append([lat, lon, weight])
            except ValueError:
                continue

        # --- 处理右下角榜单 ---
        self._process_daily_hot_papers()

    def _process_daily_hot_papers(self):
        """处理今日高分论文，用于侧边栏展示"""
        flat_list = []
        seen_urls = set()

        for source_lab, papers in self.daily_data.items():
            # [新增] 尝试将实验室名转换为学校名，如果找不到则用原名
            display_source = self.lab_to_parent.get(source_lab, source_lab)

            for p in papers:
                if p['url'] in seen_urls: continue
                seen_urls.add(p['url'])

                p_copy = p.copy()
                p_copy['source'] = display_source  # 使用学校名
                p_copy['score'] = p.get('score', 0)
                flat_list.append(p_copy)

        flat_list.sort(key=lambda x: x['score'], reverse=True)
        self.hot_papers = flat_list[:10]


# ================= 3. 地图生成器 (3-Tabs 结构) =================

# ================= 3. 地图生成器 (3-Tabs 结构) =================

class MapGenerator:
    def __init__(self, data):
        self.data = data

    def _generate_3tab_popup(self, title, items, is_school=True):
        """
        通用的3标签页弹窗生成器
        修复点：根据 is_school 动态选择颜色，避免 KeyError
        """
        unique_id = str(random.randint(10000, 99999))

        # 动态决定主色调
        main_color = THEME['accent_uni'] if is_school else THEME['accent_comp']

        # --- 1. 构建 Tab 1: 详细信息 ---
        html_info = ""
        for item in items:
            if is_school:
                sub_title = f"""
                <div style="font-size:14px; font-weight:bold; color:{main_color}; margin-bottom:4px;">
                    {item['name']}
                </div>
                <div style="font-size:12px; color:{THEME['text_primary']}; margin-bottom:2px;">
                    <span style="opacity:0.7">方向:</span> {item['direction']}
                </div>
                <div style="font-size:12px; color:{THEME['text_primary']}; margin-bottom:4px;">
                    <span style="opacity:0.7">人物:</span> {item['leader']}
                </div>
                """
            else:
                sub_title = f"""
                <div style="font-size:14px; font-weight:bold; color:{main_color}; margin-bottom:4px;">
                    {item['name']}
                </div>
                <div style="font-size:12px; color:{THEME['text_primary']}; margin-bottom:2px;">
                    <span style="opacity:0.7">分类:</span> {item['cat']} | <span style="opacity:0.7">成立:</span> {item['time']}
                </div>
                <div style="font-size:12px; color:{THEME['text_primary']}; margin-bottom:2px;">
                    <span style="opacity:0.7">团队:</span> {item['founder']}
                </div>
                <div style="font-size:12px; color:{THEME['accent_gold']}; margin-bottom:4px;">
                    <span style="opacity:0.7">产品:</span> {item['product']}
                </div>
                """

            html_info += f"""
            <div style="margin-bottom:15px; border-bottom:1px solid rgba(255,255,255,0.15); padding-bottom:10px;">
                {sub_title}
                <div style="font-size:12px; color:#cbd5e1; line-height:1.4; font-style:italic;">
                    {item['desc']}
                </div>
            </div>
            """

        # --- 2. 构建 Tab 2: 论文列表 ---
        html_papers = ""
        has_paper = False
        for item in items:
            if item['papers']:
                has_paper = True
                if len(items) > 1:
                    html_papers += f"<div style='font-size:12px; color:{main_color}; margin:10px 0 5px 0; font-weight:bold;'>[{item['name']}]</div>"

                for p in item['papers']:
                    html_papers += f"""
                    <div style="margin-bottom:8px; border-bottom:1px dashed rgba(255,255,255,0.1); padding-bottom:4px;">
                        <a href="{p['url']}" target="_blank" style="color:{THEME['text_primary']}; text-decoration:none; font-size:12px; display:block;">
                            📄 {p['title']}
                        </a>
                    </div>
                    """

        if not has_paper:
            html_papers = f"<div style='padding:20px; text-align:center; color:#64748b; font-size:13px;'>暂无公开论文记录</div>"

        # --- 3. 构建 Tab 3: 链接 ---
        html_links = ""
        for item in items:
            has_link = item['url'] and len(str(item['url'])) > 5
            # 按钮颜色跟随主色调
            if is_school:
                btn_bg = "linear-gradient(135deg, #0891b2, #06b6d4)" if has_link else "#374151"  # Cyan gradient
            else:
                btn_bg = "linear-gradient(135deg, #ea580c, #f97316)" if has_link else "#374151"  # Orange gradient

            cursor = "pointer" if has_link else "not-allowed"
            href = f"href='{item['url']}' target='_blank'" if has_link else ""

            html_links += f"""
            <a {href} style="
                display:block; 
                background:{btn_bg}; 
                color:white; 
                padding:12px; 
                margin-bottom:10px; 
                border-radius:6px; 
                text-decoration:none; 
                text-align:center; 
                font-size:14px; 
                font-weight:bold;
                cursor:{cursor};
                box-shadow: 0 4px 6px rgba(0,0,0,0.2);
                transition: transform 0.1s;">
                🔗 访问 {item['name']} 官网
            </a>
            """

        # --- 4. 组装纯 CSS Tabs 结构 ---
        return f"""
        <div style="width:340px; font-family:'Microsoft YaHei', 'Segoe UI', sans-serif;">
            <h3 style="margin:0 0 15px 0; color:#fff; font-size:18px; text-align:center; letter-spacing:1px; border-bottom: 2px solid {main_color}; padding-bottom: 8px;">
                {title}
            </h3>

            <div class="css-tabs-{unique_id}">
                <input type="radio" name="grp-{unique_id}" id="t1-{unique_id}" checked style="display:none">
                <input type="radio" name="grp-{unique_id}" id="t2-{unique_id}" style="display:none">
                <input type="radio" name="grp-{unique_id}" id="t3-{unique_id}" style="display:none">

                <div style="display:flex; background:rgba(0,0,0,0.3); border-radius:6px; padding:3px; margin-bottom:15px;">
                    <label for="t1-{unique_id}" class="tab-btn">详细信息</label>
                    <label for="t2-{unique_id}" class="tab-btn">相关论文</label>
                    <label for="t3-{unique_id}" class="tab-btn">主页链接</label>
                </div>

                <div class="tab-pane pane-1" style="max-height:300px; overflow-y:auto; padding-right:5px;">{html_info}</div>
                <div class="tab-pane pane-2" style="max-height:300px; overflow-y:auto; padding-right:5px;">{html_papers}</div>
                <div class="tab-pane pane-3">{html_links}</div>
            </div>

            <style>
                .tab-pane::-webkit-scrollbar {{ width: 6px; }}
                .tab-pane::-webkit-scrollbar-track {{ background: rgba(0,0,0,0.1); }}
                .tab-pane::-webkit-scrollbar-thumb {{ background: #4b5563; border-radius: 3px; }}

                .css-tabs-{unique_id} .tab-btn {{
                    flex:1; text-align:center; padding:8px 0; cursor:pointer; 
                    font-size:12px; color:#9ca3af; border-radius:4px; transition:0.2s;
                }}
                /* 动态高亮颜色 */
                #t1-{unique_id}:checked ~ div label[for="t1-{unique_id}"],
                #t2-{unique_id}:checked ~ div label[for="t2-{unique_id}"],
                #t3-{unique_id}:checked ~ div label[for="t3-{unique_id}"] {{
                    background: {main_color}; color: #fff; font-weight:bold; text-shadow: 0 1px 2px rgba(0,0,0,0.3);
                }}

                .css-tabs-{unique_id} .tab-pane {{ display: none; color: #fff; }}
                #t1-{unique_id}:checked ~ .pane-1 {{ display: block; }}
                #t2-{unique_id}:checked ~ .pane-2 {{ display: block; }}
                #t3-{unique_id}:checked ~ .pane-3 {{ display: block; }}
            </style>
        </div>
        """

    def generate_map(self, region, config):
        m = folium.Map(
            location=config["loc"],
            zoom_start=config["zoom"],
            # 1. 使用色彩最丰富的 Voyager 底图
            tiles="CartoDB Voyager",
            attr='&copy; CARTO',
            zoom_control=False
        )

        # 2. CSS 注入：
        #    a. .leaflet-tile-pane: 只对底图应用滤镜 -> 变成深色霓虹风格，且色彩丰富 (非单调灰色)
        #    b. .leaflet-marker-icon: 强制保护图标颜色不被滤镜影响 (虽然pane不同通常不影响，但以防万一)
        #    c. 弹窗样式保持高级感
        css_inject = f"""
        <style>
            /* --- 地图底图滤镜：赛博霓虹风格 --- */
            /* invert(1): 反转颜色 (白变黑) */
            /* hue-rotate(180deg): 旋转色相 (让反转后的颜色回归冷暖逻辑) */
            /* saturate(300%): 极大增加饱和度 (拒绝灰色，要五彩斑斓) */
            /* brightness(0.8): 压暗背景，突出光路 */
            .leaflet-tile-pane {{
                filter: invert(100%) hue-rotate(180deg) saturate(300%) brightness(85%) contrast(110%) !important;
            }}

            /* --- 弹窗样式 --- */
            .leaflet-popup-content-wrapper, .leaflet-popup-tip {{
                background: {THEME['bg_panel']} !important;
                backdrop-filter: blur(12px);
                color: {THEME['text_primary']} !important;
                border: 1px solid {THEME['accent_uni']}; 
                box-shadow: 0 0 25px rgba(0, 229, 255, 0.3);
                border-radius: 8px;
            }}
            .leaflet-popup-close-button {{
                color: #fff !important; 
                font-size: 20px !important;
            }}
        </style>
        """
        m.get_root().header.add_child(folium.Element(css_inject))

        # 热力图 (使用极光色系)
        HeatMap(self.data.heat_data, radius=25, blur=18, min_opacity=0.3,
                gradient={0.3: '#0000ff', 0.6: '#00ffff', 1: '#ffffff'}).add_to(m)

        # 3. 高校聚合 (图标使用 blue，对应 accent_uni 的青色系)
        uni_cluster = MarkerCluster(
            name="Universities",
            disableClusteringAtZoom=6,
            maxClusterRadius=15
        ).add_to(m)

        for name, info in self.data.uni_groups.items():
            if not self._filter_region(region, info['region']): continue

            popup_html = self._generate_3tab_popup(name, info['labs'], is_school=True)

            folium.Marker(
                location=[info['lat'], info['lon']],
                popup=folium.Popup(popup_html, max_width=400),
                # 使用 blue，在深色霓虹地图上非常清晰
                icon=folium.Icon(color='blue', icon='university', prefix='fa'),
                tooltip=f"🎓 {name}"
            ).add_to(uni_cluster)

        # 4. 公司聚合 (图标使用 orange，对应 accent_comp 的橙色系)
        comp_cluster = MarkerCluster(name="Companies", disableClusteringAtZoom=10).add_to(m)

        for c in self.data.companies:
            if not self._filter_region(region, c['region']): continue

            popup_html = self._generate_3tab_popup(c['name'], [c], is_school=False)

            folium.Marker(
                location=[c['lat'], c['lon']],
                popup=folium.Popup(popup_html, max_width=400),
                # 使用 orange，形成红蓝对抗色
                icon=folium.Icon(color='orange', icon='robot', prefix='fa')
            ).add_to(comp_cluster)

        return m

    def _filter_region(self, target_region, item_region):
        if target_region == "China":
            return 'China' in item_region or 'Singapore' in item_region
        elif target_region == "Europe":
            return item_region in ['Europe', 'UK', 'Germany', 'Switzerland', 'Norway']
        else:
            return target_region.lower() in item_region.lower()


# ================= 4. 仪表盘生成 (视觉升级: 巨型卡片 + 丰富信息) =================

beijing_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def create_dashboard(map_files, hot_papers):
    recs_html = ""

    if not hot_papers:
        recs_html = "<div style='padding:20px; color:#64748b; text-align:center;'>Waiting for daily updates...</div>"

    for i, p in enumerate(hot_papers):
        score_val = p.get('score', 0)

        if score_val >= 90:
            bar_color = THEME['success']
        elif score_val >= 80:
            bar_color = THEME['accent_uni']
        else:
            bar_color = THEME['accent_comp']

        summary_text = p.get('summary', "")
        if not summary_text or summary_text == "No summary available.":
            summary_text = p.get('abstract', 'No details provided.')[:120] + "..."

        # 主图 HTML 处理逻辑
        teaser_url = p.get('teaser_image')
        teaser_html = ""
        if teaser_url:
            teaser_html = f"""
            <div class="p-image-container">
                <img src="{teaser_url}" class="p-teaser" alt="Teaser Image" loading="lazy" 
                     onerror="this.parentElement.style.display='none';">
            </div>
            """

        recs_html += f"""
        <div class="paper-card">
            <div class="card-left">
                <div class="score-circle" style="border-color:{bar_color}; color:{bar_color}">{score_val:.1f}</div>
            </div>
            <div class="card-content">
                <div class="card-header">
                    <span class="source-badge">🏛 {p['source']}</span>
                    <span class="date-badge">{p.get('date', 'Today')}</span>
                </div>
                <a href="{p['url']}" target="_blank" class="p-title">{p['title']}</a>

                {teaser_html}

                <div class="p-abstract">
                    {summary_text}
                </div>
            </div>
            <div class="glow-bar" style="background:{bar_color};"></div>
        </div>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Embodied AI Global Monitor</title>
        <style>
            :root {{
                --bg-root: {THEME['bg_root']};
                --text-main: {THEME['text_primary']};
                --text-dim: {THEME['text_secondary']};
                --accent-uni: {THEME['accent_uni']};
                --accent-comp: {THEME['accent_comp']};
                --panel-bg: {THEME['bg_panel']};
            }}

            body {{
                margin: 0; padding: 0; background-color: var(--bg-root);
                font-family: system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                height: 100vh; overflow: hidden;
                display: grid;
                /* 修改布局：左侧三个地图等高，右侧列表铺满全高 */
                grid-template-rows: 60px 1fr 1fr 1fr; 
                grid-template-columns: 1fr 1fr;
                grid-template-areas: 
                    "header header"
                    "map1 feed"
                    "map2 feed"
                    "map3 feed";
                gap: 4px;
                background: #000;
            }}

            .dashboard-header {{
                grid-area: header;
                background: #020617;
                border-bottom: 2px solid var(--accent-uni);
                display: flex;
                align-items: center;
                justify-content: center; 
                position: relative;
                padding: 0 30px;
                box-shadow: 0 0 20px rgba(0, 229, 255, 0.15);
                z-index: 10;
            }}

            .header-title {{
                font-size: 24px;
                font-weight: 800;
                letter-spacing: 4px;
                background: linear-gradient(90deg, var(--accent-uni), #a78bfa);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                text-transform: uppercase;
                text-shadow: 0 0 30px rgba(0, 229, 255, 0.2);
            }}

            .header-meta {{
                color: var(--text-dim);
                font-size: 12px;
                font-family: 'Courier New', monospace;
                display: flex;
                gap: 20px;
                position: absolute;
                right: 30px;
            }}

            .area-map1 {{ grid-area: map1; }}
            .area-map2 {{ grid-area: map2; }}
            .area-map3 {{ grid-area: map3; }}
            .area-feed {{ grid-area: feed; }}

            .grid-item {{
                position: relative;
                background: #0f172a;
                overflow: hidden;
                border: 1px solid #1e293b;
            }}

            iframe {{ width: 100%; height: 100%; border: none; }}

            .map-label {{
                position: absolute; top: 10px; left: 10px; z-index: 999;
                background: rgba(15, 23, 42, 0.9); backdrop-filter: blur(10px);
                padding: 4px 10px; border-radius: 4px;
                border-left-width: 4px; border-left-style: solid;
                box-shadow: 0 4px 15px rgba(0,0,0,0.4);
            }}
            .map-title {{ color: #fff; font-size: 12px; font-weight: 700; letter-spacing: 1px; margin: 0; }}

            .rec-container {{
                background: #0b1121;
                height: 100%; display: flex; flex-direction: column;
            }}
            .rec-header {{
                padding: 15px 20px; background: #111827;
                border-bottom: 1px solid #1e293b;
                display: flex; justify-content: space-between; align-items: center;
            }}
            .rec-title {{ font-size: 16px; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 10px; }}
            .live-indicator {{
                display: flex; align-items: center; gap: 6px;
                font-size: 10px; color: var(--accent-uni); font-weight: bold;
                background: rgba(0, 229, 255, 0.1); padding: 3px 8px; border-radius: 12px; border: 1px solid rgba(0, 229, 255, 0.2);
            }}

            .rec-list {{ 
                flex: 1; overflow-y: auto; padding: 15px; 
                background-image: radial-gradient(circle at top right, #172033 0%, #0b1121 40%);
            }}
            .rec-list::-webkit-scrollbar {{ width: 6px; }}
            .rec-list::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 3px; }}

            .paper-card {{
                display: flex; 
                background: rgba(30, 41, 59, 0.4);
                border: 1px solid rgba(255,255,255,0.05); 
                margin-bottom: 12px;
                border-radius: 8px; overflow: hidden; position: relative;
                transition: all 0.3s ease;
            }}
            .paper-card:hover {{ 
                background: rgba(30, 41, 59, 0.9); 
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(0,0,0,0.3);
                border-color: rgba(255,255,255,0.15);
            }}
            .card-left {{
                width: 55px; display: flex; justify-content: center; align-items: center;
                background: rgba(0,0,0,0.2); border-right: 1px solid rgba(255,255,255,0.03);
            }}
            .score-circle {{
                width: 34px; height: 34px; border-radius: 50%;
                border: 2.5px solid; display: flex; justify-content: center; align-items: center;
                font-size: 13px; font-weight: 800;
            }}
            .card-content {{ padding: 12px; flex: 1; display: flex; flex-direction: column; justify-content: center; }}
            .card-header {{ display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 10px; }}
            .source-badge {{ color: var(--accent-uni); font-weight: bold; }}
            .date-badge {{ color: #64748b; font-family: monospace; }}
            .p-title {{
                color: #f1f5f9; font-size: 14px; font-weight: 600; line-height: 1.3;
                text-decoration: none; margin-bottom: 8px; display: block;
            }}
            .p-title:hover {{ color: var(--accent-uni); text-shadow: 0 0 10px rgba(0, 229, 255, 0.3); }}

            .p-image-container {{
                width: 100%;
                margin-bottom: 10px;
                background: rgba(0, 0, 0, 0.3);
                border-radius: 4px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                overflow: hidden;
                display: flex;
                justify-content: center;
            }}
            .p-teaser {{
                max-width: 100%;
                max-height: 220px;
                object-fit: contain;
                display: block;
            }}

            .p-abstract {{ 
                font-size: 12px; color: #94a3b8; margin-bottom: 4px; line-height: 1.5; 
                font-style: normal;
                display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; 
            }}

            .glow-bar {{ position: absolute; left: 0; top: 0; bottom: 0; width: 3px; box-shadow: 0 0 8px currentColor; }}
        </style>
    </head>
    <body>
        <div class="dashboard-header">
            <div class="header-title">EMBODIED AI GLOBAL MONITOR</div>
            <div class="header-meta">
                <span>SYSTEM: ONLINE</span>
                <span>DATA: {datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')} (CST)</span>
            </div>
        </div>

        <div class="grid-item area-map1">
            <div class="map-label" style="border-left-color: var(--accent-comp);">
                <div class="map-title">CHINA / APAC</div>
            </div>
            <iframe src="{map_files['China']}"></iframe>
        </div>

        <div class="grid-item area-map2">
            <div class="map-label" style="border-left-color: #3b82f6;">
                <div class="map-title">NORTH AMERICA</div>
            </div>
            <iframe src="{map_files['USA']}"></iframe>
        </div>

        <div class="grid-item area-map3">
            <div class="map-label" style="border-left-color: var(--accent-uni);">
                <div class="map-title">EUROPE</div>
            </div>
            <iframe src="{map_files['Europe']}"></iframe>
        </div>

        <div class="grid-item area-feed">
            <div class="rec-container">
                <div class="rec-header">
                    <div class="rec-title"> DAILY TOP PAPERS</div>
                    <div class="live-indicator">● AI RANKING</div>
                </div>
                <div class="rec-list" id="auto-scroller">{recs_html}</div>
            </div>
        </div>

        <script>
            // 自动滚动逻辑
            const scrollContainer = document.getElementById('auto-scroller');
            let scrollSpeed = 1.5; // 速度调快：从 1 增加到 1.5
            let isAutoScrolling = true;

            function autoScroll() {{
                if (isAutoScrolling) {{
                    // 增加滚动位置
                    scrollContainer.scrollTop += scrollSpeed;
                    
                    // 边界检测：使用 Math.ceil 并预留 2px 误差范围确保触发回弹
                    if (Math.ceil(scrollContainer.scrollTop + scrollContainer.clientHeight) >= scrollContainer.scrollHeight - 2) {{
                        scrollContainer.scrollTop = 0; 
                    }}
                }}
            }}

            // 保持每 50 毫秒执行一次，通过修改步长来控制视觉速度
            let scrollInterval = setInterval(autoScroll, 50);

            // 鼠标交互逻辑保持不变...
            scrollContainer.addEventListener('mouseenter', () => {{ isAutoScrolling = false; }});
            scrollContainer.addEventListener('mouseleave', () => {{ isAutoScrolling = true; }});
        </script>
    </body>
    </html>
    """

    with open("dashboard_index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print(">>> Dashboard index generated with Auto-Scroll!")

# ================= 5. 主程序 =================

def main():
    data = DataEngine()
    data.load_data()

    mapper = MapGenerator(data)
    map_files = {}

    for name, conf in REGION_CONFIG.items():
        m = mapper.generate_map(name, conf)
        filename = f"map_{name}.html"
        m.save(filename)
        map_files[name] = filename
        print(f"  > Map generated: {filename}")

    create_dashboard(map_files, data.hot_papers)
    print("\n✅ V4 完成！地图色调已调亮为深蓝，Icon弹窗逻辑已重构。")


if __name__ == "__main__":
    main()
