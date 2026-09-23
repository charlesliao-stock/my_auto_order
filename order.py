import os
import re
import time
import datetime
from collections import Counter
from playwright.sync_api import sync_playwright
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

# 從 GitHub Secrets 讀取設定
USERNAME = os.environ.get("KMUH_USERNAME")
PASSWORD = os.environ.get("KMUH_PASSWORD")
MEAL_COUNT = os.environ.get("MEAL_COUNT", "2")  # 預設份數為 2
DEPT_TEL = os.environ.get("KMUH_DEPT_TEL", "6551")  # 固定分機號碼

_VALID_CAPTCHA_RE = re.compile(r"^[A-Z0-9]{4}$")

_STATUS_ICON = {"OK": "✅", "FAIL": "❌", "WARN": "⚠️", "INFO": "ℹ️"}


def log_detailed(step_label, status, detail="", log_lines=None, page=None):
    """更詳細的 log 紀錄，包含當前網址與精確毫秒時間戳記，方便追蹤異常當下的狀態。
    page.url 的存取包 try/except：如果 page 當下處於損毀/關閉狀態，
    不能讓「記錄錯誤」這個動作本身又拋出例外，蓋掉真正要記錄的錯誤原因。"""
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    if page:
        try:
            current_url = f" [URL: {page.url}]"
        except Exception:
            current_url = " [URL: 無法取得]"
    else:
        current_url = ""
    icon = _STATUS_ICON.get(status, "")
    line = f"[{ts}] {icon} {step_label}{current_url}" + (f"：{detail}" if detail else "")
    print(line)
    if log_lines is not None:
        log_lines.append(line)
    return line


def safe_screenshot(page, path, log_lines=None):
    """截圖失敗時只記錄、不拋出例外。避免「頁面本身已經有問題」時，連截圖都逾時，
    導致真正有用的錯誤原因被截圖失敗這個次要例外蓋掉。"""
    try:
        page.screenshot(path=path, timeout=10000)
        return True
    except Exception as e:
        log_detailed(f"截圖失敗（{path}）", "WARN", str(e), log_lines=log_lines, page=page)
        return False


