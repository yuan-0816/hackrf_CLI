# GPS Spoofing Tools

GPS Spoofing Tools 是以 FastAPI、原生 HTML/CSS/JavaScript、gps-sdr-sim 與 HackRF Tools 組成的 GPS 信號測試工具。目前安裝說明以 Ubuntu 為主。

> [!WARNING]
> 本專案只能用於合法、經授權且與外界隔離的測試環境。GPS L1 發射或屏蔽信號可能干擾導航、授時及其他無線電服務。請勿接上天線向外輻射；建議使用屏蔽箱，或採用有線測試並串接 DC Block 與 50–60 dB 固定衰減器。

## 功能

- 顯示 HackRF 連線、韌體與執行狀態
- 更新並管理 GPS 廣播星曆
- 以地圖或座標建立固定點位 GPS 信號
- 以起點、方向、速度及時長建立牽引式 GPS 信號
- 顯示信號生成及單次發射進度
- 管理預儲存點位與已生成的 BIN 檔案
- 在授權的隔離環境進行 GPS L1 屏蔽測試

## 系統需求

- Ubuntu 22.04 或更新版本
- x86-64 或 ARM64 主機
- 可用的網際網路連線
- HackRF One 與支援資料傳輸的 USB 線
- 足夠的磁碟空間

預設採樣率為 2.6 MHz，8-bit I/Q 每秒約產生 5.2 MB 資料，約為：

- 1 分鐘：312 MB
- 5 分鐘：1.56 GB
- 1 小時：18.72 GB

實際檔案大小可能因生成器輸出略有差異。

## 1. 安裝 Ubuntu 系統套件

更新套件索引並安裝編譯工具、HackRF Tools 與 USB 檢查工具：

```bash
sudo apt update
sudo apt install -y \
  build-essential \
  ca-certificates \
  curl \
  git \
  hackrf \
  libhackrf-dev \
  usbutils
```

`hackrf` 套件會提供本專案需要的 `hackrf_info`、`hackrf_transfer` 與 `libhackrf`。一般使用不需要自行編譯 HackRF Host；`libhackrf-dev` 是保留給除錯或編譯其他 SDR 軟體使用。

安裝後確認工具存在：

```bash
command -v hackrf_info
command -v hackrf_transfer
```

