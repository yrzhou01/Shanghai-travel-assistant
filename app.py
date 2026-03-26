import streamlit as st
import requests
import json
import re
import os
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()  # 加载 .env 文件中的变量到环境变量

# ---------- 配置 ----------
# 从环境变量读取密钥（部署时设置）
AMAP_KEY = os.getenv("AMAP_KEY")
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")

ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 加载景点数据
with open("attractions.json", "r", encoding="utf-8") as f:
    attractions = json.load(f)

all_tags = sorted(set(tag for spot in attractions if "tags" in spot for tag in spot["tags"]))

# ---------- 辅助函数 ----------
def call_zhipu(prompt):
    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "glm-4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    try:
        resp = requests.post(ZHIPU_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"调用AI失败：{e}"

def geocode(address):
    if "上海" not in address and "沪" not in address:
        address = "上海市" + address
    url = f"https://restapi.amap.com/v3/geocode/geo?address={address}&key={AMAP_KEY}"
    try:
        resp = requests.get(url)
        data = resp.json()
        if data.get("status") == "1" and data.get("geocodes"):
            loc = data["geocodes"][0]["location"]
            lng, lat = loc.split(",")
            return float(lng), float(lat)
        else:
            return None, None
    except Exception as e:
        print(f"地理编码错误：{e}")
        return None, None

def get_transit_time(origin_lng, origin_lat, dest_lng, dest_lat):
    """获取两个地点之间的公交/地铁时间（秒），失败返回None"""
    url = f"https://restapi.amap.com/v3/direction/transit/integrated?origin={origin_lng},{origin_lat}&destination={dest_lng},{dest_lat}&city=上海&key={AMAP_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        # 快速调试输出（可选，如果出现错误可以打开打印）
        # print("时间API返回：", json.dumps(data, ensure_ascii=False)[:200])
        if data.get("status") != "1":
            return None
        route = data.get("route")
        if not route or not isinstance(route, dict):
            return None
        transits = route.get("transits")
        if not transits or not isinstance(transits, list) or len(transits) == 0:
            return None
        transit = transits[0]
        if not isinstance(transit, dict):
            return None
        # 尝试多种方式获取duration
        cost = transit.get("cost")
        if isinstance(cost, dict):
            dur = cost.get("duration")
            if dur:
                return int(dur)
        # 如果cost里没有，直接取transit的duration字段
        dur = transit.get("duration")
        if isinstance(dur, (int, float, str)):
            try:
                return int(dur)
            except:
                pass
        return None
    except Exception as e:
        print(f"获取时间失败：{e}")
        return None

def plan_route(origin_lng, origin_lat, dest_lng, dest_lat):
    url = f"https://restapi.amap.com/v3/direction/transit/integrated?origin={origin_lng},{origin_lat}&destination={dest_lng},{dest_lat}&city=上海&key={AMAP_KEY}"
    try:
        resp = requests.get(url)
        data = resp.json()
        if data.get("status") != "1":
            return f"高德API错误：{data.get('info', '未知错误')}"
        route = data.get("route")
        if not route or not isinstance(route, dict):
            return "未找到路线信息"
        transits = route.get("transits")
        if not transits or not isinstance(transits, list) or len(transits) == 0:
            return "未找到公交路线，可尝试打车或步行。"
        transit = transits[0]
        if not isinstance(transit, dict):
            return "路线数据格式错误"
        cost = transit.get("cost")
        duration = 0
        walking_distance = 0
        if isinstance(cost, dict):
            duration = int(cost.get("duration", 0)) // 60 if cost.get("duration") else 0
            walking_distance = cost.get("walking_distance", 0)
        if duration == 0:
            dur = transit.get("duration")
            if isinstance(dur, (int, float, str)):
                try:
                    duration = int(dur) // 60
                except:
                    duration = 0
        segments = transit.get("segments", [])
        if not isinstance(segments, list):
            segments = []
        instructions = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            walking = seg.get("walking")
            if isinstance(walking, dict):
                walk_dist = walking.get("distance")
                if walk_dist and isinstance(walk_dist, (int, float)) and walk_dist > 0:
                    instructions.append(f"步行{int(walk_dist)}米")
            bus = seg.get("bus")
            if isinstance(bus, dict):
                buslines = bus.get("buslines")
                if isinstance(buslines, list) and len(buslines) > 0:
                    bl = buslines[0]
                    if isinstance(bl, dict):
                        name = bl.get("name", "未知公交")
                        departure = bl.get("departure_stop")
                        arrival = bl.get("arrival_stop")
                        start_stop = departure.get("name") if isinstance(departure, dict) else "未知起点"
                        end_stop = arrival.get("name") if isinstance(arrival, dict) else "未知终点"
                        instructions.append(f"乘{name}，{start_stop}→{end_stop}")
        route_desc = f"总时间约{duration}分钟"
        if walking_distance:
            route_desc += f"，步行{walking_distance}米"
        route_desc += "\n"
        if instructions:
            route_desc += " → ".join(instructions)
        else:
            route_desc += "具体步骤详情请参考高德地图。"
        return route_desc
    except Exception as e:
        print(f"路线规划异常：{e}")
        return f"路线规划失败：{e}"

def find_best_order(start_point, spots):
    """
    使用贪心最近邻算法找出从起点出发，经过所有景点的近似最优顺序
    start_point: (lng, lat) 元组
    spots: list of dict, each with {'name': 'xx', 'lng': float, 'lat': float}
    返回：(ordered_names, ordered_spots)
    """
    n = len(spots)
    if n == 0:
        return [], []
    if n == 1:
        return [spots[0]["name"]], spots

    # 初始化距离矩阵，大小为 n+1（索引 0..n-1 为景点，n 为起点）
    dist = [[float('inf')] * (n+1) for _ in range(n+1)]
    start_lng, start_lat = start_point

    # 起点到各景点
    for i, spot in enumerate(spots):
        t = get_transit_time(start_lng, start_lat, spot["lng"], spot["lat"])
        if t is not None:
            dist[n][i] = t

    # 景点之间
    for i in range(n):
        for j in range(n):
            if i == j:
                dist[i][j] = 0
            else:
                t = get_transit_time(spots[i]["lng"], spots[i]["lat"], spots[j]["lng"], spots[j]["lat"])
                if t is not None:
                    dist[i][j] = t

    # 贪心最近邻
    unvisited = set(range(n))
    current = None
    total_time = 0
    order_indices = []

    # 第一步：从起点找最近景点
    min_time = float('inf')
    for i in unvisited:
        if dist[n][i] < min_time:
            min_time = dist[n][i]
            current = i
    if current is not None and min_time < float('inf'):
        total_time += min_time
        order_indices.append(current)
        unvisited.remove(current)
    else:
        # 起点无法到达任何景点
        return [], []

    # 循环找下一个最近景点
    while unvisited:
        min_time = float('inf')
        next_spot = None
        for i in unvisited:
            if dist[current][i] < min_time:
                min_time = dist[current][i]
                next_spot = i
        if next_spot is None or min_time == float('inf'):
            break
        total_time += min_time
        order_indices.append(next_spot)
        unvisited.remove(next_spot)
        current = next_spot

    ordered_spots = [spots[i] for i in order_indices]
    ordered_names = [s["name"] for s in ordered_spots]
    return ordered_names, ordered_spots

# ---------- Streamlit 界面 ----------
st.set_page_config(page_title="上海公益AI旅游助手", layout="wide")
st.title("🌟 上海公益AI旅游助手")
st.markdown("免费 · 无广告 · 为所有来沪游客提供智能出行帮助")

with st.sidebar:
    st.header("🔧 服务说明")
    st.markdown("本工具已集成智谱AI与高德地图服务，完全免费。")
    st.markdown("---")
    st.markdown("**公益理念**：让科技传递城市温度，帮助每一位游客轻松游上海。")

# ---------- 主界面 ----------
st.header("🏷️ 1. 按标签筛选景点")
selected_tags = st.multiselect("选择一个或多个标签", all_tags)

filtered_spots = []
if selected_tags:
    for spot in attractions:
        spot_tags = spot.get("tags", [])
        if any(tag in spot_tags for tag in selected_tags):
            filtered_spots.append(spot)
else:
    filtered_spots = attractions

st.subheader(f"📌 共 {len(filtered_spots)} 个景点符合筛选条件")
selected_names = []
cols = st.columns(4)
for i, spot in enumerate(filtered_spots):
    with cols[i % 4]:
        if st.checkbox(f"{spot['name']}", key=spot["name"]):
            selected_names.append(spot["name"])

st.subheader(f"✅ 已选中 {len(selected_names)} 个景点")
st.write(", ".join(selected_names) if selected_names else "暂无")

# 获取当前选中的景点详情
selected_spots = [s for s in attractions if s["name"] in selected_names]

st.header("🗺️ 2. 输入当前位置并获取最优路线")
start_place = st.text_input("你现在的位置（例如：人民广场）", value="人民广场")

col1, col2 = st.columns(2)
with col1:
    route_btn = st.button("🚀 分别规划路线", type="primary")
with col2:
    smart_btn = st.button("✨ 智能规划最优顺序", type="primary")

if route_btn:
    if not selected_names:
        st.warning("请先至少选择一个景点")
    else:
        with st.spinner("正在获取你的位置坐标..."):
            slng, slat = geocode(start_place)
        if slng is None:
            st.error("无法识别起点地址，请尝试更具体的地点，如‘上海人民广场’或‘外滩’")
        else:
            st.success(f"你的位置：{start_place} (经度 {slng:.4f}, 纬度 {slat:.4f})")
            st.markdown("---")
            for spot_name in selected_names:
                spot = next(s for s in attractions if s["name"] == spot_name)
                with st.spinner(f"规划去往 {spot_name} 的路线..."):
                    route_info = plan_route(slng, slat, spot["lng"], spot["lat"])
                st.subheader(f"📍 前往 {spot_name}")
                st.text(route_info)
                st.markdown("---")

if smart_btn:
    if not selected_names:
        st.warning("请先至少选择一个景点")
    else:
        with st.spinner("正在获取你的位置坐标..."):
            slng, slat = geocode(start_place)
        if slng is None:
            st.error("无法识别起点地址，请尝试更具体的地点，如‘上海人民广场’或‘外滩’")
        else:
            st.success(f"你的位置：{start_place} (经度 {slng:.4f}, 纬度 {slat:.4f})")
            st.markdown("---")
            st.info("🤖 正在计算最优游览顺序（需要一些时间，请稍候...）")
            ordered_names, ordered_spots = find_best_order((slng, slat), selected_spots)
            if not ordered_names:
                st.error("无法规划顺序，可能是部分景点无法获取路线时间，请减少景点数量或稍后重试。")
            else:
                st.subheader("📋 最优游览顺序")
                order_text = f"起点: {start_place} → "
                order_text += " → ".join(ordered_names)
                st.markdown(f"**{order_text}**")
                st.markdown("---")
                
                current_lng, current_lat = slng, slat
                total_minutes = 0
                for idx, spot_name in enumerate(ordered_names):
                    spot = next(s for s in attractions if s["name"] == spot_name)
                    with st.spinner(f"规划第{idx+1}段：{start_place if idx==0 else ordered_names[idx-1]} → {spot_name}"):
                        route_info = plan_route(current_lng, current_lat, spot["lng"], spot["lat"])
                    st.subheader(f"🚩 第{idx+1}站：前往 {spot_name}")
                    st.text(route_info)
                    match = re.search(r"总时间约(\d+)分钟", route_info)
                    if match:
                        total_minutes += int(match.group(1))
                    st.markdown("---")
                    current_lng, current_lat = spot["lng"], spot["lat"]
                
                # 可选返回起点
                with st.expander("是否返回起点？"):
                    if st.button("规划返回起点路线"):
                        with st.spinner("规划返回起点..."):
                            route_back = plan_route(current_lng, current_lat, slng, slat)
                        st.subheader("🏁 返回起点")
                        st.text(route_back)
                        match = re.search(r"总时间约(\d+)分钟", route_back)
                        if match:
                            total_minutes += int(match.group(1))
                
                st.info(f"⏱️ 预计总游览时间（含交通）约 {total_minutes} 分钟")

st.header("📖 3. 文化讲解（可选）")
culture_spot = st.selectbox("选择一个景点，听听它的故事", [s["name"] for s in attractions])
if st.button("讲述文化故事"):
    with st.spinner("AI正在生成讲解..."):
        prompt = f"请为上海景点“{culture_spot}”写一段200字左右的文化讲解，语言亲切，适合老年人听。"
        story = call_zhipu(prompt)
    st.success(f"《{culture_spot}》的文化解读：")
    st.markdown(story)

st.markdown("---")
st.markdown("📢 本项目为华东师范大学紫江公益训练营创新实践项目，公益免费，旨在用AI帮助每一位游客轻松游上海。")