def _otsu_threshold(gray_img):
    """純 Python 實作 Otsu 自動門檻值 (不依賴 numpy/OpenCV)，
    比固定門檻值更能適應每張驗證碼圖片亮度不一致的狀況。"""
    histogram = gray_img.histogram()
    total = sum(histogram)
    if total == 0:
        return 150  # 保底值

    sum_total = sum(i * histogram[i] for i in range(256))
    sum_bg, weight_bg = 0.0, 0
    max_variance, best_threshold = 0.0, 150

    for t in range(256):
        weight_bg += histogram[t]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += t * histogram[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if variance > max_variance:
            max_variance = variance
            best_threshold = t

    return best_threshold


def _preprocess_variants(image_path):
    """從同一張驗證碼原圖，產生多種前處理版本，增加辨識率。"""
    base = Image.open(image_path)
    base = base.resize((base.width * 4, base.height * 4), Image.Resampling.LANCZOS)
    gray = base.convert('L')

    gray = ImageEnhance.Contrast(gray).enhance(2.0)
    gray = ImageEnhance.Sharpness(gray).enhance(2.0)

    otsu_t = _otsu_threshold(gray)

    variants = []

    # 變體 1：Otsu 自動門檻二值化 + 中值濾波去雜訊
    bin_otsu = gray.point(lambda x, t=otsu_t: 0 if x < t else 255, '1')
    bin_otsu = bin_otsu.filter(ImageFilter.MedianFilter(size=3))
    variants.append(bin_otsu)

    # 變體 2：反相版本
    bin_otsu_inv = gray.point(lambda x, t=otsu_t: 255 if x < t else 0, '1')
    bin_otsu_inv = bin_otsu_inv.filter(ImageFilter.MedianFilter(size=3))
    variants.append(bin_otsu_inv)

    # 變體 3：固定門檻 150 的版本
    bin_fixed = gray.point(lambda x: 0 if x < 150 else 255, '1')
    bin_fixed = bin_fixed.filter(ImageFilter.MedianFilter(size=3))
    variants.append(bin_fixed)

    # 變體 4：Otsu 二值化 + 形態學開運算
    bin_denoised = bin_otsu.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    variants.append(bin_denoised)

    return variants


def solve_captcha(image_path):
    """使用多種影像前處理 + 多種 OCR 設定，取多數決結果"""
    try:
        variants = _preprocess_variants(image_path)
        psm_modes = [8, 7, 13]
        char_whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

        votes = Counter()
        for variant_img in variants:
            for psm in psm_modes:
                config = f'-c tessedit_char_whitelist={char_whitelist} --psm {psm}'
                try:
                    raw = pytesseract.image_to_string(variant_img, config=config)
                except Exception:
                    continue
                candidate = raw.strip().upper().replace(" ", "").replace("\n", "")
                if _VALID_CAPTCHA_RE.match(candidate):
                    votes[candidate] += 1

        if votes:
            best, best_count = votes.most_common(1)[0]
            print(f"驗證碼候選結果: {dict(votes)} -> 採用: {best}")
            return best

        fallback_config = f'-c tessedit_char_whitelist={char_whitelist} --psm 8'
        fallback_text = pytesseract.image_to_string(variants[0], config=fallback_config).strip()
        fallback = fallback_text.upper().replace(" ", "")[:4]
        print(f"驗證碼多數決無結果，使用備援辨識: {fallback}")
        return fallback
    except Exception as e:
        print(f"驗證碼辨識錯誤: {e}")
        return ""


def run_automation():
    os.makedirs("screenshots", exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                # 重試前開一個全新的分頁，避免上一次嘗試若卡在「導覽到一半」的異常狀態，
                # 連帶影響這一輪連最基本的登入頁都連不上（這正是上次 log 顯示的狀況：
                # 第2、3次嘗試失敗時的網址跟第1次卡住時完全相同，代表分頁沒有真正重新
                # 導覽成功）。context 沿用同一個瀏覽器 session（cookie），不會因此需要
                # 重新登入或遺失既有 session。
                try:
                    page.close()
                except Exception:
                    pass
                page = context.new_page()

            log_lines = []
            step_status = {
                "login": "未開始",
                "redirect": "未開始",
                "order_submit": "未開始",
            }

            try:
                log_detailed(f"=== 第 {attempt}/{max_retries} 次嘗試開始 ===", "INFO", log_lines=log_lines, page=page)

                # ---------- 步驟 1：登入與智慧等待 ----------
                try:
                    # 智慧等待：等待 networkidle 網路閒置，並將逾時延長至 60 秒
                    page.goto("https://www.kmuh.org.tw/Web/Webportal", wait_until="networkidle", timeout=60000)
                    
                    # 智慧等待：確保帳號欄位已完全渲染且可見
                    username_input = page.locator("#username")
                    username_input.wait_for(state="visible", timeout=30000)
                    
                    safe_screenshot(page, f"screenshots/1_login_page_{attempt}.png", log_lines)
                    log_detailed("步驟1-1 開啟登入頁", "OK", "登入頁面與欄位已完全載入", log_lines=log_lines, page=page)
                except Exception as e:
                    step_status["login"] = f"失敗：無法開啟登入頁或找不到帳號欄位（{e}）"
                    log_detailed("步驟1-1 開啟登入頁", "FAIL", str(e), log_lines=log_lines, page=page)
                    raise

                # 1-2. 擷取驗證碼圖片並進行 OCR 辨識
                captcha_img = page.locator("#kmuh-captcha-img")
                captcha_img.wait_for(state="visible", timeout=10000)
                captcha_img.screenshot(path=f"screenshots/captcha_{attempt}.png")
                captcha_code = solve_captcha(f"screenshots/captcha_{attempt}.png")
                log_detailed("步驟1-2 驗證碼辨識結果", "INFO", f"'{captcha_code}'", log_lines=log_lines, page=page)

                if len(captcha_code) != 4:
                    step_status["login"] = f"失敗：驗證碼辨識長度不正確（辨識出 '{captcha_code}'），重新整理重試"
                    log_detailed("步驟1-2 驗證碼長度檢查", "FAIL", "非 4 碼，重新整理驗證碼後重試", log_lines=log_lines, page=page)
                    _write_log(attempt, log_lines)
                    captcha_img.click()
                    page.wait_for_timeout(1500)
                    continue

                # 1-3. 填入帳號、密碼與驗證碼
                page.fill("#username", USERNAME)
                page.fill("#password", PASSWORD)
                page.fill("#kmuh-captcha", captcha_code)
                safe_screenshot(page, f"screenshots/2_filled_form_{attempt}.png", log_lines)
                log_detailed("步驟1-3 填寫帳密與驗證碼", "OK", log_lines=log_lines, page=page)

                # 1-4. 點擊登入按鈕
                page.click("#login")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                if page.locator("#username").is_visible():
                    step_status["login"] = "失敗：送出登入後仍停留在登入頁（可能驗證碼辨識錯誤或帳號密碼錯誤）"
                    log_detailed("步驟1 登入", "FAIL", "仍停留在登入頁，可能驗證碼或帳密錯誤", log_lines=log_lines, page=page)
                    safe_screenshot(page, f"screenshots/login_failed_{attempt}.png", log_lines)
                    _write_log(attempt, log_lines)
                    continue

                step_status["login"] = "成功"
                log_detailed("步驟1 登入", "OK", "登入成功", log_lines=log_lines, page=page)
                safe_screenshot(page, f"screenshots/3_logged_in.png", log_lines)

                # ---------- 步驟 2：點擊「高醫醫療體系訂餐系統」連結，接住新分頁 ----------
                # 確認過實際頁面上的連結是 target="_blank" 的 <a>，會另開新分頁：
                # <a href="/Web/WebPortal/Home/TranUrl?sysid=583&url=...&inDBName=ora92"
                #    target="_blank" title="高醫醫療體系訂餐系統">
                # 不能用 page.goto() 直接打同一個網址替代——真的點擊連結時瀏覽器會自動帶上
                # Referer 標頭，goto() 預設不會，若目標系統有做 Referer 來源檢查，
                # 行為就會不一樣（這很可能是先前轉址不穩定的真正原因）。
                login_tab = page
                try:
                    portal_link = login_tab.locator("a[title='高醫醫療體系訂餐系統']")
                    if portal_link.count() == 0:
                        # title 屬性萬一被改掉的備援：改抓 href 裡包含 TranUrl 的連結
                        portal_link = login_tab.locator("a[href*='TranUrl']").first
                    portal_link.wait_for(state="visible", timeout=15000)
                    with login_tab.context.expect_page(timeout=30000) as new_page_info:
                        portal_link.click()
                    order_tab = new_page_info.value
                    order_tab.wait_for_load_state("domcontentloaded", timeout=30000)

                    # 智慧等待選單出現且可見
                    shift_select = order_tab.locator("select[name='shift_no']")
                    shift_select.wait_for(state="visible", timeout=30000)

                    page = order_tab  # 後續步驟都改在這個新分頁上操作
                    step_status["redirect"] = "成功"
                    log_detailed("步驟2 轉址至訂餐系統", "OK", "已點擊連結並成功開啟新分頁，選單已載入", log_lines=log_lines, page=page)

                    # 原本登入用的分頁已經沒有用途，關掉保持乾淨（失敗不影響流程）
                    try:
                        login_tab.close()
                    except Exception:
                        pass
                except Exception as e:
                    step_status["redirect"] = f"失敗：點擊連結後找不到訂餐頁的餐別選單（shift_no），原始錯誤：{e}"
                    log_detailed("步驟2 轉址至訂餐系統", "FAIL", f"點擊連結或找不到 shift_no 選單：{e}", log_lines=log_lines, page=page)
                    safe_screenshot(page, f"screenshots/redirect_failed_{attempt}.png", log_lines)
                    _write_log(attempt, log_lines)
                    raise

                safe_screenshot(page, f"screenshots/4_order_system_home.png", log_lines)

                # ---------- 步驟 3：互動順序與智慧填寫 ----------
                
                # 3-1. 選擇餐別「午餐」 (shift_no = 2)
                with page.expect_navigation(wait_until="networkidle", timeout=20000):
                    page.select_option("select[name='shift_no']", "2")
                safe_screenshot(page, f"screenshots/5_lunch_selected.png", log_lines)
                log_detailed("步驟3-1 選擇餐別（午餐）", "OK", log_lines=log_lines, page=page)

                # 3-2. 選擇餐盒類別「健康均衡餐(葷)」 (value="266")
                try:
                    classkind_266 = page.locator("input[name='classkind'][value='266']")
                    classkind_266.wait_for(state="visible", timeout=10000)
                    if classkind_266.is_checked():
                        log_detailed("步驟3-2 選擇餐盒類別", "OK", "「健康均衡餐(葷)」已是預設選項", log_lines=log_lines, page=page)
                    else:
                        with page.expect_navigation(wait_until="networkidle", timeout=20000):
                            classkind_266.check()
                        log_detailed("步驟3-2 選擇餐盒類別", "OK", "已切換為「健康均衡餐(葷)」", log_lines=log_lines, page=page)
                except Exception as e:
                    log_detailed("步驟3-2 選擇餐盒類別", "FAIL", str(e), log_lines=log_lines, page=page)
                page.wait_for_timeout(1000)

                # 3-3. 填寫科室分機與尋找可訂日期份數
                order_date_selected = False
                try:
                    dept_input = page.locator("input[name='depttel']")
                    dept_input.wait_for(state="visible", timeout=10000)
                    dept_input.fill(DEPT_TEL)

                    qty_selects = page.locator("select[name^='odrpcs']")
                    count = qty_selects.count()

                    candidates = []  
                    for i in range(count):
                        sel = qty_selects.nth(i)
                        if sel.get_attribute("disabled") is not None:
                            continue  
                        name = sel.get_attribute("name") or ""
                        digits = "".join(ch for ch in name if ch.isdigit())
                        if not digits:
                            continue
                        option_values = [
                            v for v in sel.locator("option").evaluate_all(
                                "els => els.map(e => e.value)"
                            )
                            if v.strip().isdigit()
                        ]
                        candidates.append((int(digits), sel, option_values))

                    if candidates:
                        candidates.sort(key=lambda x: x[0], reverse=True)

                        chosen = None
                        for date_digits, sel, option_values in candidates:
                            if MEAL_COUNT in option_values:
                                chosen = (sel, MEAL_COUNT, date_digits)
                                break

                        if chosen is None:
                            latest_digits, latest_sel, latest_options = candidates[0]
                            if latest_options:
                                fallback_count = str(max(int(v) for v in latest_options))
                                chosen = (latest_sel, fallback_count, latest_digits)
                                log_detailed(
                                    "步驟3-3 選擇日期與份數", "WARN",
                                    f"最新可訂日期剩餘份數不足 {MEAL_COUNT} 份，改訂購剩餘可提供的 {fallback_count} 份",
                                    log_lines=log_lines, page=page
                                )

                        if chosen:
                            target_sel, target_count, target_digits = chosen
                            target_sel.select_option(target_count)
                            order_date_selected = True
                            log_detailed("步驟3-3 選擇日期與份數", "OK", f"日期(數字){target_digits} -> {target_count} 份", log_lines=log_lines, page=page)
                        else:
                            log_detailed("步驟3-3 選擇日期與份數", "FAIL", "找到開放中的日期，但剩餘份數選單為空", log_lines=log_lines, page=page)
                    else:
                        log_detailed("步驟3-3 選擇日期與份數", "FAIL", "目前所有日期的午餐皆已訂完（剩餘份數為 0）", log_lines=log_lines, page=page)
                except Exception as e:
                    log_detailed("步驟3-3 選擇日期與份數", "FAIL", str(e), log_lines=log_lines, page=page)

                safe_screenshot(page, f"screenshots/6_ready_to_submit.png", log_lines)

                # 3-4. 點擊送出按鈕 (B2)
                if not order_date_selected:
                    step_status["order_submit"] = "未送出：沒有找到可訂購的日期/份數，避免送出空白訂單"
                    log_detailed("步驟3-4 訂餐送出", "WARN", "沒有可訂日期，本次不送出", log_lines=log_lines, page=page)
                else:
                    submit_btn = page.locator("input[name='B2']")
                    submit_btn.wait_for(state="visible", timeout=10000)
                    submit_btn.click()
                    page.wait_for_timeout(3000)
                    safe_screenshot(page, f"screenshots/7_submitted.png", log_lines)
                    step_status["order_submit"] = "成功：已點擊送出訂單按鈕 (B2)"
                    log_detailed("步驟3-4 訂餐送出", "OK", "已成功點擊送出按鈕", log_lines=log_lines, page=page)

                # ---------- 本次嘗試總結 ----------
                log_detailed("=== 本次嘗試步驟總結 ===", "INFO", log_lines=log_lines, page=page)
                log_detailed("步驟1 登入", "OK" if step_status["login"] == "成功" else "FAIL", step_status["login"], log_lines=log_lines, page=page)
                log_detailed("步驟2 轉址訂餐系統", "OK" if step_status["redirect"] == "成功" else "FAIL", step_status["redirect"], log_lines=log_lines, page=page)
                log_detailed("步驟3 訂餐送出", "OK" if "成功" in step_status["order_submit"] else "WARN", step_status["order_submit"], log_lines=log_lines, page=page)
                _write_log(attempt, log_lines)

                print("【高醫員工餐自動訂餐流程執行完成】")
                browser.close()
                return True

            except Exception as e:
                log_detailed(f"第 {attempt} 次執行發生錯誤", "FAIL", str(e), log_lines=log_lines, page=page)
                log_detailed("=== 本次嘗試步驟總結（因錯誤中斷）===", "INFO", log_lines=log_lines, page=page)
                log_detailed("步驟1 登入", "OK" if step_status["login"] == "成功" else "FAIL", step_status["login"], log_lines=log_lines, page=page)
                log_detailed("步驟2 轉址訂餐系統", "OK" if step_status["redirect"] == "成功" else "FAIL", step_status["redirect"], log_lines=log_lines, page=page)
                log_detailed("步驟3 訂餐送出", "FAIL" if step_status["order_submit"] == "未開始" else "WARN", step_status["order_submit"], log_lines=log_lines, page=page)
                _write_log(attempt, log_lines)
                safe_screenshot(page, f"screenshots/error_attempt_{attempt}.png", log_lines)
                if attempt == max_retries:
                    print(f"【高醫員工餐自動訂餐失敗】已達最大重試次數，錯誤原因: {e}")
                    browser.close()
                    raise e
                time.sleep(5)

        browser.close()


def _write_log(attempt, log_lines):
    """把這次嘗試收集到的 log 寫成文字檔，跟截圖存在同一個資料夾"""
    try:
        with open(f"screenshots/log_attempt_{attempt}.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))
    except Exception as e:
        print(f"寫入 log 檔案失敗: {e}")


if __name__ == "__main__":
    run_automation()
