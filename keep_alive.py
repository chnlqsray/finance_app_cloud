"""
Streamlit 应用保活脚本 - 诊断版
在 networkidle + 等待后，详细打印 Playwright 眼中的 DOM 状态，
用于判断 locator 返回 0 的根本原因。
"""

import re
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URLS = [
    "https://my-finance-ai.streamlit.app/",
    "https://my-movie-ai.streamlit.app/",
]

PAGE_LOAD_TIMEOUT_MS = 60_000
RESTART_WAIT_SECONDS = 60
HEARTBEAT_STAY_SECONDS = 20


def wake_app(page, url: str, index: int) -> bool:
    print(f"\n[{index}] 正在访问：{url}", flush=True)
    try:
        print(f"[{index}] 等待页面完整渲染（networkidle）…", flush=True)
        page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
        print(f"[{index}] ✅ networkidle 触发，等待 3 秒让 React 完成渲染…", flush=True)
        page.wait_for_timeout(3000)

        # ── 截图① ────────────────────────────────────────────────────────
        page.screenshot(path=f"screenshot_{index}_1_after_load.png")
        print(f"[{index}] 截图①已保存", flush=True)

        # ── 诊断：用 JS 直接查询 DOM，打印所有按钮信息 ───────────────────
        btn_info = page.evaluate('''() => {
            const btns = document.querySelectorAll('button');
            return {
                total: btns.length,
                details: Array.from(btns).map(b => ({
                    testid: b.getAttribute('data-testid'),
                    text: b.innerText.trim().substring(0, 50),
                    visible: b.offsetParent !== null
                }))
            };
        }''')
        print(f"[{index}] 【诊断】JS查询到的按钮总数：{btn_info['total']}", flush=True)
        for i, b in enumerate(btn_info['details']):
            print(f"[{index}]   按钮{i}: testid={b['testid']!r}, text={b['text']!r}, visible={b['visible']}", flush=True)

        # ── 诊断：打印 page.locator 的计数 ───────────────────────────────
        loc1 = page.locator('button[data-testid="wakeup-button-owner"]')
        loc2 = page.locator('button[data-testid="wakeup-button"]')
        loc3 = page.get_by_role("button", name=re.compile("Yes, get this app back up", re.IGNORECASE))
        print(f"[{index}] 【诊断】locator(wakeup-button-owner).count() = {loc1.count()}", flush=True)
        print(f"[{index}] 【诊断】locator(wakeup-button).count() = {loc2.count()}", flush=True)
        print(f"[{index}] 【诊断】get_by_role(button, Yes...).count() = {loc3.count()}", flush=True)

        # ── 尝试点击：优先用 JS 直接点击（最可靠），其次用 locator ────────
        clicked = False

        # 方法1：JS 直接点击
        js_clicked = page.evaluate('''() => {
            const btn = document.querySelector('button[data-testid="wakeup-button-owner"]')
                     || document.querySelector('button[data-testid="wakeup-button"]');
            if (btn) { btn.click(); return true; }
            return false;
        }''')
        if js_clicked:
            print(f"[{index}] ✅ 方法1（JS直接点击）成功！", flush=True)
            clicked = True

        # 方法2：locator + force（JS失败时备用）
        if not clicked and loc1.count() > 0:
            loc1.first.click(force=True)
            print(f"[{index}] ✅ 方法2（locator wakeup-button-owner）成功！", flush=True)
            clicked = True

        if not clicked and loc3.count() > 0:
            loc3.first.click(force=True)
            print(f"[{index}] ✅ 方法3（get_by_role 文字匹配）成功！", flush=True)
            clicked = True

        if clicked:
            print(f"[{index}] 唤醒按钮已点击，等待 {RESTART_WAIT_SECONDS} 秒让应用重启…", flush=True)
            time.sleep(10)
            page.screenshot(path=f"screenshot_{index}_2_after_click.png")
            print(f"[{index}] 截图②已保存（点击唤醒后10秒）", flush=True)
            time.sleep(RESTART_WAIT_SECONDS - 10)
        else:
            print(f"[{index}] 未检测到休眠页，应用已处于活跃状态", flush=True)
            print(f"[{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…", flush=True)
            time.sleep(HEARTBEAT_STAY_SECONDS)

        page.screenshot(path=f"screenshot_{index}_3_final.png")
        print(f"[{index}] 截图③已保存（最终状态）", flush=True)
        print(f"[{index}] ✅ 保活完成", flush=True)
        return True

    except PlaywrightTimeoutError:
        print(f"[{index}] ⚠️ 页面渲染超时", flush=True)
        try:
            page.screenshot(path=f"screenshot_{index}_timeout.png")
        except Exception:
            pass
        return False

    except Exception as e:
        print(f"[{index}] ❌ 发生错误：{e}", flush=True)
        try:
            page.screenshot(path=f"screenshot_{index}_error.png")
        except Exception:
            pass
        return False


def main():
    print("=== Streamlit 保活脚本启动（诊断版）===", flush=True)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, url in enumerate(URLS, start=1):
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            success = wake_app(page, url, i)
            results.append((url, success))
            context.close()

        browser.close()

    print("\n=== 本次运行结果汇总 ===", flush=True)
    for url, success in results:
        status = "✅ 成功" if success else "⚠️ 失败"
        print(f"  {status}  {url}", flush=True)
    print("\n=== 脚本结束 ===", flush=True)


if __name__ == "__main__":
    main()
