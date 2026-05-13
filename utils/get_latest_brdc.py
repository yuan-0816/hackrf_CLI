from dotenv import load_dotenv
import requests
import datetime
import os
import gzip
import shutil
from requests.auth import HTTPBasicAuth

from utils.tool import get_project_root



# ================= 設定區 =================
# 取得專案根目錄
PROJECT_ROOT = get_project_root()
# 預設存檔路徑
DEFAULT_SAVE_DIR = os.path.join(PROJECT_ROOT, "data", "ephemeris")
# =========================================


def check_newst_brdc()-> bool:
    """
    檢查是否已有最新的星歷檔案
    parameter: None
    return: bool
    """

    url, filename = get_brdc_url()
    local_path = os.path.join(DEFAULT_SAVE_DIR, filename.replace(".gz", ""))
    
    if os.path.exists(local_path):
        print(f"已存在最新的星歷檔案: {local_path}")
        return True
    return False


def _load_credentials()-> tuple:
    """
    內部函數：載入並檢查帳號密碼
    parameter: None
    return: tuple (user, password) or (None, None) if not found
    """
    load_dotenv()
    user = os.getenv("NASA_USER")
    password = os.getenv("NASA_PASS")
    
    if not user or not password:
        # 這裡不直接報錯，而是回傳 None，讓呼叫者決定如何處理
        return None, None
    return user, password


def get_brdc_url()-> tuple:
    """
    計算今天的日期並生成 CDDIS 下載連結
    return: tuple (url, filename)
    """
    now = datetime.datetime.utcnow()
    year = now.year
    doy = now.timetuple().tm_yday
    yy = str(year)[-2:]
    doy_str = f"{doy:03d}"
    
    filename = f"brdc{doy_str}0.{yy}n.gz"
    url = f"https://cddis.nasa.gov/archive/gnss/data/daily/{year}/brdc/{filename}"
    
    return url, filename


def cleanup_old_files(directory, limit=5)-> None:
    """
    檢查並刪除超過限制數量的舊檔案
    parameter:
        directory (str): 目錄路徑
        limit (int): 保留的檔案數量上限
    return: None
    """
    if not os.path.exists(directory):
        return

    files = [os.path.join(directory, f) for f in os.listdir(directory)]
    files = [f for f in files if os.path.isfile(f)]
    
    if len(files) <= limit:
        return
        
    files.sort(key=os.path.getmtime)
    num_to_remove = len(files) - limit
    
    print(f"[清理機制] 刪除 {num_to_remove} 個舊檔案...")
    for i in range(num_to_remove):
        try:
            os.remove(files[i])
        except Exception:
            pass


def download_file(url, filename, save_dir, user, password)-> str:
    """
    下載星歷檔案並儲存到指定目錄
    parameter:
        url (str): 下載連結
        filename (str): 檔案名稱
        save_dir (str): 儲存目錄
        user (str): 帳號
        password (str): 密碼
    """
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
        
    local_path = os.path.join(save_dir, filename)
    
    if os.path.exists(local_path):
        print(f"檔案已存在，跳過下載: {local_path}")
        return local_path
    
    # 檢查本地是否已經有解壓縮後的版本 (避免重複下載)
    unzipped_path = local_path.replace(".gz", "")
    if os.path.exists(unzipped_path):
        print(f"已解壓檔案已存在，跳過下載: {unzipped_path}")
        return None # 回傳 None 代表不需要後續解壓縮

    print(f"正在從 {url} 下載...")
    
    try:
        with requests.Session() as session:
            session.auth = (user, password)
            r1 = session.request('get', url)
            r = session.get(r1.url, auth=(user, password), stream=True)
            
            if r.status_code == 200:
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
                print(f"下載成功: {local_path}")
                return local_path
            else:
                print(f"下載失敗 (HTTP {r.status_code})")
                return None
    except Exception as e:
        print(f"連線錯誤: {e}")
        return None

def uncompress_file(gz_path):
    """解壓縮 .gz 檔案"""
    if not gz_path:
        return None
    
    out_path = gz_path.replace(".gz", "")
    print(f"正在解壓縮到: {out_path}")
    
    try:
        with gzip.open(gz_path, 'rb') as f_in:
            with open(out_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        os.remove(gz_path)
        return out_path
    except Exception as e:
        print(f"解壓縮失敗: {e}")
        return None





# ================= 對外介面 (API) =================

def fetch_latest_ephemeris(save_dir=DEFAULT_SAVE_DIR, cleanup=True)-> str|None:
    """
    主要功能函數：自動下載、解壓並回傳星歷檔案路徑。
    其他程式只需呼叫此函數即可。
    parameter:
        save_dir (str): 儲存目錄
        cleanup (bool): 是否啟用清理舊檔機制
    return: str (檔案路徑) or None (失敗)
    """
    # 1. 檢查帳密
    user, password = _load_credentials()
    if not user or not password:
        print("錯誤：環境變數中找不到 NASA 帳號密碼")
        return None

    # 2. 取得連結
    url, filename = get_brdc_url()
    
    # 3. 下載
    gz_file = download_file(url, filename, save_dir, user, password)
    
    # 4. 如果下載成功 (或是需要解壓縮)
    final_file = uncompress_file(gz_file)
    
    # 如果剛剛回傳 None，可能是因為檔案已經存在並解壓過了，我們嘗試直接回傳路徑
    if not final_file:
        expected_path = os.path.join(save_dir, filename.replace(".gz", ""))
        if os.path.exists(expected_path):
            final_file = expected_path
    
    # 5. 清理舊檔
    if cleanup and final_file:
        cleanup_old_files(save_dir)
        
    return final_file

if __name__ == "__main__":
    # check_newst_brdc()
    # 當直接執行此檔案時，跑這段
    path = fetch_latest_ephemeris()
    if path:
        print(f"\n[完成] 星歷檔案位於: {path}")
    else:
        print("\n[失敗] 無法取得星歷檔案")