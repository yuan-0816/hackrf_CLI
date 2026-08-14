# GPS Spoofing Tools

此介面由 FastAPI 提供後端，前端使用原生 HTML、CSS、JavaScript 與 Leaflet。伺服器僅監聽 `127.0.0.1`，供 HackRF 主機本機操作。

## 啟動

在專案根目錄執行：

```bash
uv run uvicorn app.backend.app:app --host 127.0.0.1 --port 8000
```

瀏覽器開啟 `http://127.0.0.1:8000`。

## 功能

- 查看 HackRF、背景任務、射頻輸出與目前信號檔案狀態
- 依實際輸出檔案大小顯示信號生成進度
- 強制更新星曆並顯示實際涵蓋的 UTC 時間
- 地圖選擇固定點位，設定高度、信號時長及時間模式
- 長時間信號會在生成前依採樣率檢查磁碟可用空間
- 地圖選擇牽引起點與方向，可分別重選方向或起點，並依速度及時間預估實際終點
- 生成後以單次播放方式發射，並可隨時停止
- 啟動及停止 GPS L1 信號屏蔽
- 透過地圖新增、直接編輯及刪除預儲存點位
- 編輯完整的 HackRF、GPS 模擬與星曆設定

所有前端顯示文字集中在 `frontend/locales/zh-TW.json`，HTML 與 JavaScript 不直接寫入 UI 文案。

## 測試

```bash
uv run python -m unittest discover -s tests -v
```

射頻功能只應在隔離、合法且已獲授權的測試環境中使用。
