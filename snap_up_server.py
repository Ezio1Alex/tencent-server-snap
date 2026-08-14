# -*- coding: utf-8 -*-
"""
腾讯云秒杀抢购脚本

流程：
  1. 读取 cookies.json 与 csrf_token.txt（由 get_cookies.py 扫码登录生成）
  2. 同步腾讯云服务器时间，等待下一场秒杀（每天固定时刻）
  3. 秒杀前做毫秒级时间校准，提前开火，高频并发下单
  4. 抢到（code=0）自动退出；未抢到自动等下一场

活动参数与抢购参数集中在下方配置区，按 README 说明修改即可。
"""

import requests
import json
import sys
import time
from datetime import datetime, timedelta, timezone
import threading

# ======================= 活动配置（按自己活动页抓包填写） =======================
ACTIVITY_ID = 162634773874417          # 活动ID
CHECK_ACT_ID = 1784747698901873        # 库存检查接口里的商品活动ID
DO_ACT_ID = 1897632168296710           # 下单接口里的商品活动ID
BUSINESS_ID = 22755                    # 业务ID（下单请求 business.id）
GOODS_TYPE = "bundle_budget_mc_lg4_01" # 套餐类型
IMAGE_ID = "lhbp-eqora508"             # 镜像ID
BLUEPRINT_ID = "LINUX_UNIX"            # 操作系统镜像
TIME_SPAN_UNIT = "12m"                 # 购买时长
REGION_IDS = [1, 4]                    # 目标地域：1=广州，4=上海，8=北京
ACTIVITY_URL = "https://cloud.tencent.com/act/pro/featured-202607"  # 活动页地址

# ======================= 抢购配置 =======================
SECKILL_HOURS = [10, 15]   # 每天抢购时刻（北京时间）
RUSH_DURATION = 3          # 抢购爆发窗口（秒），窗口内持续下单
RUSH_CONCURRENCY = 14      # 独立请求通道数（每路一个线程），14路折中火力与限流
RUSH_LEAD = 0.01           # 提前开火（秒）：贴着放货瞬间发
REQUEST_TIMEOUT = 30       # 单个请求等待响应上限（秒）：踩中时刻的请求可能被排队很久才回话，给足30秒等到结果

# ======================= 地域对照（库存检查打印用） =======================
REGION_MAP = {1: "广州", 4: "上海", 8: "北京"}

# 北京时间时区。所有时间基准均用服务器时间换算，不依赖电脑本地时钟
BJ_TZ = timezone(timedelta(hours=8))

# ======================= 会话与凭证 =======================
session = requests.Session()
# 多路并发共享session，把连接池撑到20避免连接反复重建
from requests.adapters import HTTPAdapter
session.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=20))


def load_cookies():
    """加载登录凭证 cookies.json 到会话"""
    try:
        with open("cookies.json", "r", encoding="utf-8") as f:
            cookies = json.load(f)
        for cookie in cookies:
            session.cookies.set(
                cookie.get('name', ''),
                cookie.get('value', ''),
                domain=cookie.get('domain', ''),
                path=cookie.get('path', '/')
            )
    except FileNotFoundError:
        print("❌ 未找到 cookies.json。请先运行 get_cookies.py 扫码登录。")
        sys.exit(1)


