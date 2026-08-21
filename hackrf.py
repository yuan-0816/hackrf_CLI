"""
HackRF CLI
支援互動式選單 & 指令列參數雙模式

使用方式:
  python hackrf.py                              # 進入互動選單
  python hackrf.py info                         # 直接執行指令
  python hackrf.py gps static --lat 25.03 --lon 121.56 --repeat 60
"""

import argparse
import datetime
import math
import unicodedata
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config_manager import ConfigManager
from utils.tool import get_project_root
from utils.get_latest_brdc import fetch_latest_ephemeris
from hackrf_wrapper import HackRFCLI
from fake_gps import FakeGPS

# ── 路徑設定 ─────────────────────────────────────────────────────────────────

PROJECT_ROOT = get_project_root()
BIN_DIR      = os.path.join(PROJECT_ROOT, "data", "fake_signal", "gps")
RECORD_DIR   = os.path.join(PROJECT_ROOT, "data", "recorded")
CSV_DIR      = os.path.join(PROJECT_ROOT, "data", "fake_path")

DEFAULT_DRIFT_RATE_MPS = 0.05
DISK_RESERVE_BYTES = 256 * 1024 * 1024
MIN_STATIC_DURATION_S = 1
DEFAULT_STATIC_DURATION_S = 60
BACK_COMMANDS = {"b", "back"}

# ── 全域實例 ──────────────────────────────────────────────────────────────────

cfg    = ConfigManager()
hackrf = HackRFCLI()

# ── UI 工具函數 ────────────────────────────────────────────────────────────────

def header(title: str):
    print(f"\n{'─' * 80}")
    print(f"{' ' * 4}{title}")
    print(f"{'─' * 80}")

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


def is_back(value: str) -> bool:
    return value.strip().lower() in BACK_COMMANDS


def prompt_float_or_back(text: str, default=None) -> float | None:
    while True:
        raw = prompt(text, default)
        if is_back(raw):
            return None
        try:
            return float(raw)
        except ValueError:
            print("  [!] 請輸入有效的數字，或輸入 b 返回")


def prompt_int_or_back(text: str, default=None) -> int | None:
    while True:
        raw = prompt(text, default)
        if is_back(raw):
            return None
        try:
            return int(raw)
        except ValueError:
            print("  [!] 請輸入有效的整數，或輸入 b 返回")

def _parse_coords_line(line: str, default_alt: float) -> tuple[float, float, float] | None:
    parts = [p for p in re.split(r"[,\s]+", line.strip()) if p]
    if len(parts) not in (2, 3):
        return None
    try:
        lat = float(parts[0])
        lon = float(parts[1])
        alt = float(parts[2]) if len(parts) == 3 else float(default_alt)
    except ValueError:
        return None
    return lat, lon, alt

def prompt_coords(default_alt: float) -> tuple[float, float, float] | None:
    while True:
        line = prompt("  一次輸入 緯度 經度 高度 (例如: 25.03 121.56 100, b=返回)", "")
        if not line:
            lat = prompt_float_or_back("  緯度 (lat, b=返回)")
            if lat is None:
                return None
            lon = prompt_float_or_back("  經度 (lon, b=返回)")
            if lon is None:
                return None
            alt = prompt_float_or_back("  高度 (m, b=返回)", default_alt)
            if alt is None:
                return None
            return lat, lon, alt

        if is_back(line):
            return None

        parsed = _parse_coords_line(line, default_alt)
        if parsed:
            return parsed
        print("  [!] 請輸入 2~3 個數字，例如: 25.03 121.56 100")

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

    raw = prompt(f"  選擇 {label} 編號 (1-{len(items)}, b=返回)", "b")
    if is_back(raw):
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


def _display_width(text: object) -> int:
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return width


def _pad_display(text: object, width: int, align: str = "<") -> str:
    text = str(text)
    padding = max(width - _display_width(text), 0)
    if align == ">":
        return " " * padding + text
    return text + " " * padding

def confirm(text: str) -> bool:
    return prompt(f"{text} (y/n)", "n").lower() == "y"


def prompt_gps_time_mode() -> str | None:
    print("\n  GPS 時間模式")
    print("    1. 星歷一致模式 (建議，保留 TOE/TOC，使用 -t)")
    print("    2. 當前時間模式 (將 TOE/TOC 平移至現在，使用 -T now)")
    while True:
        selected = prompt("  請選擇時間模式 (1-2, b=返回)", "1")
        if is_back(selected):
            return None
        if selected == "1":
            return "ephemeris"
        if selected == "2":
            print(
                "  [Warning] 此模式產生合成的當前時間星座，"
                "不代表目前真實衛星軌道。"
            )
            if confirm("  確認使用 -T now?"):
                return "shifted-now"
            continue
        print("  [!] 無效的選擇")

