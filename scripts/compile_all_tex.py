import os
import io
import shutil
import base64
import requests
import subprocess
import pathlib
import json
from tqdm import tqdm
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ================================
# 1. Google Drive API & GAS Upload
# ================================

def get_drive_service():
    """環境変数からサービスアカウント情報を読み込んでDrive APIクライアントを作成"""
    creds_json_str = os.environ.get("GOOGLE_CREDENTIALS")
    
    if not creds_json_str:
        if os.path.exists("credentials.json"):
             creds = Credentials.from_service_account_file("credentials.json")
             return build("drive", "v3", credentials=creds)
        raise ValueError("環境変数 GOOGLE_CREDENTIALS が設定されていません。")

    try:
        creds_dict = json.loads(creds_json_str)
    except json.JSONDecodeError as e:
        print(f"JSON Decode Error: {e}")
        print(f"Content snippet: {creds_json_str[:10]}...")
        raise

    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def download_folder_recursive(service, folder_id, local_path, folder_map):
    """
    Driveの指定フォルダの中身を再帰的にダウンロードし、
    ローカルパスとフォルダIDの対応関係を folder_map に記録する
    """
    # ★重要: 絶対パスでマッピングを記録する
    abs_local_path = os.path.abspath(local_path)
    folder_map[abs_local_path] = folder_id

    if not os.path.exists(local_path):
        os.makedirs(local_path)

    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query, fields="files(id, name, mimeType)"
    ).execute()
    files = results.get("files", [])

    print(f"Downloading contents to: {local_path} (ID: {folder_id})")
    for file in tqdm(files, leave=False):
        file_id = file["id"]
        name = file["name"]
        mime_type = file["mimeType"]
        dest_path = os.path.join(local_path, name)

        if mime_type == "application/vnd.google-apps.folder":
            # 再帰呼び出し時にも folder_map を渡す
            download_folder_recursive(service, file_id, dest_path, folder_map)
        else:
            request = service.files().get_media(fileId=file_id)
            with io.FileIO(dest_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()

def upload_pdf_via_gas(local_path, new_name, parent_folder_id):
    """GAS Web API経由でPDFをアップロード"""
    gas_url = os.environ.get("GAS_UPLOAD_URL")
    token = os.environ.get("UPLOAD_TOKEN") 

    if not gas_url or not token:
        print("Error: Environment variables for upload are missing.")
        return

    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "token": token,
        "folderId": parent_folder_id,
        "fileName": new_name,
        "base64": b64,
        "mimeType": "application/pdf",
    }

    print(f"Uploading {new_name} to folder ID: {parent_folder_id} ...")
    try:
        res = requests.post(gas_url, json=payload, timeout=120)
        res.raise_for_status()
        print(f"GAS response: {res.json()}")
    except Exception as e:
        print(f"Failed to upload {new_name}: {e}")

# ================================
# 2. LaTeX Compile Logic
# ================================

def delete_auxiliary_files(tex_path: pathlib.Path):
    extensions = [".aux", ".log", ".dvi", ".toc", ".out"]
    for ext in extensions:
        file = tex_path.with_suffix(ext)
        if file.exists():
            file.unlink()

def get_tex_env():
    """emathフォルダへのパスを含む環境変数を作成する"""
    script_dir = pathlib.Path(__file__).parent.resolve()
    repo_root = script_dir.parent
    emath_dir = repo_root / "emath_tkp_ver1"
    
    env = os.environ.copy()
    
    if emath_dir.exists():
        print(f"Found emath directory: {emath_dir}")
        env["TEXINPUTS"] = f".:{emath_dir.resolve()}//:"
    else:
        print(f"Warning: emath directory not found at {emath_dir}")
        
    return env

def compile_tex_file(tex_path: pathlib.Path, tex_env: dict):
    """uplatex -> dvipdfmx でコンパイル"""
    abs_tex_path = tex_path.resolve()
    tex_dir = abs_tex_path.parent
    cwd_before = os.getcwd()
    
    try:
        os.chdir(tex_dir)
        
        tex_filename = abs_tex_path.name
        tex_stem = abs_tex_path.stem
        log_name = f"{tex_stem}_compile.log"

        print(f"Compiling: {tex_filename}")

        # uplatex
        with open(log_name, "w") as f_log:
            subprocess.check_call(
                ["uplatex", "-interaction=nonstopmode", tex_filename],
                stdout=f_log, stderr=subprocess.STDOUT,
                env=tex_env
            )
        
        # dvipdfmx
        with open(log_name, "a") as f_log:
            subprocess.check_call(
                ["dvipdfmx", tex_stem],
                stdout=f_log, stderr=subprocess.STDOUT,
                env=tex_env
            )

        print(f"Success: {tex_filename}")
        delete_auxiliary_files(pathlib.Path(tex_filename))
        return True

    except subprocess.CalledProcessError:
        print(f"Failed: {abs_tex_path.name}")
        # エラーログ表示
        if os.path.exists(log_name):
            print("================ [ERROR LOG START] ================")
            try:
                with open(log_name, "r", encoding="utf-8", errors="ignore") as f:
                    print(f.read())
            except: pass
            print("================ [ERROR LOG END] ================")
        return False
    except Exception as e:
        print(f"Unexpected Error on {abs_tex_path.name}: {e}")
        return False
    finally:
        os.chdir(cwd_before)

# ================================
# 3. Main Orchestration
# ================================

def main():
    input_folder_id = os.environ.get("INPUT_FOLDER_ID")
    # 出力先指定がなければ入力と同じにする(ただし今回は個別のフォルダIDを優先する)
    default_output_id = os.environ.get("OUTPUT_FOLDER_ID", input_folder_id)

    if not input_folder_id:
        print("Error: INPUT_FOLDER_ID is not set.")
        return

    work_dir = "./workspace"
    
    # Driveからダウンロード & フォルダマップ作成
    try:
        service = get_drive_service()
    except Exception as e:
        print(f"Auth Error: {e}")
        return

    # ★マップを初期化して渡す
    folder_map = {} 
    
    print(f"Start downloading from Folder ID: {input_folder_id}")
    download_folder_recursive(service, input_folder_id, work_dir, folder_map)

    # emath用の環境変数を準備
    tex_env = get_tex_env()

    found_tex = False
    for root, dirs, files in os.walk(work_dir):
        # 現在のディレクトリに対応するDriveフォルダIDを取得
        # マップには絶対パスで保存されているので、ここでも絶対パスに変換して検索
        abs_root = os.path.abspath(root)
        current_drive_id = folder_map.get(abs_root, default_output_id)

        for file in files:
            if file.endswith(".tex"):
                found_tex = True
                tex_path = pathlib.Path(root) / file
                
                # コンパイル実行
                if compile_tex_file(tex_path, tex_env):
                    pdf_name = tex_path.stem + ".pdf"
                    pdf_path = tex_path.parent / pdf_name
                    
                    if pdf_path.exists():
                        # ★ここで「元あったフォルダID」を指定してアップロード
                        upload_pdf_via_gas(pdf_path, pdf_name, current_drive_id)
                    else:
                        print(f"PDF not found for {file}")

    if not found_tex:
        print("No .tex files found.")

if __name__ == "__main__":
    main()
