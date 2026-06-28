import os, re, logging, random, json, time, threading, sys
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

EMAIL    = os.environ["EMAIL"]
PASSWORD = os.environ["PASSWORD"]

DASH_URL    = "https://dash.waifly.com"
LOGIN_URL   = f"{DASH_URL}/login"
INDEX_URL   = f"{DASH_URL}/index"
SERVERS_URL = f"{DASH_URL}/servers"

# 代理（可选）：如果 Waifly 对 GitHub Actions IP 有风控，可设置 USE_PROXY=1
PROXY_SERVER = "socks5://127.0.0.1:10808"
USE_PROXY = os.environ.get("USE_PROXY", "0") == "1"

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ---------- WxPusher 推送 ----------
WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID   = os.environ.get("WXPUSHER_UID", "")

def wxpush(content: str):
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        log.warning("📨 WXPUSHER_TOKEN 或 WXPUSHER_UID 未配置，跳过推送")
        return
    import urllib.request
    payload = json.dumps({
        "appToken": WXPUSHER_TOKEN,
        "content":  content,
        "contentType": 1,
        "uids": [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info("📨 WxPusher 推送成功")
            else:
                log.warning(f"📨 WxPusher 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

# ---------- 工具函数 ----------
def take_screenshot(page, name):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path, full_page=False)
        log.info(f"📸 截图: {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

def get_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""

def human_delay(min_s=0.3, max_s=0.8):
    time.sleep(random.uniform(min_s, max_s))

def wait_for_url_contains(page, keyword, timeout=10) -> bool:
    try:
        page.wait_for_url(f"**{keyword}**", timeout=timeout * 1000)
        return True
    except Exception:
        return keyword in page.url

def js_click(page, selector, desc="") -> bool:
    try:
        result = page.evaluate(f"""() => {{
            var el = document.querySelector('{selector}');
            if (el) {{ el.click(); return true; }}
            return false;
        }}""")
        if result:
            log.info(f"JS 点击成功: {desc or selector}")
            return True
    except Exception as e:
        log.warning(f"JS 点击失败 [{desc}]: {e}")
    return False

def wait_for_page_settle(page, settle_timeout=8):
    deadline = time.time() + settle_timeout
    while time.time() < deadline:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        if len(body.strip()) > 50:
            return
        time.sleep(0.5)

def navigate(page, url, timeout=30) -> bool:
    log.info(f"导航到: {url}")
    try:
        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"goto 超时/异常: {e}，继续等待...")
    wait_for_page_settle(page, settle_timeout=10)
    return True

def handle_privacy_consent(page, timeout=8) -> bool:
    """
    rgpd.js 弹出的"Privacy Consent Required"弹窗会盖在登录表单上方，
    不点掉的话后面 email/password 输入和登录按钮点击都点不到实际元素上。
    优先点 "I Accept"，找不到再退而求其次找任何包含 Accept 文案的按钮。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = js_click_by_text(page, "button", "I Accept", "隐私弹窗-I Accept")
        if clicked:
            log.info("✅ 已点击隐私同意弹窗")
            human_delay(0.5, 1)
            return True
        # 弹窗还没渲染出来，或者本来就没有弹窗，短暂等待后再检查一次
        try:
            visible = page.locator("text=Privacy Consent Required").first.is_visible(timeout=500)
        except Exception:
            visible = False
        if not visible:
            return False
        time.sleep(0.5)
    log.warning("隐私弹窗一直存在但未能点击成功")
    return False

def js_click_by_text(page, tag, text, desc="") -> bool:
    try:
        result = page.evaluate(f"""() => {{
            var els = document.querySelectorAll('{tag}');
            for (var el of els) {{
                if (el.innerText && el.innerText.trim().includes('{text}') && el.offsetParent !== null) {{
                    el.click();
                    return true;
                }}
            }}
            return false;
        }}""")
        if result:
            log.info(f"JS 点击成功: {desc or text}")
            return True
    except Exception as e:
        log.warning(f"JS 点击失败 [{desc or text}]: {e}")
    return False

# ---------- 登录 ----------
def login(page, max_retries=3) -> bool:
    for attempt in range(1, max_retries + 1):
        log.info(f"登录 {attempt}/{max_retries}")
        navigate(page, LOGIN_URL)
        handle_privacy_consent(page)

        try:
            page.wait_for_selector('input#email, input[name="email"]', timeout=10000)
        except Exception:
            log.warning("找不到邮箱输入框，重试")
            take_screenshot(page, f"login_fail_{attempt}")
            continue

        # 再保险一次：有些情况下弹窗在邮箱输入框出现之后才渲染出来
        handle_privacy_consent(page, timeout=3)

        # 人性化输入（CloakBrowser humanize=True 配合真实点击+逐字输入）
        email_el = page.locator('input#email, input[name="email"]').first
        email_el.click()
        email_el.fill("")
        page.type('input#email, input[name="email"]', EMAIL, delay=random.randint(60, 140))
        human_delay()

        pass_el = page.locator('input#password, input[name="password"]').first
        pass_el.click()
        pass_el.fill("")
        page.type('input#password, input[name="password"]', PASSWORD, delay=random.randint(60, 140))
        human_delay()

        take_screenshot(page, f"01_filled_{attempt}")

        try:
            page.locator("button#createAccount").first.click()
        except Exception as e:
            log.warning(f"登录按钮 human click 失败，降级 js_click: {e}")
            js_click(page, "button#createAccount", "登录按钮")

        log.info("已点击登录，检查跳转...")

        # 登录成功会跳到 /index；如果有错误提示会停留在 /login
        if wait_for_url_contains(page, "/index", 15):
            log.info("✅ 登录成功")
            take_screenshot(page, "02_login_success")
            return True

        # 检查页面是否给出了错误信息（比如密码错误、需要验证码等）
        err_text = ""
        try:
            err_text = page.locator("small .error").first.inner_text(timeout=1000).strip()
        except Exception:
            pass
        if err_text:
            log.warning(f"登录错误提示: {err_text}")

        log.warning("登录后未跳转，重试")
        take_screenshot(page, f"login_no_redirect_{attempt}")
        human_delay(1, 2)

    return False

# ---------- 检查服务器在线状态 ----------
def check_server_status(page, context):
    """
    去 /servers 页找到服务器列表，点击预览(眼睛图标)，
    跳到 panel.waifly.com/server/{id} 的控制台页，读取运行状态。
    eye 图标可能在当前标签页跳转，也可能开新标签页，两种都兼容处理。
    """
    log.info("前往服务器列表页...")
    navigate(page, SERVERS_URL)
    take_screenshot(page, "03_servers_list")

    try:
        page.wait_for_selector("table", timeout=10000)
    except Exception:
        log.warning("服务器列表未加载出来")
        return None, None

    # 找到第一行服务器名字 + 预览(眼睛)按钮
    server_name = None
    try:
        server_name = page.locator("table tbody tr").first.locator("td").first.inner_text(timeout=3000).strip()
    except Exception:
        pass
    log.info(f"检测到服务器: {server_name}")

    panel_page = None
    try:
        with context.expect_page(timeout=8000) as new_page_info:
            # 眼睛图标通常是 actions 列里第一个可点击元素（svg/button）
            clicked = (
                js_click(page, "table tbody tr td:last-child a:first-child", "预览按钮(a)")
                or js_click(page, "table tbody tr td:last-child button:first-child", "预览按钮(button)")
                or js_click(page, "table tbody tr td:last-child svg:first-child", "预览图标(svg)")
            )
            if not clicked:
                # 兜底：直接点 actions 列里第一个子元素
                page.evaluate("""() => {
                    var row = document.querySelector('table tbody tr');
                    if (!row) return;
                    var actions = row.querySelectorAll('td')[row.querySelectorAll('td').length - 1];
                    var el = actions && actions.querySelector('*');
                    if (el) el.click();
                }""")
        panel_page = new_page_info.value
        log.info("预览按钮在新标签页打开了控制台")
    except Exception:
        log.info("没有打开新标签页，假设在当前页跳转")
        panel_page = page

    try:
        panel_page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    wait_for_page_settle(panel_page, settle_timeout=10)
    take_screenshot(panel_page, "04_panel_console")

    # 读取控制台输出 + 运行状态
    body = get_text(panel_page)
    is_running = bool(re.search(r"App is running", body, re.IGNORECASE))
    # Stop 按钮可点击(非disabled) 通常意味着正在运行；Start 高亮则代表已停止
    stop_disabled = None
    try:
        stop_disabled = panel_page.locator("button:has-text('Stop')").first.is_disabled(timeout=2000)
    except Exception:
        pass

    status = "在线" if is_running or stop_disabled is False else "未知/可能离线"
    log.info(f"服务器状态: {status} (App is running 文本命中: {is_running}, Stop按钮可点击: {stop_disabled})")

    if panel_page is not page:
        try:
            panel_page.close()
        except Exception:
            pass

    return server_name, status

# ---------- 看门狗：防止某一步卡死后被外层 shell timeout 硬杀、什么都没留下 ----------
WATCHDOG_SECONDS = int(os.environ.get("WATCHDOG_SECONDS", "200"))

def _watchdog(page_holder):
    log.error(f"⏰ 看门狗触发：超过 {WATCHDOG_SECONDS}s 仍未结束，强制截图并退出")
    page = page_holder.get("page")
    if page is not None:
        try:
            take_screenshot(page, "WATCHDOG_TIMEOUT")
        except Exception as e:
            log.warning(f"看门狗截图失败: {e}")
    wxpush(f"⏰ Waifly 保活任务卡死超时（>{WATCHDOG_SECONDS}s），已强制终止，请查看截图排查")
    # os._exit 而不是 sys.exit：保证哪怕主线程卡在某个同步调用里（比如浏览器无响应），
    # 进程也能立刻退出，不会一直占着 runner 等到外层 270s shell timeout
    os._exit(1)

# ---------- 主流程 ----------
def main():
    from cloakbrowser import launch

    page_holder = {"page": None}
    timer = threading.Timer(WATCHDOG_SECONDS, _watchdog, args=(page_holder,))
    timer.daemon = True
    timer.start()

    log.info("启动 CloakBrowser（源码级指纹伪装）...")
    launch_kwargs = dict(headless=False, humanize=True, geoip=True)
    if USE_PROXY:
        launch_kwargs["proxy"] = PROXY_SERVER
    browser = launch(**launch_kwargs)
    context = browser  # cloakbrowser 的 launch() 返回的对象同时充当 browser/context
    page = browser.new_page()
    page_holder["page"] = page

    try:
        if not login(page):
            wxpush("❌ Waifly 登录失败，请检查账号密码")
            return

        server_name, status = check_server_status(page, context)

        lines = ["✅ Waifly 保活任务完成"]
        if server_name:
            lines.append(f"服务器: {server_name}")
        if status:
            lines.append(f"状态: {status}")
        wxpush("\n".join(lines))

    except Exception as e:
        log.exception(e)
        take_screenshot(page, "99_error")
        wxpush(f"❌ Waifly 保活任务异常: {e}")
    finally:
        timer.cancel()
        time.sleep(5)
        browser.close()
        log.info("任务结束")

if __name__ == "__main__":
    main()
