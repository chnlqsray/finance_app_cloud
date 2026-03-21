"""
Streamlit + HuggingFace Spaces 应用保活脚本
使用 Playwright 启动真实 Chromium 浏览器，维持 WebSocket 连接，
防止应用进入休眠。

【Streamlit 唤醒按钮 testid 说明】
- wakeup-button-owner：应用所有者身份访问时显示
- wakeup-button-viewer：普通访客身份访问时显示
- wakeup-button：旧版 Streamlit 的通用 testid（保留兼容）
以上三种均通过 get_by_role 文字匹配兜底，确保万无一失。

【HuggingFace 唤醒说明】
- 休眠时显示遮罩页，按钮文字为 "Restart this Space"
- 按钮位于 HuggingFace 页面层，不在 Streamlit iframe 内
- 重启时间较长，等待时间设为 90 秒
"""

import re
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Streamlit 应用列表 ────────────────────────────────────────────────────────
STREAMLIT_URLS = [
    "https://my-finance-ai.streamlit.app/",
    "https://my-movie-ai.streamlit.app/",
]

# ── HuggingFace Spaces 列表 ───────────────────────────────────────────────────
HF_URLS = [
    "https://huggingface.co/spaces/chnlqsray/finance-dashboard",
    "https://huggingface.co/spaces/chnlqsray/movie-radar",
]

PAGE_LOAD_TIMEOUT_MS   = 60_000
RESTART_WAIT_SECONDS   = 60
HF_RESTART_WAIT_SECONDS = 90
HEARTBEAT_STAY_SECONDS = 20


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit 保活逻辑
# ══════════════════════════════════════════════════════════════════════════════

def wake_streamlit(page, url: str, index: int) -> bool:
    """访问单个 Streamlit 应用，处理休眠拦截页，停留维持 WebSocket 连接。"""
    print(f"\n[ST-{index}] 正在访问：{url}", flush=True)
    try:
        print(f"[ST-{index}] 等待页面完整渲染（networkidle）…", flush=True)
        page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_timeout(3000)

        page.screenshot(path=f"screenshot_st{index}_1_after_load.png")
        print(f"[ST-{index}] 截图①已保存（页面渲染后）", flush=True)

        clicked = False

        # 方法1：testid 精确匹配
        for testid in ("wakeup-button-owner", "wakeup-button-viewer", "wakeup-button"):
            loc = page.locator(f'button[data-testid="{testid}"]')
            if loc.count() > 0:
                loc.first.click(force=True)
                print(f"[ST-{index}] ✅ 检测到休眠页（testid={testid}），唤醒按钮已点击", flush=True)
                clicked = True
                break

        # 方法2：文字匹配兜底
        if not clicked:
            loc = page.get_by_role(
                "button",
                name=re.compile("Yes, get this app back up", re.IGNORECASE)
            )
            if loc.count() > 0:
                loc.first.click(force=True)
                print(f"[ST-{index}] ✅ 检测到休眠页（文字匹配），唤醒按钮已点击", flush=True)
                clicked = True

        if clicked:
            time.sleep(10)
            page.screenshot(path=f"screenshot_st{index}_2_after_click.png")
            print(f"[ST-{index}] 截图②已保存（点击唤醒后10秒）", flush=True)
            print(f"[ST-{index}] 等待应用重启（共 {RESTART_WAIT_SECONDS} 秒）…", flush=True)
            time.sleep(RESTART_WAIT_SECONDS - 10)
        else:
            print(f"[ST-{index}] 未检测到休眠页，应用已处于活跃状态", flush=True)
            print(f"[ST-{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…", flush=True)
            time.sleep(HEARTBEAT_STAY_SECONDS)

        page.screenshot(path=f"screenshot_st{index}_3_final.png")
        print(f"[ST-{index}] 截图③已保存（最终状态）", flush=True)
        print(f"[ST-{index}] ✅ 保活完成", flush=True)
        return True

    except PlaywrightTimeoutError:
        print(f"[ST-{index}] ⚠️ 页面在 {PAGE_LOAD_TIMEOUT_MS // 1000} 秒内未完成渲染", flush=True)
        try:
            page.screenshot(path=f"screenshot_st{index}_timeout.png")
        except Exception:
            pass
        return False

    except Exception as e:
        print(f"[ST-{index}] ❌ 发生错误：{e}", flush=True)
        try:
            page.screenshot(path=f"screenshot_st{index}_error.png")
        except Exception:
            pass
        return False


