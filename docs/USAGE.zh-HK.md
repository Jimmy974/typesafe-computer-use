# jev-computer-use 使用指南（瀏覽器模式）

呢份指南講點樣用 `clicker-bench` 喺瀏覽器自動做嘢：開網頁、撳掣、填表，同埋一次過行好多個 case。

## 點樣運作

每一步都係：

1. **讀網頁**：由 Chrome 攞出所有可以撳或者打字嘅元素，唔影相、唔用 OCR
2. **Jev（TypeSafe）決定**：做邊種動作（撳、打字、scroll……）、對住邊個元素
3. **執行**：用真正嘅滑鼠同鍵盤事件去做，然後等頁面變化
4. 返去第 1 步，直至完成或者停低

要打字嘅時候，內容有兩個來源：

- **資料檔**（建議）：你預先準備好嘅值，原封不動咁打
- **Writer**：本機嘅 oMLX（Ornith）按 goal 生成，只會喺冇資料檔嗰陣用

---

## 1. 準備（做一次就夠）

```bash
cd ~/source/jev-computer-use
uv sync
```

`.env`（唔會 commit）要有：

| 設定 | 用途 |
|---|---|
| `TYPESAFE_API_KEY` | Jev，每一步都要用（必需） |
| `CLICKER_WRITER_API=openai` 等 | Writer 用本機 oMLX |
| `CLICKER_WRITER_VISION=false` | Ornith 睇唔到圖，要保持 `false` |

用 writer 之前要開住 oMLX（`http://127.0.0.1:8000`）。只用資料檔嘅話，唔開都得。

每次佢都會開一個**臨時、全新**嘅 Chrome：冇你嘅登入、cookie 或者分頁，用完就刪；唔會郁你個滑鼠同鍵盤。

---

## 2. 行一個任務：`loop`

```bash
uv run clicker-bench loop \
  --url https://books.toscrape.com \
  --goal "Open the Travel category, then open the first book in it" \
  --runs runs
```

| 選項 | 作用 | 預設 |
|---|---|---|
| `--url` | 由邊一頁開始 | — |
| `--goal` | 要做乜，用一句英文講 | — |
| `--data 檔案.toml` | 表單資料（見第 4 節） | 冇 |
| `--headed` | 顯示 Chrome 視窗，畀你睇住佢做 | 唔顯示 |
| `--steps N` | 最多行幾多步 | 10 |
| `--runs runs` | 將每一步記錄喺 `runs/<時間>/` | **唔記錄**，所以記得加 |
| `--sites 資料夾` | 網站檔放喺邊 | `./sites` |

### Goal 點樣寫

- ✅ **講清楚完成係咩樣**：「open the issue about similarly named links」，唔好只寫「look at issues」
- ✅ **用英文寫**：Jev 同網頁都係英文，對得上會準啲
- ✅ **有表單資料就用資料檔**，唔好塞入 goal
- ❌ 唔好放密碼、驗證碼、卡號，佢一定唔會填

---

## 3. 睇結果

每一步印一行：

```
  #  action       detail                              conf      ...  total
  1  click        click [16] 'Issues 6' at (172,158)  conf=1.00 ...  1889ms
```

| 欄 | 意思 |
|---|---|
| `action` | 做咗乜：`click`、`type_text`、`press_enter`、`scroll_down`、`back`、`done`…… |
| `detail` | 撳咗邊個元素、打咗乜。用資料檔嘅話只會顯示 `<欄位名>`，唔會顯示內容 |
| `conf` | Jev 嘅信心，低過 0.4 就會停 |
| `chg` | 頁面有冇變 |

最尾嘅 `outcome`：

| outcome | 意思 |
|---|---|
| `done` | ✅ Jev 有信心話完成咗 |
| `low_confidence(0.xx)` | 唔肯定下一步點做，停低 |
| `stalled` | 同一個動作連續 3 次都冇改變頁面 |
| `stuck` | 連續失敗，例如搵唔到啱嘅資料去打 |
| `blocked` | Jev 判斷呢頁冇嘢可以做 |
| `max_steps` | 用晒步數 |

⚠️ `done` 只係 Jev 自己判斷。重要嘅任務要睇最後嘅網址或者頁面確認；`batch` 會用 `expect_url` 自動幫你檢查。