def get_csrf_token():
    """读取 get_cookies.py 登录时自动捕获的 CSRF Token（与 cookies.json 同一会话）"""
    try:
        with open("csrf_token.txt", "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            print("⚠️ csrf_token.txt 为空，登录会话可能已过期，请重新运行 get_cookies.py")
        return token
    except FileNotFoundError:
        print("❌ 未找到 csrf_token.txt。请先运行 get_cookies.py 扫码登录。")
        sys.exit(1)


load_cookies()

headers = {
    "x-csrf-token": get_csrf_token(),
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "referer": ACTIVITY_URL,
    "x-requested-with": "XMLHttpRequest",
}

# ======================= 库存检查（可选：手动查看有没有货） =======================


def check_available():
    """
    检查库存，返回有货的地域ID（无货返回 None）。
    主程序不依赖它，可单独调用手动诊断：python -c "import snap_up_server as s; s.check_available()"
    """
    check_data = {
        "activity_id": ACTIVITY_ID,
        "goods": [{"act_id": CHECK_ACT_ID, "region_id": REGION_IDS}],
        "preview": 0,
    }
    try:
        resp = session.post(
            "https://act-api.cloud.tencent.com/dianshi/check-available",
            json=check_data,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
    except requests.exceptions.HTTPError as e:
        print(f"❌ 库存检查HTTP错误：{e}")
        print(f"   状态码: {resp.status_code}")
        print(f"   响应体: {resp.text[:1000]}")
        return None
    except Exception as e:
        print(f"❌ 库存检查接口调用失败：{e}")
        return None

    if result.get("code") != 0 or result.get("msg") != "ok":
        print(f"❌ 库存检查接口返回异常：{json.dumps(result, ensure_ascii=False)}")
        return None

    goods_data = result.get("data", [{}])[0]
    if goods_data.get("available") != 1 or goods_data.get("user_available") != 1:
        print("❌ 商品无购买权限/整体无货")
        return None

    quota = goods_data.get("quota", {})
    for region_id, region_name in REGION_MAP.items():
        available = quota.get(str(region_id), {}).get(GOODS_TYPE, {}).get("available", 0)
        if available > 0:
            print(f"✅ 检测到{region_name}（region_id={region_id}）有库存！")
            return region_id

    print("❌ 所有目标地域均无库存")
    return None

# ======================= 立即购买（核心下单） =======================


def buy_now(region_id):
    """
    调用 do-goods 接口下单
    :param region_id: 目标地域ID
    """
    do_data = {
        "activity_id": ACTIVITY_ID,
        "agent_channel": {
            "fromChannel": "",
            "fromSales": "",
            "isAgentClient": False,
            "fromUrl": ACTIVITY_URL,
        },
        "business": {
            "id": BUSINESS_ID,
            "from": "lightningDeals",
        },
        "goods": [
            {
                "act_id": DO_ACT_ID,
                "type": GOODS_TYPE,
                "goods_param": {
                    "BlueprintId": BLUEPRINT_ID,
                    "area": 1,
                    "ddocUnionConnect": 0,
                    "goodsNum": 1,
                    "imageId": IMAGE_ID,
                    "scenario": "0",
                    "timeSpanUnit": TIME_SPAN_UNIT,
                    "zone": "",
                    "regionId": region_id,
                    "type": GOODS_TYPE,
                },
            }
        ],
        "preview": 0,
    }
    send_time = datetime.now(tz=BJ_TZ).strftime("%H:%M:%S.%f")[:-3]  # 请求发送时刻（北京时间，毫秒）
    print(f"📤 [{send_time}] 发送下单请求 地域{region_id}")
    try:
        resp = session.post(
            "https://act-api.cloud.tencent.com/dianshi/do-goods",
            json=do_data,
            headers=headers,
            timeout=REQUEST_TIMEOUT  # 等不到响应就放弃换下一发，防止某路连接卡死整路熄火
        )
        recv_time = datetime.now(tz=BJ_TZ).strftime("%H:%M:%S.%f")[:-3]  # 响应返回时刻
        print(f"📥 [{send_time} → {recv_time}] 返回：{resp.text}")
        result = resp.json()
        if isinstance(result, dict):
            result["region_id"] = region_id  # 把地域ID附到结果上，方便定位哪个地域成功
        return result
    except Exception as e:
        recv_time = datetime.now(tz=BJ_TZ).strftime("%H:%M:%S.%f")[:-3]
        print(f"❌ [{send_time} → {recv_time}] 调用失败：{e}")
        return None

# ======================= 服务器时间同步 =======================


def get_server_time():
    """获取腾讯云服务器时间（响应头Date），返回北京时间毫秒时间戳；失败返回None"""
    try:
        response = requests.head(ACTIVITY_URL, timeout=10)
        server_time = response.headers.get("Date")
        if server_time:
            # 显式按UTC解析，再转北京时间，与电脑本地时区无关
            utc = datetime.strptime(server_time, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
            return int(utc.astimezone(BJ_TZ).timestamp() * 1000)
    except Exception as e:
        print(f"⚠️ 获取服务器时间失败：{e}")
    return None


def calibrate_offset(samples=8):
    """估算本地时钟与服务器时间的偏移（毫秒），用于秒杀时刻毫秒级卡点。

    发送时刻对齐的采样会被慢响应污染（响应越慢，样本虚高越多）。
    先丢弃最大的25%样本（大概率是慢响应污染的），再取剩余的最大值：
    既贴近真实偏移，又不会被单个抖动样本带偏开火点。
    """
    offsets = []
    print("⏳ 校准中：采样服务器时间偏移（8次）...")
    for i in range(samples):
        t0 = time.time() * 1000  # 本地发送时刻
        t = get_server_time()    # 服务器时间（Date头，秒级）
        if t is not None:
            offsets.append(t - t0)
            print(f"   样本{i+1}/{samples}：偏移 {t - t0:+.0f}ms")
        time.sleep(0.15)
    if not offsets:
        print("⚠️ 校准失败")
        return None
    offsets.sort()
    drop = max(1, len(offsets) // 4)  # 丢弃最大的25%（慢响应污染样本）
    best = offsets[-drop - 1] if len(offsets) > drop else offsets[0]
    print(f"✅ 校准完成：偏移 {best:+.0f}ms")
    return best

# ======================= 高频抢购 =======================


def prewarm_pool(routes):
    """开抢前预热连接池：向act-api顺序建好连接（keep-alive复用），
    开火瞬间各路线程直接复用现成连接，请求才能真正同时发出。
    """
    print(f"🔥 预热连接池（{routes}条连接）...")
    ok = 0
    for _ in range(routes):
        try:
            session.head(
                "https://act-api.cloud.tencent.com/dianshi/check-available",
                headers=headers,
                timeout=5,
            )
            ok += 1
        except Exception:
            pass
    print(f"✅ 连接池预热完成：{ok}/{routes} 条连接就绪")


def rush_buy(region_ids, duration, routes):
    """开多路独立请求通道：每路一个线程，各自持续发下单请求，任何一路成功即整体停止。

    各路完全独立——某路请求卡住/超时只影响自己，其他路照常打，互不拖累。
    """
    stop = threading.Event()
    holder = {'result': None}

    def worker(route_idx):
        rid = region_ids[route_idx % len(region_ids)]
        deadline = time.time() + duration
        while time.time() < deadline and not stop.is_set():
            result = buy_now(rid)
            if isinstance(result, dict) and result.get("code") == 0:
                holder['result'] = result
                stop.set()
                return

    print(f"🔥 开火！开启{routes}路独立请求通道，窗口{duration}秒，抢到即停")
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(routes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if holder['result']:
        r = holder['result']
        print(f"🎉 抢购成功！地域ID: {r.get('region_id', '未知')}")
        return r
    print(f"❌ 抢购窗口结束（{duration}秒），未抢到")
    return None

# ======================= 主程序 =======================


def get_next_seckill(current_beijing_ms):
    """计算距当前最近的一场秒杀（当天SECKILL_HOURS各时刻，均已过则算次日最早一场），返回毫秒时间戳"""
    now = datetime.fromtimestamp(current_beijing_ms / 1000, tz=timezone.utc).astimezone(BJ_TZ)
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in SECKILL_HOURS]
    futures = [t for t in today if t >= now]  # 含当前整点，避免轮询恰好踩中整点误判成下一场
    if not futures:
        tomorrow = now + timedelta(days=1)
        futures = [tomorrow.replace(hour=h, minute=0, second=0, microsecond=0) for h in SECKILL_HOURS]
    return int(min(futures).timestamp() * 1000)


if __name__ == "__main__":
    print("🚀 启动腾讯云抢购脚本...")
    while True:
        current_time = get_server_time()
        if current_time is None:
            print("⚠️ 服务器时间获取失败，3秒后重试")
            time.sleep(3)
            continue

        next_seckill = get_next_seckill(current_time)
        remain_s = (next_seckill - current_time) / 1000

        if remain_s <= 15:
            # 临门一脚：校准毫秒级服务器时间，提前RUSH_LEAD秒开火，卡在时刻点上
            print("🎯 进入最后15秒，校准毫秒级服务器时间...")
            offset = calibrate_offset()
            if offset is None:
                print("⚠️ 时间校准失败，改用秒级时间兜底")
                offset = 0
            prewarm_pool(RUSH_CONCURRENCY)  # 预热连接，开火瞬间请求真正同时发出
            lead_ms = int(RUSH_LEAD * 1000)
            while True:
                synced = time.time() * 1000 + offset  # 毫秒级服务器时间（本地时钟+校准偏移）
                if synced >= next_seckill - lead_ms:
                    nxt_dt = datetime.fromtimestamp(next_seckill / 1000, tz=BJ_TZ)
                    fire_dt = datetime.fromtimestamp(synced / 1000, tz=BJ_TZ)
                    diff_ms = synced - next_seckill
                    print(f"🎯 开火！秒杀时刻 {nxt_dt:%Y-%m-%d %H:%M:%S}，实际开火 {fire_dt:%H:%M:%S.%f}（{diff_ms:+.0f}ms）")
                    print("🔥 进入高频抢购窗口...")
                    result = rush_buy(REGION_IDS, RUSH_DURATION, RUSH_CONCURRENCY)
                    if result:
                        print("🎉 抢购成功，脚本退出")
                        sys.exit(0)
                    print("❌ 本场未抢到，等待下一场...")
                    break
                time.sleep(0.05)  # 50毫秒粒度卡点
            time.sleep(3)
            continue
        else:
            h, rem = divmod(int(remain_s), 3600)
            m, s = divmod(rem, 60)
            nxt_dt = datetime.fromtimestamp(next_seckill / 1000, tz=BJ_TZ)
            print(f"⏳ 距下一场秒杀（{nxt_dt:%Y-%m-%d %H:%M} 北京时间）还有 {h}小时{m}分{s}秒，当前服务器时间: {current_time}")
            time.sleep(3)  # 固定3秒轮询，直到进入15秒校准阶段