def pick_file(directory: str, extension: str, label: str) -> str | None:
    files = _list_files(directory, extension)
    if not files:
        print(f"  [!] 在 {directory} 找不到 {extension} 檔案")
        return None

    print(f"\n  可用的{label}:")
    for i, f in enumerate(files, 1):
        size_mb = os.path.getsize(f) / (1024 * 1024)
        print(f"    {i}. {os.path.basename(f)}  ({size_mb:.1f} MB)")

    idx = prompt_int_or_back(f"  選擇編號 (1-{len(files)}, b=返回)", 1)
    if idx is None:
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
    header("硬體資訊")
    try:
        result = subprocess.run(
            [hackrf.info_exec],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        output = (result.stdout + result.stderr).strip()
        print(output if output else "  [!] 未偵測到 HackRF 設備")
    except FileNotFoundError:
        print("  [Error] 找不到 hackrf_info，請確認已安裝 hackrf 套件")


def cmd_ephemeris(_args=None):
    header("更新星歷檔案")
    print("  [*] 正在重新取得今日最新星歷...")
    path = fetch_latest_ephemeris(force=True)
    if path:
        print(f"  [V] 星歷更新成功: {path}")
        bounds = FakeGPS._get_ephemeris_time_bounds(path)
        if bounds:
            _, latest = bounds
            now = datetime.datetime.now(datetime.timezone.utc)
            lag_seconds = max((now - latest).total_seconds(), 0)
            print(
                "  [*] 星歷最晚 epoch (UTC): "
                f"{latest.strftime('%Y/%m/%d,%H:%M:%S')}"
            )
            print(f"  [*] 與目前 UTC 相差: {lag_seconds:.0f} 秒")
    else:
        print("  [X] 星歷更新失敗")


def cmd_gps_static(args=None):
    header("GPS 靜態點位模擬")

    lat = lon = alt = None
    duration = DEFAULT_STATIC_DURATION_S

    drift_enabled = False
    drift_rate_mps = DEFAULT_DRIFT_RATE_MPS
    drift_seed = None
    time_mode = "ephemeris"

    if args and (args.lat or args.preset):
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
        if args.duration is not None and args.repeat is not None:
            print("  [Error] --duration 與舊版 --repeat 不可同時使用")
            return
        if args.repeat is not None:
            print("  [Warning] --repeat 已棄用，本次將作為 --duration 處理")
            duration = args.repeat
        elif args.duration is not None:
            duration = args.duration
        time_mode = args.time_mode
        if args.drift or args.drift_rate is not None or args.drift_seed is not None:
            drift_enabled = True
            drift_rate_mps = args.drift_rate if args.drift_rate is not None else DEFAULT_DRIFT_RATE_MPS
            drift_seed = args.drift_seed
    else:
        presets = cfg.preset_list()
        if presets:
            _show_preset_table(presets)
            items = _sorted_presets(presets)
            raw = prompt(f"\n  選擇 Preset 編號 (1-{len(items)}, m=手動輸入座標, b=返回)", "m").lower()
            if is_back(raw):
                return
            if raw != "m" and raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(items):
                    _, p = items[idx - 1]
                    lat, lon, alt = p["lat"], p["lon"], p["alt"]
                else:
                    print("  [!] 無效的編號，改為手動輸入")
            if lat is None:
                coords = prompt_coords(cfg.get("gps_sim.default_height", 100.0))
                if not coords:
                    return
                lat, lon, alt = coords
        else:
            coords = prompt_coords(cfg.get("gps_sim.default_height", 100.0))
            if not coords:
                return
            lat, lon, alt = coords

        while True:
            duration = prompt_int_or_back(
                "  GPS 訊號時長 (秒, 最少 1 秒, b=返回)",
                DEFAULT_STATIC_DURATION_S,
            )
            if duration is None:
                return
            if duration >= MIN_STATIC_DURATION_S:
                break
            print("  [!] 時長必須至少為 1 秒")

        time_mode = prompt_gps_time_mode()
        if time_mode is None:
            return

    if duration < MIN_STATIC_DURATION_S:
        print("  [Error] 時長必須至少為 1 秒")
        return

    sample_rate = cfg.get("hackrf.sample_rate", 2600000)
    estimated_size = sample_rate * 2 * duration
    os.makedirs(BIN_DIR, exist_ok=True)
    free_space = shutil.disk_usage(BIN_DIR).free
    print(f"\n  座標: {lat}, {lon}, alt={alt}m")
    print(f"  訊號時長: {duration} 秒（單次播放）")
    print(f"  預估檔案大小: {estimated_size / (1024 * 1024):.1f} MB")
    if estimated_size > max(0, free_space - DISK_RESERVE_BYTES):
        print(
            "  [Error] 磁碟空間不足："
            f"可用 {free_space / (1024 * 1024):.1f} MB"
        )
        return

    output_bin = os.path.join(
        BIN_DIR,
        f"static_{lat:.5f}_{lon:.5f}_{duration}s.bin",
    )
    sim = _make_simulator()
    actual_bin = sim.generate_bin(
        output_bin=output_bin,
        static_mode=True,
        manual_coords=(lat, lon, alt),
        drift_enabled=drift_enabled,
        drift_rate_mps=drift_rate_mps,
        drift_seed=drift_seed,
        time_mode=time_mode,
        drift_duration_s=duration,
    )
    if not actual_bin:
        print("  [X] Bin 檔案生成失敗")
        return

    if args is None and not confirm("\n  是否立即發射?"):
        print(f"  [V] Bin 已儲存: {actual_bin}")
        return

    _transmit(actual_bin, repeat=0, loop=False)


def cmd_gps_traction(args=None):
    header("牽引式 GPS 實驗軌跡")

    if args and args.lat is not None:
        lat, lon, alt = args.lat, args.lon, args.alt
        heading  = args.heading
        speed    = args.speed
        ramp     = args.ramp
        duration = args.duration
        hold     = args.hold
        final_hold = args.final_hold
        time_mode = args.time_mode
    else:
        print("  此模式從載具當前 GPS 位置出發，產生緩慢移動軌跡。")
        print("  [Warning] 軌跡參數不保證 EKF3 接受，需以 XKF3/XKF4 log 驗證。\n")

        lat = lon = alt = None
        heading = 0.0
        speed = 0.5
        ramp = 20.0
        duration = 120.0
        hold = 10.0
        final_hold = 5.0
        time_mode = "ephemeris"
        step = 1

        while True:
            if step == 1:
                print("  [步驟 1/4] 輸入無人機當前真實座標")
                coords = prompt_coords(cfg.get("gps_sim.default_height", 50.0))
                if not coords:
                    return
                lat, lon, alt = coords
                step = 2

            elif step == 2:
                print("\n  [步驟 2/4] 設定誘騙方向")
                print("    0° = 正北  |  90° = 正東  |  180° = 正南  |  270° = 正西")
                val = prompt_float_or_back("  方向 (度, b=返回)", heading)
                if val is None:
                    step = 1
                    print()
                else:
                    heading = val
                    step = 3

            elif step == 3:
                print("\n  [步驟 3/4] 設定速度")
                print("    建議從低速開始；實際接受門檻取決於 EKF covariance 與 GPS 精度。")
                val = prompt_float_or_back("  目標速度 (m/s, b=返回)", speed)
                if val is None:
                    step = 2
                else:
                    speed = val
                    step = 4

            elif step == 4:
                print(f"\n  [步驟 4/4] 設定時長")
                val = prompt_float_or_back("  加速與減速時間 (各自秒數, b=返回)", ramp)
                if val is None:
                    step = 3
                else:
                    ramp = val
                    val = prompt_float_or_back("  誘騙總時長 (s, b=返回)", duration)
                    if val is None:
                        step = 4
                    else:
                        duration = val
                        val = prompt_float_or_back("  起始靜止時間 (s, b=返回)", hold)
                        if val is None:
                            step = 4
                        else:
                            hold = val
                            val = prompt_float_or_back("  終點靜止時間 (s, b=返回)", final_hold)
                            if val is None:
                                step = 4
                            else:
                                final_hold = val
                                time_mode = prompt_gps_time_mode()
                                if time_mode is None:
                                    return
                                break

    numeric_values = (lat, lon, alt, heading, speed, ramp, duration, hold, final_hold)
    if not all(math.isfinite(value) for value in numeric_values):
        print("  [Error] 軌跡參數必須是有限數值")
        return
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        print("  [Error] 緯度必須介於 -90~90，經度必須介於 -180~180")
        return
    if speed <= 0 or ramp <= 0:
        print("  [Error] 速度與加減速時間必須大於 0")
        return
    if duration <= 0:
        print("  [Error] 總時長必須大於 0 秒")
        return
    active_duration = duration - hold - final_hold
    if hold < 0 or final_hold < 0 or active_duration < 2 * ramp:
        print("  [Error] 總時長必須足以容納起始/終點靜止及完整加減速")
        return
    heading %= 360.0

    peak_accel = 1.5 * speed / ramp if ramp > 0 else float("inf")
    cruise_t = active_duration - 2 * ramp
    est_dist = speed * ramp + speed * cruise_t

    print(f"\n  ── 執行摘要 ─────────────────────────────")
    print(f"  起始座標 : {lat:.6f}, {lon:.6f}, Alt={alt:.1f}m")
    print(f"  方向     : {heading}°（0=正北, 90=正東）")
    print(f"  目標速度 : {speed} m/s")
    print(f"  加/減速   : 各 {ramp} s  →  S-curve 峰值加速度 {peak_accel:.3f} m/s^2")
    print(f"  靜止時間 : 起始 {hold} s / 終點 {final_hold} s")
    print(f"  總時長   : {duration} s  →  預計移動 ≈ {est_dist:.1f} m")
    print(f"  ─────────────────────────────────────────")

    if args is None and not confirm("\n  確認執行?"):
        return

    csv_file   = os.path.join(CSV_DIR, f"traction_{lat:.5f}_{lon:.5f}.csv")
    output_bin = os.path.join(BIN_DIR,  f"traction_{lat:.5f}_{lon:.5f}.bin")

    os.makedirs(CSV_DIR, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)

    sample_rate = cfg.get("hackrf.sample_rate", 2600000)
    estimated_size = sample_rate * 2 * duration
    free_space = shutil.disk_usage(BIN_DIR).free
    if estimated_size > max(0, free_space - DISK_RESERVE_BYTES):
        print(
            "  [Error] 磁碟空間不足："
            f"預估需要 {estimated_size / (1024 * 1024):.1f} MB，"
            f"可用 {free_space / (1024 * 1024):.1f} MB"
        )
        return

    sim = _make_simulator()

    ok = sim._generate_traction_csv(
        csv_file=csv_file,
        start_lat=lat, start_lon=lon, start_alt=alt,
        heading_deg=heading,
        target_speed_mps=speed,
        ramp_duration_s=ramp,
        total_duration_s=duration,
        hold_duration_s=hold,
        final_hold_duration_s=final_hold,
    )
    if not ok:
        print("  [X] CSV 生成失敗")
        return

    output_bin = sim.generate_bin(
        output_bin=output_bin,
        static_mode=False,
        csv_file=csv_file,
        time_mode=time_mode,
    )
    if not output_bin:
        print("  [X] Bin 生成失敗")
        return

    if args is None and not confirm("\n  是否立即發射?"):
        print(f"  [V] Bin 已儲存: {output_bin}")
        return

    _transmit(output_bin, repeat=0, loop=False)


def cmd_record(args=None):
    header("錄製訊號")

    if args and args.freq:
        freq   = args.freq
        output = args.output or _default_record_path()
    else:
        freq   = prompt_int_or_back("  錄製頻率 (Hz, b=返回)", cfg.get("hackrf.default_freq", 1575420000))
        if freq is None:
            return
        output = prompt("  輸出檔案路徑 (b=返回)", _default_record_path())
        if is_back(output):
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
    header("播放訊號")

    bin_file = repeat = None

    if args and args.file:
        bin_file = args.file
        repeat   = args.repeat
    else:
        while True:
            all_bins = _list_files(BIN_DIR, ".bin") + _list_files(RECORD_DIR, ".bin")
            if not all_bins:
                bin_file = prompt("  [!] 找不到 bin 檔案，請輸入完整路徑 (b=返回)")
                if is_back(bin_file):
                    return
                break

            _show_bin_files(all_bins)
            choice = prompt(f"  選擇編號播放 (1-{len(all_bins)}, d=刪除 bin, b=返回)", "1").lower()
            if is_back(choice):
                return
            if choice == "d":
                _delete_bin_from_list(all_bins)
                continue
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(all_bins):
                    bin_file = all_bins[idx - 1]
                    break

            print("  [!] 無效的選擇")

    repeat = prompt_int_or_back("  重複播放時間 (秒, 0=無限, b=返回)", 0)
    if repeat is None:
        return

    if not os.path.exists(bin_file):
        print(f"  [Error] 找不到檔案: {bin_file}")
        return

    _transmit(bin_file, repeat=repeat)


def _show_bin_files(files: list[str]):
    print("\n  可用的 bin 檔案:")
    for i, f in enumerate(files, 1):
        size_mb = os.path.getsize(f) / (1024 * 1024)
        src = "GPS" if os.path.commonpath([BIN_DIR, f]) == BIN_DIR else "REC"
        print(f"    {i}. [{src}] {os.path.basename(f)}  ({size_mb:.1f} MB)")


def _delete_bin_from_list(files: list[str]):
    idx = prompt_int_or_back(f"  選擇要刪除的編號 (1-{len(files)}, b=返回)", "b")
    if idx is None:
        return
    if not 1 <= idx <= len(files):
        print("  [!] 無效的選擇")
        return

    target = files[idx - 1]
    print(f"\n  將刪除: {target}")
    if not confirm("  確定刪除此 bin 檔?"):
        print("  [!] 已取消刪除")
        return

    try:
        os.remove(target)
        print(f"  [V] 已刪除: {os.path.basename(target)}")
    except OSError as exc:
        print(f"  [Error] 刪除失敗: {exc}")


# ── Preset 指令 ───────────────────────────────────────────────────────────────

def cmd_preset(args=None):
    if args:
        action = args.preset_action
        if action == "list":
            _show_preset_table(cfg.preset_list())
        elif action == "add":
            cfg.preset_add(args.name, args.lat, args.lon, args.alt)
            print(f"  [V] 已新增 Preset: {args.name}")
        elif action == "edit":
            if all(
                value is None
                for value in (args.new_name, args.lat, args.lon, args.alt)
            ):
                print("  [!] 請至少提供一個要修改的欄位")
                return
            try:
                updated = cfg.preset_update(
                    args.name,
                    new_name=args.new_name,
                    lat=args.lat,
                    lon=args.lon,
                    alt=args.alt,
                )
            except ValueError as exc:
                print(f"  [Error] {exc}")
                return
            if updated:
                print(f"  [V] 已更新 Preset: {args.new_name or args.name}")
            else:
                print(f"  [!] 找不到 Preset: {args.name}")
        elif action == "delete":
            if cfg.preset_delete(args.name):
                print(f"  [V] 已刪除 Preset: {args.name}")
            else:
                print(f"  [!] 找不到 Preset: {args.name}")
        else:
            print("  [!] 請指定子指令: list / add / edit / delete")
    else:
        _menu_preset()


def _show_preset_table(presets: dict):
    header("Preset 列表")
    if not presets:
        print("  (尚無 Preset)")
        return

    id_w, name_w, lat_w, lon_w, alt_w = 3, 16, 12, 13, 8
    print(
        "  "
        + _pad_display("ID", id_w, ">") + " "
        + _pad_display("名稱", name_w) + " "
        + _pad_display("緯度", lat_w, ">") + " "
        + _pad_display("經度", lon_w, ">") + " "
        + _pad_display("高度", alt_w, ">")
    )
    print(
        "  "
        + "-" * id_w + " "
        + "-" * name_w + " "
        + "-" * lat_w + " "
        + "-" * lon_w + " "
        + "-" * alt_w
    )
    for idx, (name, p) in enumerate(_sorted_presets(presets), 1):
        print(
            "  "
            + _pad_display(idx, id_w, ">") + " "
            + _pad_display(name, name_w) + " "
            + _pad_display(f"{p['lat']:.6f}", lat_w, ">") + " "
            + _pad_display(f"{p['lon']:.6f}", lon_w, ">") + " "
            + _pad_display(f"{p['alt']:.1f}m", alt_w, ">")
        )


# ── Config 指令 ───────────────────────────────────────────────────────────────

SETTABLE_GROUPS = [
    ("HackRF", [
        ("hackrf.default_freq",       "預設頻率 (Hz)",         int),
        ("hackrf.sample_rate",        "採樣率 (Hz)",           int),
        ("hackrf.tx_gain",            "TX 增益 (0-47)",        int),
        ("hackrf.lna_gain",           "LNA 增益 (0-40, 8步)", int),
        ("hackrf.vga_gain",           "VGA 增益 (0-62, 2步)", int),
    ]),
    ("GPS 模擬", [
        ("gps_sim.default_speed_mps", "預設速度 (m/s)",        float),
        ("gps_sim.default_height",    "預設高度 (m)",           float),
        ("gps_sim.update_rate_hz",    "更新頻率 (Hz)",          float),
    ]),
    ("星歷", [
        ("ephemeris.save_dir",        "儲存目錄",               str),
        ("ephemeris.max_files",       "最多保留份數",            int),
    ]),
]

def _build_settable_index() -> dict:
    idx = {}
    n = 1
    for _, items in SETTABLE_GROUPS:
        for key, desc, cast in items:
            idx[str(n)] = (key, desc, cast)
            n += 1
    return idx

SETTABLE = _build_settable_index()


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
        print("  3. 編輯 Preset")
        print("  4. 刪除 Preset")
        print("  b. 返回")
        choice = prompt("\n  請選擇")

        if choice == "1":
            _show_preset_table(cfg.preset_list())
        elif choice == "2":
            presets = cfg.preset_list()
            next_id = len(presets) + 1
            name = prompt("  Preset 名稱 (留空自動命名)", f"preset_{next_id}")
            lat  = prompt_float_or_back("  緯度 (lat, b=返回)")
            if lat is None:
                continue
            lon  = prompt_float_or_back("  經度 (lon, b=返回)")
            if lon is None:
                continue
            alt  = prompt_float_or_back("  高度 (m, b=返回)", 10.0)
            if alt is None:
                continue
            cfg.preset_add(name, lat, lon, alt)
            print(f"  [V] 已新增 Preset: {name}")
        elif choice == "3":
            presets = cfg.preset_list()
            selected = _select_preset(presets, "要編輯的 Preset")
            if not selected:
                continue
            name, current = selected

            new_name = prompt("  Preset 名稱", name)
            lat = prompt_float_or_back("  緯度 (lat, b=返回)", current["lat"])
            if lat is None:
                continue
            lon = prompt_float_or_back("  經度 (lon, b=返回)", current["lon"])
            if lon is None:
                continue
            alt = prompt_float_or_back("  高度 (m, b=返回)", current["alt"])
            if alt is None:
                continue

            try:
                cfg.preset_update(
                    name,
                    new_name=new_name,
                    lat=lat,
                    lon=lon,
                    alt=alt,
                )
                print(f"  [V] 已更新 Preset: {new_name}")
            except ValueError as exc:
                print(f"  [Error] {exc}")
        elif choice == "4":
            presets = cfg.preset_list()
            selected = _select_preset(presets, "要刪除的 Preset")
            if not selected:
                continue
            name, _ = selected
            if cfg.preset_delete(name):
                print(f"  [V] 已刪除: {name}")
            else:
                print(f"  [!] 找不到: {name}")
        elif is_back(choice):
            break


def _show_config_params():
    n = 1
    for group_name, items in SETTABLE_GROUPS:
        print(f"\n  ── {group_name} {'─' * (30 - len(group_name))}")
        for key, desc, _ in items:
            print(f"  {n:2}. {_pad_display(desc, 20)}  目前: {cfg.get(key)}")
            n += 1


def _menu_config():
    while True:
        header("設定管理")
        print("  1. 顯示目前設定")
        print("  2. 修改參數")
        print("  3. 恢復預設值")
        print("  b. 返回")
        choice = prompt("\n  請選擇")

        if choice == "1":
            cfg.show()

        elif choice == "2":
            header("可修改的參數")
            _show_config_params()
            sel = prompt(f"\n  選擇編號 (1-{len(SETTABLE)}, b=返回)")
            if is_back(sel):
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

        elif is_back(choice):
            break


def cmd_gps_jam(args=None):
    header("屏蔽GPS信號")

    GPS_L1_FREQ  = 1575420000
    JAM_GAIN     = 47
    NOISE_SIZE_B = 4 * 1024 * 1024

    print(f"  發射寬頻噪聲以屏蔽 GPS L1 信號")
    print(f"    頻率   : {GPS_L1_FREQ:,} Hz")
    print(f"    TX 增益: {JAM_GAIN} dB")
    print(f"    按 Ctrl+C 可隨時停止\n")

    if args is None and not confirm("  確認執行?"):
        return

    noise_path = os.path.join(tempfile.gettempdir(), "hackrf_jam_noise.bin")
    print("  [*] 生成噪聲資料...")
    with open(noise_path, "wb") as f:
        f.write(os.urandom(NOISE_SIZE_B))

    ok = hackrf.start_tx(
        filename=noise_path,
        freq_hz=GPS_L1_FREQ,
        sample_rate_hz=cfg.get("hackrf.sample_rate", 2600000),
        tx_gain=JAM_GAIN,
        repeat=True
    )
    if not ok:
        print("  [Error] 發射啟動失敗")
        try:
            os.remove(noise_path)
        except OSError:
            pass
        return

    try:
        while hackrf.is_running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  [!] 使用者中斷")
        hackrf.stop()
    finally:
        try:
            os.remove(noise_path)
        except OSError:
            pass


def _menu_gps():
    while True:
        header("GPS 模擬 / 誘騙")
        print("  1. 固定點位誘騙   (瞬間跳躍至目標座標)")
        print("  2. 牽引式實驗軌跡 (緩慢移動，需以 EKF log 驗證)")
        print("  3. 屏蔽GPS信號    (Freq=1575420000Hz, Gain=47 噪聲覆蓋)")
        print("  b. 返回")
        choice = prompt("\n  請選擇")

        if choice == "1":
            cmd_gps_static()
        elif choice == "2":
            cmd_gps_traction()
        elif choice == "3":
            cmd_gps_jam()
        elif is_back(choice):
            break


def run_interactive_menu():
    while True:
        status = "HackRF 已連接" if hackrf.is_device_connected() else "HackRF 未連接"
        header(f"無人機資安GPS檢測工具\n    {status}")
        print()
        print("  1. 查看硬體資訊")
        print("  2. 更新星歷檔案")
        print("  3. GPS 模擬")
        print("  4. 錄製訊號")
        print("  5. 播放訊號")
        print("  6. Preset 管理")
        print("  7. 設定管理")
        print("  q. 離開")
        choice = prompt("\n  請選擇")

        if   choice == "1": cmd_info()
        elif choice == "2": cmd_ephemeris()
        elif choice == "3": _menu_gps()
        elif choice == "4": cmd_record()
        elif choice == "5": cmd_play()
        elif choice == "6": _menu_preset()
        elif choice == "7": _menu_config()
        elif choice.lower() == "q":
            sys.exit(0)

# ── 發射工具 ──────────────────────────────────────────────────────────────────

def _transmit(bin_file: str, repeat: int = 0, loop: bool = True):
    freq        = cfg.get("hackrf.default_freq", 1575420000)
    sample_rate = cfg.get("hackrf.sample_rate", 2600000)
    tx_gain     = cfg.get("hackrf.tx_gain", 47)

    print(f"\n  發射設定:")
    print(f"    頻率   : {freq:,} Hz")
    print(f"    採樣率 : {sample_rate:,} Hz")
    print(f"    TX 增益: {tx_gain} dB")
    if not loop:
        mode = "單次播放"
    elif repeat:
        mode = f"{repeat} 秒後停止"
    else:
        mode = "無限迴圈"
    print(f"    模式   : {mode}")
    print("    按 Ctrl+C 可隨時停止\n")

    ok = hackrf.start_tx(
        filename=bin_file,
        freq_hz=freq,
        sample_rate_hz=sample_rate,
        tx_gain=tx_gain,
        repeat=loop
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
  python hackrf.py                                                          # 互動選單
  python hackrf.py info                                                     # 硬體資訊
  python hackrf.py ephemeris                                                # 更新星歷

  # GPS 誘騙
  python hackrf.py gps static --lat 25.03 --lon 121.56 --duration 60       # 固定點位 (60秒單次播放)
  python hackrf.py gps static --preset 台北101 --duration 300              # 固定點位 (5分鐘上限)
  python hackrf.py gps traction --lat 25.03 --lon 121.56 --heading 90      # 牽引式誘騙 (往正東)
  python hackrf.py gps traction --lat 25.03 --lon 121.56 --speed 0.3 --ramp 30 --duration 180

  # 錄製 / 播放
  python hackrf.py record --freq 433000000                                  # 錄製
  python hackrf.py play --file drone.bin --repeat 120                       # 播放 120 秒

  # Preset / 設定
  python hackrf.py preset add --name 台北101 --lat 25.03 --lon 121.56 --alt 10
  python hackrf.py preset delete --name 台北101
  python hackrf.py config set hackrf.tx_gain 30
  python hackrf.py config show
        """
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="查看硬體資訊")
    sub.add_parser("ephemeris", help="更新星歷檔案")

    gps_p   = sub.add_parser("gps", help="GPS 模擬")
    gps_sub = gps_p.add_subparsers(dest="gps_mode")

    s = gps_sub.add_parser("static", help="靜態定點模擬")
    s.add_argument("--lat",    type=float, help="緯度")
    s.add_argument("--lon",    type=float, help="經度")
    s.add_argument("--alt",    type=float, default=10.0, help="高度 m (預設 10)")
    s.add_argument("--preset", type=str,   help="使用已儲存的 Preset 名稱")
    s.add_argument(
        "--duration",
        type=int,
        default=None,
        help=f"GPS 訊號時長秒數 (最少 1 秒, 預設 {DEFAULT_STATIC_DURATION_S})",
    )
    s.add_argument("--repeat", type=int, default=None, help=argparse.SUPPRESS)
    s.add_argument("--drift",  action="store_true", help="啟用緩慢漂移 (random walk)")
    s.add_argument("--drift-rate", type=float, default=None, help="漂移速度 m/s (預設 0.05)")
    s.add_argument("--drift-seed", type=int,   default=None, help="漂移亂數種子 (選用)")
    s.add_argument(
        "--time-mode",
        choices=("ephemeris", "shifted-now"),
        default="ephemeris",
        help=(
            "GPS 時間模式: ephemeris=保留星歷時間; "
            "shifted-now=使用 -T now 平移 TOE/TOC"
        ),
    )

    t = gps_sub.add_parser("traction", help="牽引式 GPS 實驗軌跡（緩慢移動）")
    t.add_argument("--lat",      type=float, required=True, help="無人機當前緯度")
    t.add_argument("--lon",      type=float, required=True, help="無人機當前經度")
    t.add_argument("--alt",      type=float, default=50.0,  help="無人機當前高度 m (預設 50)")
    t.add_argument("--heading",  type=float, default=0.0,   help="誘騙方向 度 (0=正北, 90=正東, 預設 0)")
    t.add_argument("--speed",    type=float, default=0.5,   help="目標速度 m/s (預設 0.5)")
    t.add_argument("--ramp",     type=float, default=20.0,  help="加速與減速時間 s (各自，預設 20)")
    t.add_argument("--duration", type=float, default=120.0, help="總時長 s (預設 120)")
    t.add_argument("--hold",     type=float, default=10.0,  help="起始靜止時間 s (預設 10)")
    t.add_argument("--final-hold", type=float, default=5.0, help="終點靜止時間 s (預設 5)")
    t.add_argument(
        "--time-mode",
        choices=("ephemeris", "shifted-now"),
        default="ephemeris",
        help=(
            "GPS 時間模式: ephemeris=保留星歷時間; "
            "shifted-now=使用 -T now 平移 TOE/TOC"
        ),
    )

    r = sub.add_parser("record", help="錄製訊號")
    r.add_argument("--freq",   type=int, help="錄製頻率 Hz")
    r.add_argument("--output", type=str, help="輸出路徑")

    p = sub.add_parser("play", help="播放 bin 檔案")
    p.add_argument("--file",   type=str, help="bin 檔案路徑")
    p.add_argument("--repeat", type=int, default=0, help="播放秒數 (0=無限, 預設 0)")

    preset_p   = sub.add_parser("preset", help="Preset 管理")
    preset_sub = preset_p.add_subparsers(dest="preset_action")
    preset_sub.add_parser("list", help="列出所有 Preset")

    pa = preset_sub.add_parser("add", help="新增 Preset")
    pa.add_argument("--name", required=True)
    pa.add_argument("--lat",  type=float, required=True)
    pa.add_argument("--lon",  type=float, required=True)
    pa.add_argument("--alt",  type=float, default=10.0)

    pe = preset_sub.add_parser("edit", help="編輯 Preset")
    pe.add_argument("--name", required=True, help="現有 Preset 名稱")
    pe.add_argument("--new-name", help="新的 Preset 名稱")
    pe.add_argument("--lat", type=float, help="新緯度")
    pe.add_argument("--lon", type=float, help="新經度")
    pe.add_argument("--alt", type=float, help="新高度")

    pd = preset_sub.add_parser("delete", help="刪除 Preset")
    pd.add_argument("--name", required=True)

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
        elif args.gps_mode == "traction":
            cmd_gps_traction(args)
    elif args.command in dispatch:
        dispatch[args.command](args)


if __name__ == "__main__":
    main()
