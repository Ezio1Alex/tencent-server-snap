# -*- coding: utf-8 -*-
"""
登录腾讯云活动页，自动保存登录 Cookie 与 CSRF Token。

运行后浏览器会弹出二维码，用手机扫码登录即可。
凭证自动写入 cookies.json 与 csrf_token.txt，供 snap_up_server.py 使用。
"""

from playwright.sync_api import sync_playwright
import json
import sys
import time
import urllib.parse

# 活动页地址：腾讯云活动页地址每月会更换，失效时改成浏览器地址栏里的当前地址即可
ACTIVITY_URL = "https://cloud.tencent.com/act/pro/featured-202607"

# 登录页地址（自动拼装）：登录成功后浏览器会自动跳回活动页
LOGIN_URL = "https://cloud.tencent.com/login?s_url=" + urllib.parse.quote(ACTIVITY_URL, safe="")

# 捕获到的 CSRF Token（服务器按会话下发，与 cookies 绑定，每次登录都会变化）
csrf_token = {"value": ""}
logged_in = {"ok": False}


def auto_login():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # 监听 act-api 请求，捕获 x-csrf-token 头（登录后活动页会自动发起带 token 的请求）
        def on_request(req):
            if logged_in["ok"] and "act-api.cloud.tencent.com" in req.url:
                tok = req.headers.get("x-csrf-token")
                if tok:
                    csrf_token["value"] = tok

        page.on("request", on_request)

        page.goto(LOGIN_URL)
        print("请在浏览器中扫码登录...")

        # 等待登录成功后跳回活动页（活动页路径都以 /act/pro/ 开头）
        page.wait_for_url("**/act/pro/**", timeout=0)

        # 登录完成，开始捕获 token（给活动页脚本一点时间发请求），最多等 15 秒
        logged_in["ok"] = True
        deadline = time.time() + 15
        while not csrf_token["value"] and time.time() < deadline:
            page.wait_for_timeout(500)

        print("登录成功！")

        cookies = context.cookies()
        with open("cookies.json", "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

        # 保存 CSRF Token 到文件，snap_up_server.py 会自动读取（与 cookies 同一会话）
        with open("csrf_token.txt", "w", encoding="utf-8") as f:
            f.write(csrf_token["value"])

        if csrf_token["value"]:
            print(f"✅ CSRF Token 已保存: {csrf_token['value']}")
            print("✅ 登录完成！浏览器将自动关闭，随后自动启动抢购脚本...")
            page.wait_for_timeout(2000)  # 停留2秒，让用户看清提示
        else:
            print("⚠️ 未捕获到 CSRF Token！请检查活动页是否正常加载")
            page.wait_for_timeout(1000)
            sys.exit(1)  # 退出码1，让一键启动脚本检测到失败并停止


if __name__ == "__main__":
    auto_login()
