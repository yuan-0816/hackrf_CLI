# GPS Spoofing Tools

GPS Spoofing Tools 是以 FastAPI、原生 HTML/CSS/JavaScript、gps-sdr-sim 與 HackRF Tools 組成的 GPS 信號測試工具，支援 Windows 10/11 x64 與 Ubuntu 22.04。

本工具會產生並發射射頻信號。只能在已獲授權的屏蔽箱、有線衰減環境或其他合法隔離環境使用，避免干擾真實 GNSS 接收器及公共無線電服務。

預設採樣率為 2.6 MHz，8-bit I/Q 每秒約產生 5.2 MB：

- 1 分鐘：約 312 MB
- 5 分鐘：約 1.56 GB
- 1 小時：約 18.72 GB

## Windows 安裝與執行

### 1. 系統需求

- Windows 10 或 Windows 11 x64
- PowerShell 5.1 以上
- 可用的網際網路連線
- HackRF One 與支援資料傳輸的 USB 線
- 足夠的磁碟空間

### 2. 取得專案

在 PowerShell 執行：

```powershell
git clone https://github.com/yuan-0816/GPS-Spoofing-Tools.git
Set-Location GPS-Spoofing-Tools
```

專案已包含 `third_party\gps-sdr-sim` 原始碼及 Windows HackRF Tools，不需要另外下載。HackRF Tools 的所有 DLL 必須留在 EXE 的同一資料夾：

```text
third_party\hackrf-tools-windows\hackrf_info.exe
third_party\hackrf-tools-windows\hackrf_transfer.exe
third_party\hackrf-tools-windows\hackrf_sweep.exe
third_party\hackrf-tools-windows\hackrf.dll
third_party\hackrf-tools-windows\libusb-1.0.dll
```

程式在 Windows 會優先使用此資料夾，不需要修改系統 `PATH`。

### 3. 安裝 uv 與 Python 相依套件

使用 WinGet 安裝 uv：

```powershell
winget install --id=astral-sh.uv -e
```

也可以使用 uv 官方 PowerShell 安裝程式：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

重新開啟 PowerShell，然後在專案根目錄執行：

```powershell
uv python install 3.14.2
uv sync --frozen
uv run python --version
```

### 4. 安裝 HackRF USB 驅動並檢查裝置

先接上 HackRF，直接測試專案內工具：

```powershell
.\third_party\hackrf-tools-windows\hackrf_info.exe
```

正常時會顯示 `Found HackRF`、Board ID、Firmware Version 與 Hardware Revision。

