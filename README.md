# 扭蛋登記交換配對系統（LINE Bot + Python + PostgreSQL）

## 一、專案簡介

本專案是一個透過 **LINE Bot** 進行扭蛋登記時段「交換配對」的系統。

使用者在 LINE 中向 Bot 登記以下資訊（一次貼上 8 欄）：

1. 聯繫方式（需標明：Line ID:/脆:/IG:/FB:…）
2. 扭蛋訂單編號（9 碼，不可重複）
3. 原登記日期（僅單日，12 或 01 月）
4. 原登記時段（24 小時制，例：14:00~15:00）
5. 原登記地點（1.MAYDAY LAND 2.洲際棒球場；不可填 3）
6. 希望交換日期（可多日，12/xx 或 01/xx，逗號/頓號分隔）
7. 希望交換時段（可多段，與日期筆數一致，24 小時制）
8. 希望交換地點（1.MAYDAY LAND 2.洲際棒球場 3.皆可）

系統會將資料儲存於後端資料庫中，並自動執行配對：

- 當 A 的「原登記日期 / 時段 / 地點」= B 的「希望交換日期 / 時段 / 地點」  
- 且 B 的「原登記日期 / 時段」= A 的「希望交換日期 / 時段 / 地點」  
- 以上皆需符合

即判定為 **成功配對**。  
配對成功後，系統會主動推播通知給雙方，內容包含：

- 對方聯繫方式   
- 對方可提供交換的原登記日期 / 時段 / 地點 
- 對方的 6 位數驗證碼（供雙方核對身份）

---

## 二、系統架構與技術棧

### 2.1 架構概觀

整體流程如下：

1. 使用者透過 LINE 傳訊息給官方帳號（Bot）  
2. LINE 平台透過 **Webhook** 將事件以 HTTPS POST 送到後端伺服器  
3. 後端使用 Python + Flask + line-bot-sdk 驗證簽名、解析事件  
4. 根據使用者指令（登記 / 查詢 / 取消）：
   - 收集欄位資料（一次貼 8 欄）
   - 寫入 PostgreSQL 資料庫（`created_at` 作為配對優先順序）
   - 執行配對邏輯（同 LINE ID 不互配）
   - 透過 Reply 或 Push 發送訊息給使用者  

### 2.2 開發技術與套件

- **LINE Messaging API + LINE Bot SDK for Python**  
  - 負責處理 webhook 事件、回覆訊息（Reply）與主動推播（Push）

- **後端框架：Flask（Python）**  
  - 接收來自 LINE 的 HTTP POST 請求，作為 Webhook 入口  
  - 提供 `/callback` 路由，用於處理訊息事件

- **資料庫：PostgreSQL（Render Internal DB）**  
  - 使用 `psycopg2-binary` 連線 `DATABASE_URL`  
  - `created_at` 排序配對優先，`phone`/`email` 已移除  

