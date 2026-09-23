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

# 餐盒類別代碼 -> 顯示名稱，目前程式固定訂購 266（健康均衡餐(葷)），
# 這裡做成對照表方便訂餐結果回報時顯示中文名稱，之後若要改訂 267 也只需改這一個常數
CLASSKIND_VALUE = "266"
CLASSKIND_NAME_MAP = {"266": "健康均衡餐(葷)", "267": "低脂窈窕餐(葷)"}


def roc_digits_to_date_str(digits):
    """把 odrpcs<民國年月日> 的數字（例如 1150930）轉成一般看得懂的日期字串（例如 2026/9/30）"""
    try:
        s = str(digits).zfill(7)  # 民國年(3碼) + 月(2碼) + 日(2碼)
        roc_year = int(s[:-4])
        month = int(s[-4:-2])
        day = int(s[-2:])
        return f"{roc_year + 1911}/{month}/{day}"
    except Exception:
        return f"民國年月日代碼:{digits}"


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


# ---------- 每日首次登入的「體溫填報」彈窗處理 ----------
# 每天 00:00 後第一次登入，入口網站會跳出 #system-hint-modal（Bootstrap modal，
# data-bs-backdrop="static"、data-bs-keyboard="false"），會蓋住整個頁面，
# 導致後面點「高醫醫療體系訂餐系統」連結時一直被攔截、逾時。
#
# 依入口網頁原始碼，彈窗結構如下：
#   - 「無，我的體溫(°C)是:」單選鈕帶 data-show-temp，點了會展開體溫表(#temp-table)
#   - 體溫表內每個溫度是 [data-upload-temp="36"] 之類的元素，點下去就 AJAX 送出
#   - 「體溫超過37.5°C…」單選鈕帶 data-show-url，會【開新分頁】跳到體溫網站，絕對不能點
#   - 「進行填寫」(#overtime-fill) 也是開新分頁，不能點
#   - 若同時有延長工時提醒(#overtime-hint)，送出體溫後彈窗不會自動關，要按右上角 ×
#     (.portal-modal-close)
TEMPERATURE_VALUE = os.environ.get("KMUH_TEMPERATURE", "36")

_FORCE_CLOSE_MODAL_JS = """
() => {
  document.querySelectorAll('#system-hint-modal, .modal.show').forEach(m => m.remove());
  document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
  document.body.classList.remove('modal-open');
  document.body.style.removeProperty('overflow');
  document.body.style.removeProperty('padding-right');
}
"""


def _pick_temperature_value(options, wanted):
    """從頁面上實際存在的 data-upload-temp 值裡，挑出等於 wanted 的；
    沒有完全相同的就挑數值最接近、且不超過 37.4 的（避免誤報發燒）。"""
    parsed = []
    for o in options:
        try:
            parsed.append((float(o), o))
        except ValueError:
            continue
    if not parsed:
        return None
    w = float(wanted)
    for v, raw in parsed:
        if v == w:
            return raw
    safe = [(abs(v - w), raw) for v, raw in parsed if v <= 37.4]
    return min(safe)[1] if safe else None


def _submit_temperature(page, modal, log_lines):
    """展開體溫表 -> 點選 36 度。成功回傳實際點的值，失敗丟例外。"""
    # 1) 點「無，我的體溫(°C)是:」（不是 data-show-url 那個會開新分頁的選項）
    show_temp = modal.locator("[data-show-temp]").first
    if show_temp.count() > 0:
        show_temp.click(force=True, timeout=5000)
    else:
        modal.locator("label", has_text=re.compile(r"^\s*無，我的體溫")).first.click(timeout=5000)

    # 2) 等體溫表出現，挑出 36
    modal.locator("[data-upload-temp]").first.wait_for(state="visible", timeout=8000)
    options = modal.locator("[data-upload-temp]").evaluate_all(
        "els => els.map(e => e.getAttribute('data-upload-temp'))"
    )
    chosen = _pick_temperature_value(options, TEMPERATURE_VALUE)
    if chosen is None:
        raise RuntimeError(f"體溫表中找不到可用的選項（現有選項：{options}）")

    # 3) 點下去送出；成功時網站會把 #tempature-panel 隱藏
    modal.locator(f"[data-upload-temp='{chosen}']").first.click(timeout=5000)
    page.locator("#tempature-panel").wait_for(state="hidden", timeout=15000)
    # 送出時網站會蓋一層 blockUI 遮罩，等它消失再繼續
    try:
        page.locator(".blockUI").first.wait_for(state="detached", timeout=8000)
    except Exception:
        pass
    return chosen