# ══════════════════════════════════════════════════════════════════════════════
# HuggingFace 保活逻辑
# ══════════════════════════════════════════════════════════════════════════════

def wake_huggingface(page, url: str, index: int) -> bool:
    """
    访问 HuggingFace Space，检测休眠遮罩并点击 'Restart this Space' 唤醒。
    休眠时 HuggingFace 在页面层显示遮罩，按钮不在 Streamlit iframe 内。
    """
    print(f"\n[HF-{index}] 正在访问：{url}", flush=True)
    try:
        print(f"[HF-{index}] 等待页面基础加载（load）…", flush=True)
        page.goto(url, wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_timeout(3000)

        page.screenshot(path=f"screenshot_hf{index}_1_after_load.png")
        print(f"[HF-{index}] 截图①已保存（页面渲染后）", flush=True)

        clicked = False

        # 方法1：按钮文字精确匹配（HuggingFace 官方文案）
        for btn_text in ("Restart this Space", "Wake up"):
            loc = page.get_by_role(
                "button",
                name=re.compile(btn_text, re.IGNORECASE)
            )
            if loc.count() > 0:
                loc.first.click(force=True)
                print(f"[HF-{index}] ✅ 检测到休眠遮罩（按钮文字='{btn_text}'），唤醒按钮已点击", flush=True)
                clicked = True
                break

        # 方法2：通过包含关键词的任意元素兜底
        if not clicked:
            loc = page.locator("button", has_text=re.compile("restart|wake", re.IGNORECASE))
            if loc.count() > 0:
                loc.first.click(force=True)
                print(f"[HF-{index}] ✅ 检测到休眠遮罩（关键词兜底），唤醒按钮已点击", flush=True)
                clicked = True

        if clicked:
            time.sleep(15)
            page.screenshot(path=f"screenshot_hf{index}_2_after_click.png")
            print(f"[HF-{index}] 截图②已保存（点击唤醒后15秒）", flush=True)
            print(f"[HF-{index}] 等待 Space 重启（共 {HF_RESTART_WAIT_SECONDS} 秒）…", flush=True)
            time.sleep(HF_RESTART_WAIT_SECONDS - 15)
        else:
            print(f"[HF-{index}] 未检测到休眠遮罩，Space 已处于活跃状态", flush=True)
            print(f"[HF-{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒…", flush=True)
            time.sleep(HEARTBEAT_STAY_SECONDS)

        page.screenshot(path=f"screenshot_hf{index}_3_final.png")
        print(f"[HF-{index}] 截图③已保存（最终状态）", flush=True)
        print(f"[HF-{index}] ✅ 保活完成", flush=True)
        return True

    except PlaywrightTimeoutError:
        print(f"[HF-{index}] ⚠️ 页面在 {PAGE_LOAD_TIMEOUT_MS // 1000} 秒内未完成渲染", flush=True)
        try:
            page.screenshot(path=f"screenshot_hf{index}_timeout.png")
        except Exception:
            pass
        return False

    except Exception as e:
        print(f"[HF-{index}] ❌ 发生错误：{e}", flush=True)
        try:
            page.screenshot(path=f"screenshot_hf{index}_error.png")
        except Exception:
            pass
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 主函数
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=== 保活脚本启动（Streamlit + HuggingFace）===", flush=True)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ── Streamlit 应用 ────────────────────────────────────────────────
        print("\n─── Streamlit 应用保活 ───", flush=True)
        for i, url in enumerate(STREAMLIT_URLS, start=1):
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            success = wake_streamlit(page, url, i)
            results.append(("Streamlit", url, success))
            context.close()

        # ── HuggingFace Spaces ────────────────────────────────────────────
        print("\n─── HuggingFace Spaces 保活 ───", flush=True)
        for i, url in enumerate(HF_URLS, start=1):
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            success = wake_huggingface(page, url, i)
            results.append(("HuggingFace", url, success))
            context.close()

        browser.close()

    print("\n=== 本次运行结果汇总 ===", flush=True)
    for platform, url, success in results:
        status = "✅ 成功" if success else "⚠️ 失败"
        print(f"  {status}  [{platform}]  {url}", flush=True)
    print("\n=== 脚本结束 ===", flush=True)


if __name__ == "__main__":
    main()