如果顯示 `No HackRF boards found`，使用 [Zadig](https://zadig.akeo.ie/)：

1. 執行 Zadig，從 `Options` 啟用 `List All Devices`。
2. 選擇 `HackRF One`，務必確認沒有選到鍵盤、滑鼠或其他 USB 裝置。
3. 選擇 `WinUSB`，按下安裝或取代驅動。
4. 重新插拔 HackRF，再執行 `hackrf_info.exe`。

HackRF 官方的 Windows 主機端說明也指定可透過 Zadig 為 HackRF 安裝 WinUSB：[HackRF host README](https://github.com/greatscottgadgets/hackrf/blob/main/host/README.md)。

### 5. 建置 gps-sdr-sim.exe

執行：

```powershell
.\scripts\build_gps_sdr_sim_windows.ps1
```

腳本會優先使用已安裝的 Zig、GCC 或 Visual Studio C++ 編譯器；若都不存在，會透過 `uv` 暫時取得 Zig。第一次執行需要下載約 100 MB 的編譯工具，之後會使用快取。

成功後會建立：

```text
third_party\gps-sdr-sim\gps-sdr-sim.exe
```

預設動態軌跡容量為 864,000 筆（10 Hz 共 24 小時）。如需重建，可指定容量：

```powershell
.\scripts\build_gps_sdr_sim_windows.ps1 -UserMotionSize 864000
```

### 6. 設定 NASA Earthdata 帳號

專案會從 NASA CDDIS 下載 GPS 廣播星曆。先申請 [NASA Earthdata Login](https://urs.earthdata.nasa.gov/documentation/for_users/how_to_register)，再於專案根目錄建立 `.env`：

```dotenv
NASA_USER="你的 Earthdata 帳號"
NASA_PASS="你的 Earthdata 密碼"
```

不要把 `.env` 或真實帳號密碼提交到 Git。

### 7. 啟動網頁介面

最簡單的方式：

```powershell
.\start_windows.ps1
```

腳本會安裝鎖定的 Python 套件、在缺少時自動建置 `gps-sdr-sim.exe`、執行 HackRF 偵測，最後啟動網頁伺服器。它會先檢查 `127.0.0.1:8000` 是否可綁定；若該埠已被占用或被 Windows 保留，會自動依序尋找 8001 至 8100，並在 PowerShell 顯示實際網址。

未發生埠衝突時，瀏覽器開啟：

```text
http://127.0.0.1:8000
```

若 PowerShell 阻擋本機腳本，可只對這次執行放寬：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
```

也可手動啟動：

```powershell
uv run uvicorn app.backend.app:app --host 127.0.0.1 --port 8000
```

停止伺服器請在 PowerShell 按 `Ctrl+C`。

也可以指定搜尋起始埠：

```powershell
.\start_windows.ps1 -Port 8080
```

腳本會從 8080 開始尋找可用埠。若使用 `-ExecutionPolicy Bypass`，參數放在腳本路徑後面：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1 -Port 8080
```

### 8. 使用 CLI

查看 HackRF：

```powershell
uv run python hackrf.py info
```

進入互動選單：

```powershell
uv run python hackrf.py
```

例如生成 60 秒固定點位信號：

```powershell
uv run python hackrf.py gps static --lat 25.03 --lon 121.56 --duration 60
```

## Ubuntu 安裝與執行

### 1. 系統套件

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

### 2. 取得專案並編譯 gps-sdr-sim

```bash
git clone https://github.com/yuan-0816/GPS-Spoofing-Tools.git
cd GPS-Spoofing-Tools
```

專案已包含 `third_party/gps-sdr-sim` 原始碼。直接編譯模擬器：

```bash
make -C third_party/gps-sdr-sim clean
make -C third_party/gps-sdr-sim USER_MOTION_SIZE=864000
test -x third_party/gps-sdr-sim/gps-sdr-sim
```

### 3. 安裝 uv 與 Python 相依套件

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv python install 3.14.2
uv sync --frozen
uv run python --version
```

### 4. 設定帳號、檢查裝置並啟動

依照 Windows 第 6 節建立 `.env`，然後執行：

```bash
hackrf_info
uv run uvicorn app.backend.app:app --host 127.0.0.1 --port 8000
```

瀏覽器開啟 `http://127.0.0.1:8000`。若有 USB 權限問題，可重新載入 udev 規則並重新插拔 HackRF：

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

HackRF 官方安裝說明：[Installing HackRF Software](https://hackrf.readthedocs.io/en/latest/installing_hackrf_software.html)。

Ubuntu CLI 指令與 Windows 相同，只需將 PowerShell 路徑語法換成 Linux 路徑。

## 初次使用檢查

1. 開啟「系統總覽」，按「重新偵測」，確認 HackRF 已連接。
2. 開啟「星曆更新」，確認能下載星曆並顯示涵蓋時間。
3. 先生成短時間的固定點位信號。
4. 僅在屏蔽箱或有線衰減環境確認發射與停止功能。

生成固定點位或牽引式信號前，系統會自動檢查 UTC 當日星曆；當日檔案已存在時會跳過下載。

## 測試

Windows 與 Ubuntu 都在專案根目錄執行：

```text
uv run python -m unittest discover -s tests -v
```

## 常見問題

### Windows 找不到 HackRF Tools

確認 `third_party\hackrf-tools-windows` 中的 EXE 與 DLL 都存在。不要只把 EXE 複製到其他資料夾，否則 Windows 可能找不到 `hackrf.dll`、`libusb-1.0.dll` 或其他相依 DLL。

### Windows 顯示 `No HackRF boards found`

- 更換支援資料傳輸的 USB 線或 USB 埠。
- 關閉可能正在占用 HackRF 的 SDR 軟體。
- 依 Windows 安裝第 4 節確認 Zadig 的 HackRF 驅動是 WinUSB。
- 重新插拔裝置後再執行 `hackrf_info.exe`。

### 找不到 gps-sdr-sim

Windows：

```powershell
.\scripts\build_gps_sdr_sim_windows.ps1
```

Ubuntu：

```bash
make -C third_party/gps-sdr-sim USER_MOTION_SIZE=864000
```

### Windows 無法執行 PowerShell 腳本

```powershell
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
```

此指令只調整該次 PowerShell 程序，不會永久修改全機執行原則。

### Windows 顯示 WinError 10013 或埠已被占用

使用 `start_windows.ps1` 時，腳本會自動跳過被占用或被 Windows 保留的埠，並顯示實際網址。也可以指定另一個搜尋起始埠：

```powershell
.\start_windows.ps1 -Port 8080
```

若要手動使用 uv 啟動，請自行指定可用埠：

```powershell
uv run uvicorn app.backend.app:app --host 127.0.0.1 --port 8080
```

### 星曆更新失敗

- 確認 `.env` 中的 `NASA_USER`、`NASA_PASS` 正確。
- 確認 Earthdata 帳號已啟用。
- 確認能連線至 `cddis.nasa.gov`。
- 檢查系統時間、時區與 UTC 日期。

### 生成檔案過大

縮短固定點位或牽引式模擬時間，或從「檔案管理」永久刪除不再使用的 BIN 檔案。系統會在生成前依採樣率檢查可用磁碟空間。