def dismiss_system_hint_modal(page, log_lines=None, wait_ms=8000):
    """處理每日首次登入的體溫彈窗。
    方案 2（優先）：點「無，我的體溫」-> 點 36 度送出 -> 按 × 關閉彈窗。
    方案 1（備援）：任何一步失敗，就用 JS 直接把彈窗與遮罩從 DOM 移除。
    沒出現彈窗（例如當天已填過）就直接略過，回傳 False；有處理回傳 True。"""
    modal = page.locator("#system-hint-modal")
    try:
        modal.wait_for(state="visible", timeout=wait_ms)
    except Exception:
        log_detailed("步驟1-5 檢查體溫彈窗", "INFO", "未出現彈窗，略過", log_lines=log_lines, page=page)
        return False

    safe_screenshot(page, "screenshots/popup_before.png", log_lines)
    log_detailed("步驟1-5 檢查體溫彈窗", "INFO", "偵測到彈窗，嘗試送出體溫", log_lines=log_lines, page=page)

    # 方案 2：送出體溫（若彈窗裡本來就沒有體溫區塊，例如只剩延長工時提醒，就直接關閉）
    try:
        if modal.locator("[data-show-temp], [data-upload-temp]").count() > 0:
            chosen = _submit_temperature(page, modal, log_lines)
            log_detailed("步驟1-5 體溫彈窗", "OK", f"已送出體溫 {chosen}", log_lines=log_lines, page=page)
        else:
            log_detailed("步驟1-5 體溫彈窗", "INFO", "彈窗內沒有體溫區塊，直接關閉", log_lines=log_lines, page=page)

        # 送出後若還有其他提醒，彈窗不會自己關，手動按右上角 ×
        try:
            modal.wait_for(state="hidden", timeout=3000)
        except Exception:
            modal.locator(".portal-modal-close").first.click(timeout=5000)
            modal.wait_for(state="hidden", timeout=8000)
        page.wait_for_timeout(500)
        log_detailed("步驟1-5 體溫彈窗", "OK", "彈窗已關閉", log_lines=log_lines, page=page)
        safe_screenshot(page, "screenshots/popup_after.png", log_lines)
        return True
    except Exception as e:
        log_detailed("步驟1-5 體溫彈窗", "WARN", f"正常流程失敗，改用強制關閉：{e}", log_lines=log_lines, page=page)

    # 方案 1：強制關閉
    try:
        page.evaluate(_FORCE_CLOSE_MODAL_JS)
        page.wait_for_timeout(500)
        if page.locator("#system-hint-modal").count() == 0:
            log_detailed("步驟1-5 體溫彈窗", "OK", "已強制移除彈窗", log_lines=log_lines, page=page)
            safe_screenshot(page, "screenshots/popup_after.png", log_lines)
            return True
    except Exception as e:
        log_detailed("步驟1-5 體溫彈窗", "FAIL", f"強制關閉也失敗：{e}", log_lines=log_lines, page=page)
    return False


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

                # 每日首次登入會跳出體溫彈窗，必須先處理掉，否則後面的連結會被攔截
                dismiss_system_hint_modal(page, log_lines)

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
                        portal_link = login_tab.locator("a[href*='TranUrl']")
                    # 頁面上這個連結可能同時出現在多個地方（例如「常用功能」跟主選單各一個），
                    # 內容完全相同、指向同一個網址，取第一個即可，避免 Playwright 嚴格模式
                    # 因為比對到多個元素而報錯（strict mode violation）
                    portal_link = portal_link.first
                    portal_link.wait_for(state="visible", timeout=15000)
                    # 保險：彈窗若延遲出現（或上一步沒處理到），點連結前再確認一次
                    dismiss_system_hint_modal(login_tab, log_lines, wait_ms=1500)
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
                # 跟 3-2 選餐盒類別一樣的道理：OnChange 只有在「值真的改變」時才會觸發
                # form11.submit() 整頁重載。先判斷目前是否已經是 "2"，避免萬一伺服器 session
                # 記得上次選過午餐、頁面一進來就是 "2" 時，白白空等一次不會發生的導覽到逾時。
                shift_select_el = page.locator("select[name='shift_no']")
                if shift_select_el.input_value() == "2":
                    log_detailed("步驟3-1 選擇餐別（午餐）", "OK", "已是預設選項，無需切換", log_lines=log_lines, page=page)
                else:
                    with page.expect_navigation(wait_until="networkidle", timeout=20000):
                        page.select_option("select[name='shift_no']", "2")
                    log_detailed("步驟3-1 選擇餐別（午餐）", "OK", "已切換為午餐", log_lines=log_lines, page=page)
                safe_screenshot(page, f"screenshots/5_lunch_selected.png", log_lines)

                # 3-2. 選擇餐盒類別「健康均衡餐(葷)」 (value="266")
                try:
                    classkind_266 = page.locator(f"input[name='classkind'][value='{CLASSKIND_VALUE}']")
                    classkind_266.wait_for(state="visible", timeout=10000)
                    if classkind_266.is_checked():
                        log_detailed("步驟3-2 選擇餐盒類別", "OK", f"「{CLASSKIND_NAME_MAP[CLASSKIND_VALUE]}」已是預設選項", log_lines=log_lines, page=page)
                    else:
                        with page.expect_navigation(wait_until="networkidle", timeout=20000):
                            classkind_266.check()
                        log_detailed("步驟3-2 選擇餐盒類別", "OK", f"已切換為「{CLASSKIND_NAME_MAP[CLASSKIND_VALUE]}」", log_lines=log_lines, page=page)
                except Exception as e:
                    log_detailed("步驟3-2 選擇餐盒類別", "FAIL", str(e), log_lines=log_lines, page=page)
                page.wait_for_timeout(1000)

                # 3-3. 填寫科室分機，並鎖定「最後一筆可訂購日期」（更早的日期一律不理會）
                order_date_selected = False
                order_report = None  # (日期數字, 份數) 供 3-4 組回報訊息用
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
                            # disabled 可能代表「當天已無剩餘份數」或「已過當天訂購截止時間」，
                            # 兩種情況都無法訂購，這裡不區分原因，一律跳過
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
                        # 只鎖定日期最新（最後一筆）的可訂購日期，不再往前找其他日期
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        target_digits, target_sel, target_options = candidates[0]

                        if MEAL_COUNT in target_options:
                            target_count = MEAL_COUNT
                        elif target_options:
                            target_count = str(max(int(v) for v in target_options))
                            log_detailed(
                                "步驟3-3 選擇日期與份數", "WARN",
                                f"最後一筆可訂購日期（{roc_digits_to_date_str(target_digits)}）"
                                f"剩餘份數不足 {MEAL_COUNT} 份，改訂購剩餘可提供的 {target_count} 份",
                                log_lines=log_lines, page=page,
                            )
                        else:
                            target_count = None

                        if target_count:
                            target_sel.select_option(target_count)
                            order_date_selected = True
                            order_report = (target_digits, target_count)
                            log_detailed(
                                "步驟3-3 選擇日期與份數", "OK",
                                f"{roc_digits_to_date_str(target_digits)} -> {target_count} 份",
                                log_lines=log_lines, page=page,
                            )
                        else:
                            log_detailed("步驟3-3 選擇日期與份數", "FAIL", "最後一筆可訂購日期的份數選單為空", log_lines=log_lines, page=page)
                    else:
                        log_detailed("步驟3-3 選擇日期與份數", "FAIL", "目前所有日期皆無法訂購（可能已截止收單，或剩餘份數為 0）", log_lines=log_lines, page=page)
                except Exception as e:
                    log_detailed("步驟3-3 選擇日期與份數", "FAIL", str(e), log_lines=log_lines, page=page)

                safe_screenshot(page, f"screenshots/6_ready_to_submit.png", log_lines)

                # 3-4. 點擊送出按鈕 (B2)
                order_summary = None
                if not order_date_selected:
                    step_status["order_submit"] = "未送出：沒有找到可訂購的日期/份數，避免送出空白訂單"
                    log_detailed("步驟3-4 訂餐送出", "WARN", "沒有可訂日期，本次不送出", log_lines=log_lines, page=page)
                else:
                    submit_btn = page.locator("input[name='B2']")
                    submit_btn.wait_for(state="visible", timeout=10000)
                    submit_btn.click()
                    page.wait_for_timeout(3000)
                    safe_screenshot(page, f"screenshots/7_submitted.png", log_lines)

                    target_digits, target_count = order_report
                    order_date_str = roc_digits_to_date_str(target_digits)
                    classkind_name = CLASSKIND_NAME_MAP.get(CLASSKIND_VALUE, CLASSKIND_VALUE)
                    order_summary = f"{order_date_str} {classkind_name} {target_count}份，訂餐成功"

                    step_status["order_submit"] = f"成功：{order_summary}"
                    log_detailed("步驟3-4 訂餐送出", "OK", order_summary, log_lines=log_lines, page=page)

                # ---------- 本次嘗試總結 ----------
                log_detailed("=== 本次嘗試步驟總結 ===", "INFO", log_lines=log_lines, page=page)
                log_detailed("步驟1 登入", "OK" if step_status["login"] == "成功" else "FAIL", step_status["login"], log_lines=log_lines, page=page)
                log_detailed("步驟2 轉址訂餐系統", "OK" if step_status["redirect"] == "成功" else "FAIL", step_status["redirect"], log_lines=log_lines, page=page)
                log_detailed("步驟3 訂餐送出", "OK" if "成功" in step_status["order_submit"] else "WARN", step_status["order_submit"], log_lines=log_lines, page=page)
                _write_log(attempt, log_lines)

                if order_summary:
                    print(f"【高醫員工餐自動訂餐流程執行完成】{order_summary}")
                else:
                    print("【高醫員工餐自動訂餐流程執行完成】（本次未送出訂單，詳見上方 log）")

                # 把訂餐結果寫進 GITHUB_OUTPUT，方便 workflow 後續步驟（例如 LINE 通知）
                # 直接引用這個值，不用自己重新組字串
                github_output_path = os.environ.get("GITHUB_OUTPUT")
                if github_output_path:
                    try:
                        with open(github_output_path, "a", encoding="utf-8") as f:
                            f.write(f"order_summary={order_summary or '本次未送出訂單'}\n")
                    except Exception as e:
                        print(f"寫入 GITHUB_OUTPUT 失敗: {e}")

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
