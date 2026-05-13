"""
HackRF CLI
支援互動式選單 & 指令列參數雙模式

使用方式:
  python hackrf.py                              # 進入互動選單
  python hackrf.py info                         # 直接執行指令
  python hackrf.py gps static --lat 25.03 --lon 121.56 --repeat 60
"""

import argparse
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config_manager import ConfigManager
from utils.tool import get_project_root
from utils.get_latest_brdc import check_newst_brdc, fetch_latest_ephemeris
from hackrf_wrapper import HackRFCLI
from fake_gps import FakeGPS

# ── 路徑設定 ─────────────────────────────────────────────────────────────────

PROJECT_ROOT = get_project_root()
BIN_DIR      = os.path.join(PROJECT_ROOT, "data", "fake_signal", "gps")
RECORD_DIR   = os.path.join(PROJECT_ROOT, "data", "recorded")
KML_DIR      = os.path.join(PROJECT_ROOT, "data", "fake_path")
CSV_DIR      = os.path.join(PROJECT_ROOT, "data", "fake_path")

DEFAULT_DRIFT_RATE_MPS = 0.05
DEFAULT_DRIFT_DURATION_S = 300

# ── 全域實例 ──────────────────────────────────────────────────────────────────

cfg    = ConfigManager()
hackrf = HackRFCLI()

# ── UI 工具函數 ────────────────────────────────────────────────────────────────

def header(title: str):
    print(f"\n{'─' * 44}")
    print(f"  {title}")
    print(f"{'─' * 44}")

