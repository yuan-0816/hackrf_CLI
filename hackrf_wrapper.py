import subprocess
import os
import shutil

class HackRFCLI:
    def __init__(self):
        """
        初始化 HackRF CLI 封裝器
        會自動檢查系統中是否有 hackrf_transfer 和 hackrf_sweep
        """
        self.transfer_exec = "hackrf_transfer"
        self.sweep_exec = "hackrf_sweep"
        self.process = None

    def is_installed(self):
        """檢查必要指令是否存在"""
        t_check = shutil.which(self.transfer_exec) is not None
        s_check = shutil.which(self.sweep_exec) is not None
        return t_check and s_check

    def is_device_connected(self):
        """透過 hackrf_info 檢查連接"""
        try:
            result = subprocess.run(["hackrf_info"], capture_output=True, text=True)
            return "Found HackRF" in result.stdout
        except FileNotFoundError:
            return False

    def _start_process(self, cmd_args):
        """(內部方法) 啟動子進程"""
        if self.is_running():
            print("[Warning] 上一個任務尚未結束，正在強制停止...")
            self.stop()

        try:
            # 使用 Popen 啟動 (非阻塞)
            self.process = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            return True
        except FileNotFoundError as e:
            print(f"[Error] 找不到執行檔: {e.filename}，請確認已安裝 hackrf 套件。")
            return False
        except Exception as e:
            print(f"[Error] 啟動失敗: {e}")
            return False

    def start_tx(self, filename, freq_hz, sample_rate_hz=2600000, amp=False, tx_gain=0, repeat=False):
        """
        [hackrf_transfer] 定頻發射 (GPS模擬用這個)
        :param tx_gain: 0-47dB
        """
        if not os.path.exists(filename):
            print(f"[Error] 發射檔案不存在: {filename}")
            return False

        cmd = [
            self.transfer_exec,
            "-t", filename,
            "-f", str(int(freq_hz)),
            "-s", str(int(sample_rate_hz)),
            "-a", "1" if amp else "0",
            "-x", str(int(tx_gain)) # TX VGA Gain
        ]
        if repeat:
            cmd.append("-R")

        print(f"[*] 啟動 TX 發射: Freq={freq_hz}Hz, Gain={tx_gain}, File={filename}")
        return self._start_process(cmd)

    def start_rx(self, filename, freq_hz, sample_rate_hz=2600000, amp=False, lna_gain=16, vga_gain=20, num_samples=None):
        """
        [hackrf_transfer] 定頻接收 (錄製訊號)
        :param lna_gain: 0-40dB (8dB steps)
        :param vga_gain: 0-62dB (2dB steps) -> 對應文件中的 -g
        """
        cmd = [
            self.transfer_exec,
            "-r", filename,
            "-f", str(int(freq_hz)),
            "-s", str(int(sample_rate_hz)),
            "-a", "1" if amp else "0",
            "-l", str(int(lna_gain)),
            "-g", str(int(vga_gain)) # RX VGA Gain
        ]
        if num_samples is not None:
            cmd.extend(["-n", str(int(num_samples))])

        print(f"[*] 啟動 RX 接收: Freq={freq_hz}Hz, LNA={lna_gain}, VGA={vga_gain} -> {filename}")
        return self._start_process(cmd)

    def start_sweep(self, output_file, freq_min_mhz, freq_max_mhz, bin_width_hz=1000000, 
                    amp=False, lna_gain=16, vga_gain=20, one_shot=False, num_sweeps=None):
        """
        [hackrf_sweep] 頻譜掃描
        :param freq_min_mhz: 起始頻率 (MHz)
        :param freq_max_mhz: 結束頻率 (MHz)
        :param bin_width_hz: 解析度頻寬 (Hz)
        :param output_file: 儲存結果的 CSV 檔案路徑 (若為 None 則不會存檔，但通常建議存檔)
        """
        # 根據文件: -f freq_min:freq_max (單位 MHz)
        freq_range = f"{int(freq_min_mhz)}:{int(freq_max_mhz)}"
        
        cmd = [
            self.sweep_exec,
            "-f", freq_range,
            "-w", str(int(bin_width_hz)),
            "-a", "1" if amp else "0",
            "-l", str(int(lna_gain)),
            "-g", str(int(vga_gain))
        ]

        if output_file:
            cmd.extend(["-r", output_file])
        
        if one_shot:
            cmd.append("-1")
        
        if num_sweeps:
            cmd.extend(["-N", str(int(num_sweeps))])

        print(f"[*] 啟動 Sweep 掃描: Range={freq_range} MHz, Width={bin_width_hz}Hz -> {output_file}")
        return self._start_process(cmd)

    def is_running(self):
        if self.process is None: return False
        return self.process.poll() is None

    def stop(self):
        if self.is_running():
            print("[*] 正在停止 HackRF...")
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            print("[V] HackRF 已停止")

    def wait(self):
        """阻塞直到程式結束"""
        if self.is_running():
            self.process.wait()

if __name__ == "__main__":
    # === 使用範例 ===
    hackrf = HackRFCLI()

    if not hackrf.is_device_connected():
        print("警告: 未偵測到 HackRF")