- **設定與機密管理：python-dotenv / Render 環境變數**  
  - 開發環境使用 `.env` 儲存 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`  
  - 雲端環境使用平台提供的環境變數機制

- **部署平台：Render（Web Service）**  
  - 透過 GitHub 連動，自動 Build & Deploy  
  - Build：`pip install -r requirements.txt`  
  - Start：`gunicorn app:app` 或 `python app.py`

---

## 三、功能說明

### 3.1 使用者指令

1. `登記`  
   - 開始登記，直接貼上 8 欄：聯繫方式、訂單編號(9碼)、原登記日期/時段/地點、希望交換日期/時段/地點。  
   - 希望交換日期/時段可多筆，筆數需一致；原登記地點僅可填 1 或 2；同 LINE ID 不互配。  
   - 送出後產生 6 位驗證碼，寫入資料庫（`created_at` 用於配對優先）。若有符合條件，立即推播配對成功通知。

2. `查詢 訂單編號 驗證碼`  
   - 回傳該筆登記的資料與配對狀態，若已配對會附上對方資訊。

3. `取消 訂單編號 驗證碼`  
   - 可取消待配對資料；已配對請改用「解除 訂單編號 你的驗證碼 對方驗證碼」恢復為待配對。

4. 其他文字  
   - 若不在流程中，顯示說明與指令提示。

---

### 3.2 登記流程與狀態管理

- 系統以 `user_states`（記憶體中的字典）管理每位使用者目前進度：  
  - `step`：目前問到第幾個欄位（0–9）  
  - `data`：已經填寫的欄位資料  

- 使用者輸入 `登記`：
  - 開始「扭蛋交換登記流程」，一次輸出全部問題，並需依照問題格式進行回覆
  - 系統收到登記內容，並與資料庫之內容比對(每一欄位階需相同)，若相同則會要求先「取消」再登記新的  

- 每回覆一次，Bot 會：
  - 將使用者回覆內容寫入 `data[欄位名]`  
  - 若尚未完成 10 題，繼續問下一題  
  - 若 10 題全部完成：
    - 將 `user_states[user_id]` 移除  
    - 呼叫 DB 函式寫入 `exchange_requests`  
    - 產生 6 位數驗證碼並一併儲存  
    - 回覆「登記完成」訊息，並重複傳送登記資料以供使用者核對

---

### 3.3 資料庫設計（交換登記）

資料表：`exchange_requests`（示意）

- `id`：整數主鍵，自動遞增  
- `line_user_id`：LINE 使用者 ID  
- `contact`：聯繫方式  
- `order_no`：扭蛋訂單編號  
- `orig_date`：原登記日期（字串型別yyyy-mm-dd）  
- `orig_slot`：原登記時段（字串型別hh:mm-hh:mm） 
- `orig_place`：原登記地點（字串型別）
- `desired_date`：希望交換日期  
- `desired_slot`：希望交換時段  
- `desired_place`：希望登記地點（字串型別）
- `verif_code`：系統產生的 6 位數驗證碼  
- `status`：狀態（`pending` / `matched` / `cancelled` 等）  
- `match_id`：配對群組 ID（配對成功的兩筆資料會擁有相同的 match_id）
- `created_at`：登記時間戳（用於配對優先順序）

---

### 3.4 自動配對機制

當有新的登記資料寫入時，系統會：

1. 讀出新資料 `me`  
2. 在資料庫搜尋另一筆狀態為 `pending` 的資料 `other`，需符合：

   - `other.orig_date  = me.desired_date`  
   - `other.orig_slot  = me.desired_slot`  
   - `other.desired_date = me.orig_date`  
   - `other.desired_slot = me.orig_slot`  
   - `other.orig_place  = me.desired_place`  
   

3. 若找到 `other`：  
   - 將兩筆資料的 `status` 更新為 `matched`  
   - 設定相同 `match_id`（可使用兩者 `id` 中較小者）  
   - 使用 LINE Push API：  
     - 向 `me.line_user_id` 推送一則配對成功訊息（內容帶入 `other` 的資料）  
     - 向 `other.line_user_id` 推送一則配對成功訊息（內容帶入 `me` 的資料）

4. 若沒有符合條件者：  
   - 保持新資料為 `pending`，等待之後有相符條件的使用者登記  

---

## 四、專案目標與優點

- 透過 LINE Bot 作為介面，降低使用者操作門檻  
- 不需即時配對，可接受延遲，適合以 SQLite / 單一 Web Service 進行實作  
- 支援：
  - 多次登記（每次僅允許一筆 pending，需先取消再重新登記）  
  - 使用者主動取消待配對資料  
  - 雙向配對成功後立即互相推播聯繫資訊與驗證碼  

未來可進一步擴充：

- 改用雲端資料庫（如 PostgreSQL）  
- 增加管理後台、查詢歷史紀錄  
- 加入更多驗證與錯誤處理，提升資料品質與系統穩定性