def prompt(text: str, default=None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{text}{suffix}: ").strip()
    return val if val else (str(default) if default is not None else "")

def prompt_float(text: str, default=None) -> float:
    while True:
        try:
            return float(prompt(text, default))
        except ValueError:
            print("  [!] 請輸入有效的數字")

def _parse_coords_line(line: str, default_alt: float) -> tuple[float, float, float] | None:
    parts = [p for p in re.split(r"[,\s]+", line.strip()) if p]
    if len(parts) not in (2, 3):
        return None
    try:
        lon = float(parts[0])
        lat = float(parts[1])
        alt = float(parts[2]) if len(parts) == 3 else float(default_alt)
    except ValueError:
        return None
    return lat, lon, alt

def prompt_coords(default_alt: float) -> tuple[float, float, float] | None:
    while True:
        line = prompt("  一次輸入 經度 緯度 高度 (例如: 55 33 100, 0=返回)", "")
        if not line:
            lat = prompt_float("  緯度 (lat)")
            lon = prompt_float("  經度 (lon)")
            alt = prompt_float("  高度 (m)", default_alt)
            return lat, lon, alt

        if line == "0":
            return None

        parsed = _parse_coords_line(line, default_alt)
        if parsed:
            return parsed
        print("  [!] 請輸入 2~3 個數字，例如: 55 33 100")

def prompt_int(text: str, default=None) -> int:
    while True:
        try:
            return int(prompt(text, default))
        except ValueError:
            print("  [!] 請輸入有效的整數")


def _sorted_presets(presets: dict) -> list[tuple[str, dict]]:
    return sorted(presets.items(), key=lambda item: item[0])


def _select_preset(presets: dict, label: str = "Preset") -> tuple[str, dict] | None:
    if not presets:
        print("  (尚無 Preset)")
        return None

    items = _sorted_presets(presets)
    _show_preset_table(presets)

    raw = prompt(f"  選擇 {label} 編號 (1-{len(items)}, 0=返回)", "0")
    if raw == "0":
        return None

    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(items):
            return items[idx - 1]

    preset = presets.get(raw)
    if preset:
        return raw, preset

    print("  [!] 無效的選擇")
    return None

def confirm(text: str) -> bool:
    return prompt(f"{text} (y/n)", "n").lower() == "y"

def pick_file(directory: str, extension: str, label: str) -> str | None:
    """列出目錄中的檔案，讓使用者選擇，回傳路徑"""
    files = _list_files(directory, extension)
    if not files:
        print(f"  [!] 在 {directory} 找不到 {extension} 檔案")
        return None

    print(f"\n  可用的{label}:")
    for i, f in enumerate(files, 1):
        size_mb = os.path.getsize(f) / (1024 * 1024)
        print(f"    {i}. {os.path.basename(f)}  ({size_mb:.1f} MB)")

    idx = prompt_int(f"  選擇編號 (1-{len(files)}, 0=返回)", 1)
    if idx == 0:
        return None
    if 1 <= idx <= len(files):
        return files[idx - 1]
    print("  [!] 無效的選擇")
    return None

def _list_files(directory: str, extension: str) -> list[str]:
    if not os.path.exists(directory):
        return []
    files = [
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.endswith(extension)
    ]
    return sorted(files, key=os.path.getmtime, reverse=True)

# ── 核心功能 ──────────────────────────────────────────────────────────────────

def cmd_info(_args=None):
    """查看硬體資訊"""
    header("硬體資訊")
    try:
        result = subprocess.run(["hackrf_info"], capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        print(output if output else "  [!] 未偵測到 HackRF 設備")
    except FileNotFoundError:
        print("  [Error] 找不到 hackrf_info，請確認已安裝 hackrf 套件")


def cmd_ephemeris(_args=None):
    """更新星歷檔案"""
    header("更新星歷檔案")
    if check_newst_brdc():
        print("  [V] 已是最新星歷，無需更新")
        return
    print("  [*] 正在下載最新星歷...")
    path = fetch_latest_ephemeris()
    if path:
        print(f"  [V] 星歷更新成功: {path}")
    else:
        print("  [X] 星歷更新失敗")


def cmd_gps_static(args=None):
    """靜態 GPS 點位模擬"""
    header("GPS 靜態點位模擬")

    # ── 取得座標 ──
    lat = lon = alt = None
    repeat = 0  # 0 = 無限

    drift_enabled = False
    drift_rate_mps = DEFAULT_DRIFT_RATE_MPS
    drift_seed = None

    if args and (args.lat or args.preset):
        # CLI 模式
        if args.preset:
            p = cfg.preset_get(args.preset)
            if not p:
                print(f"  [Error] 找不到 Preset: {args.preset}")
                return
            lat, lon, alt = p["lat"], p["lon"], p["alt"]
        else:
            if args.lat is None or args.lon is None:
                print("  [Error] 請提供 --lat 和 --lon，或使用 --preset")
                return
            lat, lon, alt = args.lat, args.lon, args.alt
        repeat = args.repeat
        if args.drift or args.drift_rate is not None or args.drift_seed is not None:
            drift_enabled = True
            drift_rate_mps = args.drift_rate if args.drift_rate is not None else DEFAULT_DRIFT_RATE_MPS
            drift_seed = args.drift_seed
    else:
        # 互動模式
        presets = cfg.preset_list()
        if presets and confirm("  使用已儲存的 Preset?"):
            selected = _select_preset(presets)
            if not selected:
                return
            _, p = selected
            lat, lon, alt = p["lat"], p["lon"], p["alt"]
        else:
            coords = prompt_coords(cfg.get("gps_sim.default_height", 100.0))
            if not coords:
                return
            lat, lon, alt = coords

        repeat = prompt_int("  重複播放時間 (秒, 0=無限)", 0)
        if confirm("  啟用緩慢漂移?"):
            drift_enabled = True
            drift_rate_mps = prompt_float("  漂移速度 (m/s)", DEFAULT_DRIFT_RATE_MPS)

    print(f"\n  座標: {lat}, {lon}, alt={alt}m")

    # ── 生成 bin ──
    output_bin = os.path.join(BIN_DIR, f"static_{lat:.5f}_{lon:.5f}.bin")
    sim = _make_simulator()
    ok = sim.generate_bin(
        output_bin=output_bin,
        static_mode=True,
        manual_coords=(lat, lon, alt),
        drift_enabled=drift_enabled,
        drift_rate_mps=drift_rate_mps,
        drift_seed=drift_seed
    )
    if not ok:
        print("  [X] Bin 檔案生成失敗")
        return

    # 互動模式才問要不要發射
    if args is None and not confirm("\n  是否立即發射?"):
        print(f"  [V] Bin 已儲存: {output_bin}")
        return

    _transmit(output_bin, repeat=repeat)


def cmd_gps_dynamic(args=None):
    """動態 GPS 軌跡模擬 (KML)"""
    header("GPS 動態軌跡模擬")

    # ── 取得 KML ──
    if args and args.kml:
        kml_file = args.kml
    else:
        kml_file = pick_file(KML_DIR, ".kml", "KML 檔案")
        if not kml_file:
            kml_file = prompt("  請輸入 KML 完整路徑 (0=返回)")
            if kml_file == "0":
                return

    if not os.path.exists(kml_file):
        print(f"  [Error] 找不到 KML 檔案: {kml_file}")
        return

    base_name = os.path.splitext(os.path.basename(kml_file))[0]
    csv_file   = os.path.join(CSV_DIR, f"{base_name}_motion.csv")
    output_bin = os.path.join(BIN_DIR, f"dynamic_{base_name}.bin")

    sim = _make_simulator()

    if not sim.kml_to_csv(kml_file, csv_file):
        return

    ok = sim.generate_bin(
        output_bin=output_bin,
        static_mode=False,
        csv_file=csv_file
    )
    if not ok:
        print("  [X] Bin 檔案生成失敗")
        return

    if args is None and not confirm("\n  是否立即發射?"):
        print(f"  [V] Bin 已儲存: {output_bin}")
        return

    _transmit(output_bin, repeat=0)


def cmd_gps_drift():
    """緩慢飄移誘騙 (靜態點位 + 漂移)"""
    header("GPS 緩慢飄移誘騙")

    coords = prompt_coords(cfg.get("gps_sim.default_height", 100.0))
    if not coords:
        return

    lat, lon, alt = coords
    drift_rate_mps = prompt_float("  漂移速度 (m/s)", DEFAULT_DRIFT_RATE_MPS)
    drift_duration_s = prompt_int("  飄移時間/播放秒數 (秒)", DEFAULT_DRIFT_DURATION_S)

    print(f"\n  起點座標: {lat}, {lon}, alt={alt}m")
    print(f"  漂移速度: {drift_rate_mps} m/s")
    print(f"  飄移/播放: {drift_duration_s} 秒")

    output_bin = os.path.join(BIN_DIR, f"drift_{lat:.5f}_{lon:.5f}.bin")
    sim = _make_simulator()
    ok = sim.generate_bin(
        output_bin=output_bin,
        static_mode=True,
        manual_coords=(lat, lon, alt),
        drift_enabled=True,
        drift_rate_mps=drift_rate_mps,
        drift_duration_s=drift_duration_s
    )
    if not ok:
        print("  [X] Bin 檔案生成失敗")
        return

    if not confirm("\n  是否立即發射?"):
        print(f"  [V] Bin 已儲存: {output_bin}")
        return

    _transmit(output_bin, repeat=drift_duration_s)


def cmd_record(args=None):
    """錄製訊號"""
    header("錄製訊號")

    if args and args.freq:
        freq   = args.freq
        output = args.output or _default_record_path()
    else:
        freq   = prompt_int("  錄製頻率 (Hz, 0=返回)", cfg.get("hackrf.default_freq", 1575420000))
        if freq == 0:
            return
        output = prompt("  輸出檔案路徑 (0=返回)", _default_record_path())
        if output == "0":
            return

    os.makedirs(os.path.dirname(output), exist_ok=True)
    print(f"\n  [*] 開始錄製 → {output}")
    print("      按 Ctrl+C 停止\n")

    ok = hackrf.start_rx(
        filename=output,
        freq_hz=freq,
        sample_rate_hz=cfg.get("hackrf.sample_rate", 2600000),
        lna_gain=cfg.get("hackrf.lna_gain", 16),
        vga_gain=cfg.get("hackrf.vga_gain", 20)
    )
    if not ok:
        return

    try:
        while hackrf.is_running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  [!] 停止錄製")
        hackrf.stop()

    print(f"  [V] 錄製完成: {output}")


def cmd_play(args=None):
    """播放 bin 檔案"""
    header("播放訊號")

    bin_file = repeat = None

    if args and args.file:
        bin_file = args.file
        repeat   = args.repeat
    else:
        # 合併 GPS bin 與錄製 bin
        all_bins = _list_files(BIN_DIR, ".bin") + _list_files(RECORD_DIR, ".bin")
        if not all_bins:
            bin_file = prompt("  [!] 找不到 bin 檔案，請輸入完整路徑 (0=返回)")
            if bin_file == "0":
                return
        else:
            print("\n  可用的 bin 檔案:")
            for i, f in enumerate(all_bins, 1):
                size_mb = os.path.getsize(f) / (1024 * 1024)
                src     = "GPS" if BIN_DIR in f else "REC"
                print(f"    {i}. [{src}] {os.path.basename(f)}  ({size_mb:.1f} MB)")

            idx = prompt_int(f"  選擇編號 (1-{len(all_bins)}, 0=返回)", 1)
            if idx == 0:
                return
            if 1 <= idx <= len(all_bins):
                bin_file = all_bins[idx - 1]
            else:
                print("  [!] 無效的選擇")
                return

    repeat = prompt_int("  重複播放時間 (秒, 0=無限)", 0)

    if not os.path.exists(bin_file):
        print(f"  [Error] 找不到檔案: {bin_file}")
        return

    _transmit(bin_file, repeat=repeat)


# ── Preset 指令 ───────────────────────────────────────────────────────────────

def cmd_preset(args=None):
    if args:
        action = args.preset_action
        if action == "list":
            _show_preset_table(cfg.preset_list())
        elif action == "add":
            cfg.preset_add(args.name, args.lat, args.lon, args.alt)
            print(f"  [V] 已新增 Preset: {args.name}")
        elif action == "delete":
            if cfg.preset_delete(args.name):
                print(f"  [V] 已刪除 Preset: {args.name}")
            else:
                print(f"  [!] 找不到 Preset: {args.name}")
        else:
            print("  [!] 請指定子指令: list / add / delete")
    else:
        _menu_preset()


def _show_preset_table(presets: dict):
    header("Preset 列表")
    if not presets:
        print("  (尚無 Preset)")
        return
    print(f"  {'ID':>3} {'名稱':<16} {'緯度':>12} {'經度':>13} {'高度':>8}")
    print(f"  {'─'*3} {'─'*16} {'─'*12} {'─'*13} {'─'*8}")
    for idx, (name, p) in enumerate(_sorted_presets(presets), 1):
        print(f"  {idx:>3} {name:<16} {p['lat']:>12.6f} {p['lon']:>13.6f} {p['alt']:>7.1f}m")


# ── Config 指令 ───────────────────────────────────────────────────────────────

# 可由 CLI 調整的參數清單
SETTABLE = {
    "1":  ("hackrf.default_freq",        "HackRF 預設頻率 (Hz)",    int),
    "2":  ("hackrf.sample_rate",         "HackRF 採樣率 (Hz)",      int),
    "3":  ("hackrf.tx_gain",             "TX 增益 (0–47)",           int),
    "4":  ("hackrf.lna_gain",            "LNA 增益 (0–40, 8步)",    int),
    "5":  ("hackrf.vga_gain",            "VGA 增益 (0–62, 2步)",    int),
    "6":  ("gps_sim.default_speed_mps",  "GPS 預設速度 (m/s)",       float),
    "7":  ("gps_sim.default_height",     "GPS 預設高度 (m)",         float),
    "8":  ("gps_sim.update_rate_hz",     "GPS 更新頻率 (Hz)",        float),
}

def cmd_config(args=None):
    if args:
        action = args.config_action
        if action == "show":
            cfg.show()
        elif action == "set":
            cfg.set(args.key, args.value)
            print(f"  [V] {args.key} = {cfg.get(args.key)}")
        elif action == "reset":
            cfg.reset()
            print("  [V] 已恢復預設設定 (Preset 不受影響)")
        else:
            print("  [!] 請指定子指令: show / set / reset")
    else:
        _menu_config()


# ── 互動選單 ──────────────────────────────────────────────────────────────────

def _menu_preset():
    while True:
        header("Preset 管理")
        print("  1. 列出所有 Preset")
        print("  2. 新增 Preset")
        print("  3. 刪除 Preset")
        print("  0. 返回")
        choice = prompt("\n  請選擇")

        if choice == "1":
            _show_preset_table(cfg.preset_list())
        elif choice == "2":
            presets = cfg.preset_list()
            next_id = len(presets) + 1
            name = prompt("  Preset 名稱 (留空自動命名)", f"preset_{next_id}")
            lat  = prompt_float("  緯度 (lat)")
            lon  = prompt_float("  經度 (lon)")
            alt  = prompt_float("  高度 (m)", 10.0)
            cfg.preset_add(name, lat, lon, alt)
            print(f"  [V] 已新增 Preset: {name}")
        elif choice == "3":
            presets = cfg.preset_list()
            selected = _select_preset(presets, "要刪除的 Preset")
            if not selected:
                continue
            name, _ = selected
            if cfg.preset_delete(name):
                print(f"  [V] 已刪除: {name}")
            else:
                print(f"  [!] 找不到: {name}")
        elif choice == "0":
            break


def _menu_config():
    while True:
        header("設定管理")
        print("  1. 顯示目前設定")
        print("  2. 修改參數")
        print("  3. 恢復預設值")
        print("  0. 返回")
        choice = prompt("\n  請選擇")

        if choice == "1":
            cfg.show()

        elif choice == "2":
            header("可修改的參數")
            for k, (key, desc, _) in SETTABLE.items():
                print(f"  {k}. {desc:<28}  目前: {cfg.get(key)}")
            sel = prompt("\n  選擇編號 (0=返回)")
            if sel == "0":
                continue
            if sel in SETTABLE:
                key, desc, cast = SETTABLE[sel]
                new_val = prompt(f"  {desc}", cfg.get(key))
                try:
                    cfg.set(key, cast(new_val))
                    print(f"  [V] 已更新 {key} = {cfg.get(key)}")
                except ValueError:
                    print("  [Error] 無效的數值")
            else:
                print("  [!] 無效的選擇")

        elif choice == "3":
            if confirm("  確定要恢復預設值? (Preset 不受影響)"):
                cfg.reset()
                print("  [V] 已恢復預設設定")

        elif choice == "0":
            break


def _menu_gps():
    while True:
        header("GPS 模擬")
        print("  1. 靜態點位")
        print("  2. 動態軌跡 (KML)")
        print("  3. 緩慢飄移誘騙")
        print("  0. 返回")
        choice = prompt("\n  請選擇")

        if choice == "1":
            cmd_gps_static()
        elif choice == "2":
            cmd_gps_dynamic()
        elif choice == "3":
            cmd_gps_drift()
        elif choice == "0":
            break


def run_interactive_menu():
    while True:
        header("HackRF CLI")
        print("  1. 查看硬體資訊")
        print("  2. 更新星歷檔案")
        print("  3. GPS 模擬")
        print("  4. 錄製訊號")
        print("  5. 播放訊號")
        print("  6. Preset 管理")
        print("  7. 設定管理")
        print("  0. 離開")
        choice = prompt("\n  請選擇")

        if   choice == "1": cmd_info()
        elif choice == "2": cmd_ephemeris()
        elif choice == "3": _menu_gps()
        elif choice == "4": cmd_record()
        elif choice == "5": cmd_play()
        elif choice == "6": _menu_preset()
        elif choice == "7": _menu_config()
        elif choice == "0":
            sys.exit(0)

# ── 發射工具 ──────────────────────────────────────────────────────────────────

def _transmit(bin_file: str, repeat: int = 0):
    """
    發射 bin 檔案
    repeat=0  → 無限迴圈，直到 Ctrl+C
    repeat=N  → 播放 N 秒後自動停止
    """
    freq        = cfg.get("hackrf.default_freq", 1575420000)
    sample_rate = cfg.get("hackrf.sample_rate", 2600000)
    tx_gain     = cfg.get("hackrf.tx_gain", 47)

    print(f"\n  發射設定:")
    print(f"    頻率   : {freq:,} Hz")
    print(f"    採樣率 : {sample_rate:,} Hz")
    print(f"    TX 增益: {tx_gain} dB")
    print(f"    模式   : {'無限迴圈' if not repeat else f'{repeat} 秒後停止'}")
    print("    按 Ctrl+C 可隨時停止\n")

    ok = hackrf.start_tx(
        filename=bin_file,
        freq_hz=freq,
        sample_rate_hz=sample_rate,
        tx_gain=tx_gain,
        repeat=True       # 永遠啟用 -R，由程式控制何時停止
    )
    if not ok:
        print("  [Error] 發射啟動失敗")
        return

    try:
        if repeat and repeat > 0:
            time.sleep(repeat)
            hackrf.stop()
            print(f"  [V] 播放完成 ({repeat} 秒)")
        else:
            while hackrf.is_running():
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  [!] 使用者中斷")
        hackrf.stop()


def _make_simulator() -> FakeGPS:
    return FakeGPS(
        target_speed_mps=cfg.get("gps_sim.default_speed_mps", 5.0),
        default_height=cfg.get("gps_sim.default_height", 100.0),
        update_rate_hz=cfg.get("gps_sim.update_rate_hz", 10.0)
    )


def _default_record_path() -> str:
    return os.path.join(RECORD_DIR, f"record_{int(time.time())}.bin")


# ── Argument Parser ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hackrf.py",
        description="HackRF CLI — 無參數時進入互動選單",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  python hackrf.py                                         # 互動選單
  python hackrf.py info                                    # 硬體資訊
  python hackrf.py ephemeris                               # 更新星歷
  python hackrf.py gps static --lat 25.03 --lon 121.56    # 靜態 GPS (無限)
  python hackrf.py gps static --preset 台北101 --repeat 60 # 靜態 GPS (60秒)
  python hackrf.py gps dynamic --kml path/to/route.kml    # 動態 GPS
  python hackrf.py record --freq 433000000                 # 錄製
  python hackrf.py play --file drone.bin --repeat 120      # 播放 120 秒
  python hackrf.py preset add --name 台北101 --lat 25.03 --lon 121.56 --alt 10
  python hackrf.py preset delete --name 台北101
  python hackrf.py config set hackrf.tx_gain 30
  python hackrf.py config show
        """
    )
    sub = parser.add_subparsers(dest="command")

    # info
    sub.add_parser("info", help="查看硬體資訊")

    # ephemeris
    sub.add_parser("ephemeris", help="更新星歷檔案")

    # gps
    gps_p   = sub.add_parser("gps", help="GPS 模擬")
    gps_sub = gps_p.add_subparsers(dest="gps_mode")

    s = gps_sub.add_parser("static", help="靜態定點模擬")
    s.add_argument("--lat",    type=float, help="緯度")
    s.add_argument("--lon",    type=float, help="經度")
    s.add_argument("--alt",    type=float, default=10.0, help="高度 m (預設 10)")
    s.add_argument("--preset", type=str,   help="使用已儲存的 Preset 名稱")
    s.add_argument("--repeat", type=int,   default=0,
                   help="播放秒數 (0=無限, 預設 0)")
    s.add_argument("--drift", action="store_true", help="啟用緩慢漂移 (random walk)")
    s.add_argument("--drift-rate", type=float, default=None,
                   help="漂移速度 m/s (預設 0.05)")
    s.add_argument("--drift-seed", type=int, default=None,
                   help="漂移亂數種子 (選用)")

    d = gps_sub.add_parser("dynamic", help="動態軌跡模擬 (KML)")
    d.add_argument("--kml", type=str, help="KML 檔案路徑")

    # record
    r = sub.add_parser("record", help="錄製訊號")
    r.add_argument("--freq",   type=int, help="錄製頻率 Hz")
    r.add_argument("--output", type=str, help="輸出路徑")

    # play
    p = sub.add_parser("play", help="播放 bin 檔案")
    p.add_argument("--file",   type=str, help="bin 檔案路徑")
    p.add_argument("--repeat", type=int, default=0,
                   help="播放秒數 (0=無限, 預設 0)")

    # preset
    preset_p   = sub.add_parser("preset", help="Preset 管理")
    preset_sub = preset_p.add_subparsers(dest="preset_action")
    preset_sub.add_parser("list", help="列出所有 Preset")

    pa = preset_sub.add_parser("add", help="新增 Preset")
    pa.add_argument("--name", required=True)
    pa.add_argument("--lat",  type=float, required=True)
    pa.add_argument("--lon",  type=float, required=True)
    pa.add_argument("--alt",  type=float, default=10.0)

    pd = preset_sub.add_parser("delete", help="刪除 Preset")
    pd.add_argument("--name", required=True)

    # config
    config_p   = sub.add_parser("config", help="設定管理")
    config_sub = config_p.add_subparsers(dest="config_action")
    config_sub.add_parser("show", help="顯示目前設定")

    cs = config_sub.add_parser("set", help="修改參數 (e.g. hackrf.tx_gain 30)")
    cs.add_argument("key",   help="參數路徑")
    cs.add_argument("value", help="新數值")

    config_sub.add_parser("reset", help="恢復預設值")

    return parser


# ── 主程式 ─────────────────────────────────────────────────────────────────────

def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.command is None:
        run_interactive_menu()
        return

    dispatch = {
        "info":      cmd_info,
        "ephemeris": cmd_ephemeris,
        "record":    cmd_record,
        "play":      cmd_play,
        "preset":    cmd_preset,
        "config":    cmd_config,
    }

    if args.command == "gps":
        if not args.gps_mode:
            parser.parse_args(["gps", "--help"])
        elif args.gps_mode == "static":
            cmd_gps_static(args)
        elif args.gps_mode == "dynamic":
            cmd_gps_dynamic(args)
    elif args.command in dispatch:
        dispatch[args.command](args)


if __name__ == "__main__":
    main()