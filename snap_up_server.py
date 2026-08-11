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
from concurrent.futures import ThreadPoolExecutor

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
RUSH_DURATION = 5          # 抢购爆发窗口（秒），窗口内高频下单
RUSH_INTERVAL = 0.3        # 每轮下单间隔（秒），调小更激进但易被限流
RUSH_LEAD = 0.8            # 提前开火（秒），补偿秒级时间戳截断与网络延迟

# ======================= 地域对照（库存检查打印用） =======================
REGION_MAP = {1: "广州", 4: "上海", 8: "北京"}

# 北京时间时区。所有时间基准均用服务器时间换算，不依赖电脑本地时钟
BJ_TZ = timezone(timedelta(hours=8))

# ======================= 会话与凭证 =======================
session = requests.Session()


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
    try:
        resp = session.post(
            "https://act-api.cloud.tencent.com/dianshi/do-goods",
            json=do_data,
            headers=headers,
            timeout=10,
        )
        print(f"🎯 核心购买接口返回：{resp.text}")
        result = resp.json()
        if isinstance(result, dict):
            result["region_id"] = region_id  # 把地域ID附到结果上，方便定位哪个地域成功
        return result
    except Exception as e:
        print(f"❌ 核心购买接口调用失败：{e}")
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

    Date响应头按秒截断，直接用会低估真实偏移0~1秒。
    改用发送时刻对齐（offset = 服务器Date - 本地发送时刻），多次采样取最大值，
    误差可压到约±0.3秒。校准后「本地时间+偏移」即可跟踪服务器时间。
    """
    offsets = []
    for _ in range(samples):
        t0 = time.time() * 1000  # 本地发送时刻
        t = get_server_time()    # 服务器时间（Date头，秒级）
        if t is not None:
            offsets.append(t - t0)  # 用发送时刻对齐，抵消RTT和截断的影响
        time.sleep(0.15)
    return max(offsets) if offsets else None

# ======================= 高频抢购 =======================


def rush_buy(region_ids, duration, interval):
    """秒杀开始后高频暴力抢购：duration秒窗口内反复并发下单，抢到即停"""
    deadline = time.time() + duration
    round_no = 0
    with ThreadPoolExecutor(max_workers=len(region_ids)) as executor:
        while time.time() < deadline:
            round_no += 1
            print(f"⚡ 第{round_no}轮抢购 ...")
            futures = [executor.submit(buy_now, rid) for rid in region_ids]
            for future in futures:
                result = future.result()
                if isinstance(result, dict) and result.get("code") == 0:
                    print(f"🎉 抢购成功！地域ID: {result.get('region_id', '未知')}")
                    return result
            time.sleep(interval)
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
            lead_ms = int(RUSH_LEAD * 1000)
            while True:
                synced = time.time() * 1000 + offset  # 毫秒级服务器时间（本地时钟+校准偏移）
                if synced >= next_seckill - lead_ms:
                    print("🎯 开火！进入高频抢购窗口...")
                    result = rush_buy(REGION_IDS, RUSH_DURATION, RUSH_INTERVAL)
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