### Run folder（`runs/<時間>/`）

| 檔案 | 內容 |
|---|---|
| `run.json` | 成個 run 嘅總結、最後網址 |
| `step-NN-state.json` | 嗰一步 Jev 見到乜（元素清單） |
| `step-NN-answers.json` | Jev 揀咗乜、每個選項嘅機率 |
| `step-NN-elements.json` | 用嚟 replay 嘅頁面記錄 |

`runs/` 喺你本機，已經 gitignore。

---

## 4. 填表：資料檔（`--data`）

有現成資料嘅話，放入一個 TOML 檔：

```toml
# mydata/order.toml
[fields]    # 要打字嘅：Jev 只會見到欄位名，見唔到內容
customer_name = "Jimmy Wong"
telephone = "12345678"
email = "jimmy@example.com"

[choices]   # 要撳嘅 radio 或 checkbox：Jev 會見到，先揀得啱選項
size = "Medium"
topping = "Bacon"
```

```bash
uv run clicker-bench loop --url https://httpbin.org/forms/post \
  --data mydata/order.toml \
  --goal "Fill the pizza order form with the saved data, then submit the order." \
  --runs runs
```

**點樣運作：**

1. Jev 揀「喺邊一格打字」
2. 就嗰一格單獨再問 Jev：「應該打邊個欄位？」例如 Telephone 格 → `telephone`
3. 程式將值**原封不動**打入去，再喺本機檢查個格係咪真係有呢個值

**規則：**

- 欄位內容**永遠唔會送去 TypeSafe**，亦唔會出現喺記錄入面，只會出現欄位名
- 只會打入 Jev 指定嗰一格；指住嘅唔係輸入框就拒絕，唔會亂打
- 資料入面冇嘅欄位**會留空**，唔會叫 writer 作
- 欄位名似密碼（`password`、`pin`、`otp`、`cvv`……）一開始就會報錯

**效果**（pizza form，各行 3 次）：成功率同寫入 goal 一樣（3/3），但每格打字由約 1.5 秒快到約 0.24 秒，成個表單由 7.1 秒快到 3.2 秒。

---

## 5. 一次行好多個 case：`batch`

### A. 同一個流程，唔同資料 → CSV

可以用 Excel 維護，一行一個 case：

```csv
name,customer_name,telephone,choice:size,choice:topping
jimmy-medium,Jimmy Wong,12345678,Medium,Bacon
ken-small,Ken Lee,23456789,Small,Onion
```

| 欄 | 意思 |
|---|---|
| `name` | case 名（冇嘅話會叫 `row-1`、`row-2`……） |
| `goal`、`url`、`expect_url`、`steps` | 呢一行專用嘅設定，留空就用預設 |
| `choice:xxx` | 要撳嘅選項 |
| 其他欄 | 要打字嘅欄位 |

```bash
uv run clicker-bench batch mydata/orders.csv \
  --url https://httpbin.org/forms/post \
  --goal "Fill the pizza order form with the saved data, then submit the order." \
  --expect-url httpbin.org/post
```

⚠️ **Excel 會食咗前面嘅 0**，例如電話 `0123` 會變成 `123`。將嗰欄設定做「文字」先儲存。

### B. 個別 case 有自己嘅流程 → TOML（可以同 CSV 一齊用）

```toml
# mydata/orders.toml
url = "https://httpbin.org/forms/post"
goal = "Fill the pizza order form with the saved data, then submit the order."
expect_url = "httpbin.org/post"
rows = "orders.csv"                  # 讀埋 CSV 入面每一行

[defaults.fields]                    # 所有 case 共用；case 自己有嘅話會蓋過
email = "orders@example.com"

[[scenario]]                         # 特別 case：自己嘅 goal
name = "amy-no-toppings"
goal = "Fill the pizza order form with the saved data, leave every topping unticked, then submit the order."
fields = { customer_name = "Amy Chan", telephone = "87654321" }
choices = { size = "Large" }
```

```bash
uv run clicker-bench batch mydata/orders.toml
uv run clicker-bench batch mydata/orders.toml --only amy-no-toppings --headed
```

### Batch 嘅規則

