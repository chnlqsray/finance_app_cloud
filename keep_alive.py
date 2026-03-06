"""
Streamlit 应用保活脚本
使用 Playwright 启动真实 Chromium 浏览器，维持 WebSocket 连接，
防止应用进入休眠（12小时无活动触发）。

唤醒按钮 testid 说明：
- wakeup-button-owner：应用所有者身份访问时显示
- wakeup-button-viewer：普通访客身份访问时显示
- wakeup-button：旧版 Streamlit 的通用 testid（保留兼容）
以上三种均通过 get_by_role 文字匹配兜底，确保万无一失。
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
    """访问单个 Streamlit 应用，处理休眠拦截页，停留维持 WebSocket 连接。"""
    print(f"\n[{index}] 正在访问：{url}", flush=True)
    try:
        # networkidle 触发 = React 渲染完毕，是"页面已加载"的可靠信号
        print(f"[{index}] 等待页面完整渲染（networkidle）…", flush=True)
        page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)

        # networkidle 后 React 还需约 3 秒完成本地渲染，才能查到按钮
        page.wait_for_timeout(3000)

        # ── 截图①：确认页面状态 ───────────────────────────────────────────
        page.screenshot(path=f"screenshot_{index}_1_after_load.png")
        print(f"[{index}] 截图①已保存（页面渲染后）", flush=True)

        # ── 检测唤醒按钮（按优先级尝试三种 testid + 文字匹配兜底）─────────
        clicked = False

        # 方法1：testid 精确匹配（覆盖 owner / viewer / 旧版三种情况）
        for testid in ("wakeup-button-owner", "wakeup-button-viewer", "wakeup-button"):
            loc = page.locator(f'button[data-testid="{testid}"]')
            if loc.count() > 0:
                loc.first.click(force=True)
                print(f"[{index}] ✅ 检测到休眠页（testid={testid}），唤醒按钮已点击", flush=True)
                clicked = True
                break

        # 方法2：文字匹配兜底（testid 变化时仍然有效）
        if not clicked:
            loc = page.get_by_role(
                "button",
                name=re.compile("Yes, get this app back up", re.IGNORECASE)
            )
            if loc.count() > 0:
                loc.first.click(force=True)
                print(f"[{index}] ✅ 检测到休眠页（文字匹配），唤醒按钮已点击", flush=True)
                clicked = True

        if clicked:
            # ── 截图②：点击后10秒，确认唤醒是否触发 ─────────────────────
            time.sleep(10)
            page.screenshot(path=f"screenshot_{index}_2_after_click.png")
            print(f"[{index}] 截图②已保存（点击唤醒后10秒）", flush=True)

            # 等待剩余重启时间（实测60秒内完成）
            print(f"[{index}] 等待应用重启（共 {RESTART_WAIT_SECONDS} 秒）…", flush=True)
            time.sleep(RESTART_WAIT_SECONDS - 10)
        else:
            print(f"[{index}] 未检测到休眠页，应用已处于活跃状态", flush=True)
            print(f"[{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…", flush=True)
            time.sleep(HEARTBEAT_STAY_SECONDS)

        # ── 截图③：最终状态 ───────────────────────────────────────────────
        page.screenshot(path=f"screenshot_{index}_3_final.png")
        print(f"[{index}] 截图③已保存（最终状态）", flush=True)
        print(f"[{index}] ✅ 保活完成", flush=True)
        return True

    except PlaywrightTimeoutError:
        print(f"[{index}] ⚠️ 页面在 {PAGE_LOAD_TIMEOUT_MS // 1000} 秒内未完成渲染", flush=True)
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
    print("=== Streamlit 保活脚本启动 ===", flush=True)
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
