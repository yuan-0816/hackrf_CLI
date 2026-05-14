import json
import os
import copy

DEFAULT_CONFIG = {
    "hackrf": {
        "default_freq": 1575420000,
        "sample_rate": 2600000,
        "tx_gain": 47,
        "lna_gain": 16,
        "vga_gain": 20
    },
    "gps_sim": {
        "default_speed_mps": 5.0,
        "default_height": 100.0,
        "update_rate_hz": 10.0
    },
    "ephemeris": {
        "save_dir": "data/ephemeris",
        "max_files": 5
    },
    "presets": {}
}


class ConfigManager:
    def __init__(self, config_path=None):
        if config_path is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(root, "config", "config.json")

        self.config_path = config_path
        self._ensure_exists()
        self.config = self._load()

    # ── 內部 I/O ──────────────────────────────────────────

    def _ensure_exists(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        if not os.path.exists(self.config_path):
            self._write(DEFAULT_CONFIG)

    def _load(self) -> dict:
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 一般參數 ──────────────────────────────────────────

    def get(self, key_path: str, default=None):
        """
        用點記法讀取設定值
        e.g. cfg.get('hackrf.tx_gain')
        """
        keys = key_path.split(".")
        val = self.config
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val

    def set(self, key_path: str, value):
        """
        用點記法寫入設定值，自動轉型
        e.g. cfg.set('hackrf.tx_gain', 30)
        """
        # 嘗試自動轉型 (string → number)
        if isinstance(value, str):
            try:
                value = int(value) if "." not in value else float(value)
            except ValueError:
                pass  # 保留字串

        keys = key_path.split(".")
        d = self.config
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

        self._write(self.config)
        self.config = self._load()

    def reset(self):
        """恢復所有設定為預設值 (保留 presets)"""
        presets = self.config.get("presets", {})
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["presets"] = presets
        self._write(self.config)

    def show(self):
        """顯示目前設定 (不含 presets)"""
        display = {k: v for k, v in self.config.items() if k != "presets"}
        print(json.dumps(display, ensure_ascii=False, indent=2))

    # ── Preset ────────────────────────────────────────────

    def preset_list(self) -> dict:
        return self.config.get("presets", {})

    def preset_get(self, name: str) -> dict | None:
        return self.config.get("presets", {}).get(name)

    def preset_add(self, name: str, lat: float, lon: float, alt: float):
        self.config.setdefault("presets", {})[name] = {
            "lat": lat, "lon": lon, "alt": alt
        }
        self._write(self.config)
        self.config = self._load()

    def preset_delete(self, name: str) -> bool:
        if name in self.config.get("presets", {}):
            del self.config["presets"][name]
            self._write(self.config)
            self.config = self._load()
            return True
        return False