- **行之前先檢查晒所有 case**：漏咗 url 或者 goal、名重複、key 串錯、似密碼嘅欄位名，一開始就報錯
- 每個 case 用一個全新嘅 Chrome，互不影響
- **成功嘅定義**：最後網址包含 `expect_url`；冇設定 `expect_url` 就睇係咪 `done`
- **一個失敗唔會影響其他**，會繼續行落去
- **唔會自動重試**：重試可能會提交兩次表單。失敗嘅 case 用 `--only` 自己重行
- 全部成功 exit code 係 0，有失敗就係 1
- 結果存喺 `runs/batch-*.json`，**只有名、結果同網址，唔會有你嘅資料**

`examples/pizza-orders.toml` 同 `examples/pizza-orders.csv` 係可以直接行嘅範例（假資料）。

### 真實資料放邊？

放喺 **`mydata/`**。呢個資料夾已經 gitignore，**唔會被 commit 或者 push**。

---

## 6. 網站檔（`sites/`）

某個網站成日卡住，就喺 `sites/<網域>.toml` 加提示。唔係必需，冇都照行。

```toml
# sites/example.com.toml
domain = "example.com"      # 包埋所有子網域
settle_ms = 1500            # 撳完之後最多等幾耐，等頁面定落嚟
notes = [
  "The Submit button is below the message box.",
  "After submitting, the page shows 'Thank you'.",
]
```

- **一個網站一個檔**，唔係一頁一個
- `notes` 只寫**事實**，唔好寫步驟，亦唔好寫個人資料（呢啲檔會 commit）
- **遇到問題先加**：先唔加照行，卡住再睇 run folder，加一樣會幫到佢嘅嘢

---

## 7. 卡住點算

1. 開 `runs/<時間>/`，睇 `step-NN-state.json`（佢見到乜）同 `step-NN-answers.json`（佢揀咗乜）
2. 對症下藥：

| 睇到 | 做法 |
|---|---|
| 撳完仲喺舊頁 | `sites/` 加 `settle_ms` |
| 兩個相似嘅掣揀錯 | `sites/` 加 note 講清楚邊個做乜 |
| 唔知幾時算完成 | 加 note 講完成頁會顯示乜；batch 就設 `expect_url` |
| 元素名唔清楚或者重複 | 讀取頁面嘅問題，要改 code |
| `stuck`，而 detail 寫 `no_saved_fit` | 資料檔冇啱嗰格嘅欄位，或者欄位名唔夠清楚，改個易明啲嘅名 |

3. 唔開瀏覽器，重新決定某一步：

```bash
uv run clicker-bench replay --run runs/<時間> --step 2
```

---

## 8. 測試系統本身：`compare`

`bench/tasks.toml` 係用嚟測試系統有冇改壞嘢嘅任務清單，唔係用嚟做真正嘅工作：

```bash
uv run clicker-bench compare --hands cdp --repeat 2
```

⚠️ 一定要加 **`--hands cdp`**。預設會 CDP 同 Playwright 兩樣都試，而 Playwright 係選擇性安裝嘅（`uv sync --extra playwright`）。

---

## 9. 做得到同做唔到

| ✅ 做得到 | ❌ 做唔到 |
|---|---|
| 撳 link、掣、tab | 打密碼、驗證碼、卡號（刻意設計） |
| 喺文字框打字 | 標準 `<select>` 下拉選單 |
| Radio、checkbox | 上載檔案 |
| 提交表單、Enter、Esc | iframe 入面嘅內容 |
| Scroll、返上一頁 | 要登入先用到嘅網站 |
| 一次行大量 case | canvas 或者圖片入面嘅字 |

已知問題：Wikipedia 大約 20 次有 3 次會喺開始時 crash（`JS error: Uncaught`），原因未搵到。

---

## 10. 私隱

| 資料 | 會唔會送去 TypeSafe |
|---|---|
| 頁面文字、元素清單 | 會，每一步都會 |
| Goal | 會 |
| 資料檔 `[fields]` 嘅內容 | **唔會**，只會送欄位名 |
| 資料檔 `[choices]` 嘅內容 | 會，Jev 要靠佢揀選項 |
| 輸入框入面嘅內容 | **唔會**，只會送「有冇填」 |
| Writer 生成嘅字 | 會，打完之後檢查時會送出 |

- Writer 喺你本機（oMLX），唔會送去其他雲端服務
- `runs/`、`mydata/`、`.env` 都已經 gitignore