官方安裝說明：[Installing HackRF Software](https://hackrf.readthedocs.io/en/latest/installing_hackrf_software.html)

## 2. 取得專案

使用 HTTPS：

```bash
git clone https://github.com/yuan-0816/hackrf_CLI.git
cd hackrf_CLI
```

若已經下載專案，直接進入專案根目錄即可。

## 3. 安裝並編譯 gps-sdr-sim

本專案固定從以下位置尋找執行檔：

```text
third_party/gps-sdr-sim/gps-sdr-sim
```

在專案根目錄執行：

```bash
mkdir -p third_party
git clone https://github.com/osqzss/gps-sdr-sim.git \
  third_party/gps-sdr-sim

make -C third_party/gps-sdr-sim clean
make -C third_party/gps-sdr-sim USER_MOTION_SIZE=864000
```

`864000` 代表 10 Hz 軌跡最多 864,000 筆資料，即 24 小時。實際可生成時長仍會受到磁碟空間限制。

確認編譯結果：

```bash
test -x third_party/gps-sdr-sim/gps-sdr-sim \
  && echo "gps-sdr-sim 已就緒"
```

gps-sdr-sim 官方說明：[osqzss/gps-sdr-sim](https://github.com/osqzss/gps-sdr-sim/blob/master/README.md)

若該資料夾已經存在，不要再次執行 `git clone`，直接執行兩個 `make` 指令即可。

## 4. 安裝 Python 與專案套件

專案使用 `uv` 管理 Python 和相依套件，並由 `.python-version` 指定 Python 3.14.2。

安裝 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
```

若目前終端仍找不到 `uv`，關閉終端後重新開啟，再確認：

```bash
uv --version
```

安裝指定的 Python 與鎖定套件：

```bash
uv python install 3.14.2
uv sync --frozen
```

確認環境：

```bash
uv run python --version
```

官方說明：[Installing uv](https://docs.astral.sh/uv/getting-started/installation/)

## 5. 設定 NASA Earthdata 帳號

專案會從 NASA CDDIS 下載每日 GPS 廣播星曆。請先申請免費的 [NASA Earthdata Login](https://urs.earthdata.nasa.gov/documentation/for_users/how_to_register)。CDDIS 的每日 GNSS 資料說明可參考 [Daily GNSS Data](https://cddis.nasa.gov/Data_and_Derived_Products/GNSS/daily_gnss_x.html)。

在專案根目錄建立 `.env`：

```dotenv
NASA_USER="你的 Earthdata 帳號"
NASA_PASS="你的 Earthdata 密碼"
```

限制檔案權限：

```bash
chmod 600 .env
```

不要把真實帳號、密碼或 `.env` 提交到 Git。

## 6. 連接並檢查 HackRF

接上 HackRF 後執行：

```bash
lsusb | grep -i hackrf
hackrf_info
```

正常情況下，`hackrf_info` 會顯示 `Found HackRF`、Board ID、Firmware Version 與 Hardware Revision。

若剛安裝套件後仍出現權限或找不到裝置，可以重新載入 udev 規則並重新插拔 HackRF：

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

若 Ubuntu 執行於 VMware，還必須在 VMware 的 USB 裝置選單中把 HackRF 連接至 Ubuntu 虛擬機，而不是留在 Windows 主機。先用 `lsusb` 確認虛擬機已取得裝置，再執行 `hackrf_info`。

## 7. 啟動網頁介面

### 僅在 Ubuntu 本機操作

```bash
uv run uvicorn app.backend.app:app \
  --host 127.0.0.1 \
  --port 8000
```

瀏覽器開啟：

```text
http://127.0.0.1:8000
```

### VMware NAT 模式，由 Windows 主機操作

在 Ubuntu 虛擬機內啟動：

```bash
uv run uvicorn app.backend.app:app \
  --host 0.0.0.0 \
  --port 8000
```

查詢 Ubuntu 虛擬機 IP：

```bash
hostname -I
```

假設 IP 是 `192.168.176.128`，在 Windows 瀏覽器開啟：

```text
http://192.168.176.128:8000
```

如果 Ubuntu 啟用了 UFW，可開放此連接埠：

```bash
sudo ufw allow 8000/tcp
```

目前介面沒有登入驗證。`--host 0.0.0.0` 只應用於可信任的 VMware NAT 或封閉測試網路，不要將 8000 埠轉發到網際網路。

## 8. 初次使用檢查

1. 開啟「系統總覽」。
2. 按「重新偵測」，確認顯示 HackRF 已連接。
3. 開啟「星曆更新」，確認能下載星曆並顯示涵蓋時間。
4. 先生成短時間的固定點位信號。
5. 在屏蔽箱或有線衰減環境確認發射與停止功能。

生成固定點位或牽引式信號前，系統會自動檢查 UTC 當日星曆；當日檔案已存在時會跳過下載。

## 測試

執行完整測試：

```bash
uv run python -m unittest discover -s tests -v
```

## 常見問題

### `hackrf_info: command not found`

確認已安裝 HackRF Tools：

```bash
sudo apt install -y hackrf
```

### `hackrf_info` 找不到裝置

- 更換支援資料傳輸的 USB 線或 USB 埠。
- 重新插拔 HackRF。
- 確認 `lsusb` 能看到裝置。
- VMware 使用者應確認 USB 裝置已交給 Ubuntu 虛擬機。
- 重新載入 udev 規則後再次測試。

### 找不到 gps-sdr-sim

確認執行檔位於正確位置：

```bash
ls -l third_party/gps-sdr-sim/gps-sdr-sim
```

若不存在，重新執行：

```bash
make -C third_party/gps-sdr-sim USER_MOTION_SIZE=864000
```

### 星曆更新失敗

- 確認 `.env` 中的 `NASA_USER`、`NASA_PASS` 正確。
- 確認 Earthdata 帳號已啟用。
- 確認虛擬機能連線至 `cddis.nasa.gov`。
- 檢查系統時間與 UTC 日期是否正確。

### Windows 無法開啟虛擬機網頁

確認後端監聽所有介面：

```bash
ss -ltnp | grep 8000
```

正常應看到 `0.0.0.0:8000`。接著在 Windows PowerShell 測試：

```powershell
Test-NetConnection 192.168.176.128 -Port 8000
```

請將範例 IP 換成 Ubuntu 虛擬機的實際 IP。

### 生成檔案過大

縮短固定點位或牽引式模擬的總時長，或從「檔案管理」永久刪除不再使用的 BIN 檔案。系統會在生成前依採樣率檢查可用磁碟空間。
