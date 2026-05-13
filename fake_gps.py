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
        self.gps_sim_exe_path = gps_sim_exe_path

        self.hackrf = HackRFCLI()


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
        
        # 確保輸出目錄存在
        os.makedirs(os.path.dirname(output_bin), exist_ok=True)
        
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

    KML_PATH = os.path.join(get_project_root(), "data", "fake_path", "NMSL.kml")
    CSV_PATH = os.path.join(get_project_root(), "data", "fake_path", "drone_motion.csv")
    BIN_PATH = os.path.join(get_project_root(), "data", "fake_signal", "gps", "drone_motion.bin")
    STATIC_BIN_PATH = os.path.join(get_project_root(), "data", "fake_signal", "gps", "static_point.bin")


    # 使用範例: 模擬無人機以 5 m/s (約 18 km/h) 飛行
    simulator = FakeGPS(target_speed_mps=5.0, update_rate_hz=10.0, default_height=100.0)

    # 2. 轉換 KML -> CSV
    # if simulator.kml_to_csv(KML_PATH, CSV_PATH):
        
    # 3. 生成 .bin 檔案
    simulator.generate_bin(
                output_bin=STATIC_BIN_PATH,
                static_mode=True,
                # manual_coords=(19.685885, -155.954189, 100.0),
                manual_coords=(23.14020741597821, 113.34317939905957, 10.0),
                csv_file=CSV_PATH
    )
    
    # 4. 發射信號
    # if success:
    simulator.transmit_bin(
        bin_file=STATIC_BIN_PATH,
        freq=1575420000,       # GPS L1 頻率
        sample_rate=2600000,   # 建議使用 2.6 MHz
        tx_gain=47             # HackRF TX 增益 (0-47)
    )
    # else:
        # print("[X] 模擬檔案生成失敗，取消發射。")


