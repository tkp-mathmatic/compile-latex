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
        # ローカルテスト用（もしあれば）
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

def download_folder_recursive(service, folder_id, local_path):
    """Driveの指定フォルダの中身を再帰的にダウンロード"""
    if not os.path.exists(local_path):
        os.makedirs(local_path)

    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query, fields="files(id, name, mimeType)"
    ).execute()
    files = results.get("files", [])

    print(f"Downloading contents to: {local_path}")
    for file in tqdm(files, leave=False):
        file_id = file["id"]
        name = file["name"]
        mime_type = file["mimeType"]
        dest_path = os.path.join(local_path, name)

        if mime_type == "application/vnd.google-apps.folder":
            download_folder_recursive(service, file_id, dest_path)
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

    print(f"Uploading {new_name} to GAS...")
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
    """
    emathフォルダへのパスを含む環境変数を作成する
    """
    # このスクリプト(compile_all_tex.py)がある場所: .../scripts/
    script_dir = pathlib.Path(__file__).parent.resolve()
    # リポジトリのルート: .../compile-latex/
    repo_root = script_dir.parent
    
    # emathフォルダのパス: .../compile-latex/emath_tkp_ver1
    emath_dir = repo_root / "emath_tkp_ver1"
    
    env = os.environ.copy()
    
    if emath_dir.exists():
        print(f"Found emath directory: {emath_dir}")
        # TEXINPUTS設定: . (カレント) : emath(再帰//) : システム標準
        # 末尾の : を忘れると標準ライブラリが読めなくなるので注意
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

        # uplatex 実行 (envを渡す)
        with open(log_name, "w") as f_log:
            subprocess.check_call(
                ["uplatex", "-interaction=nonstopmode", tex_filename],
                stdout=f_log, stderr=subprocess.STDOUT,
                env=tex_env  # ★ここが重要
            )
        
        # dvipdfmx 実行 (envを渡す)
        with open(log_name, "a") as f_log:
            subprocess.check_call(
                ["dvipdfmx", tex_stem],
                stdout=f_log, stderr=subprocess.STDOUT,
                env=tex_env  # ★ここも重要
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
            except:
                pass
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
    output_folder_id = os.environ.get("OUTPUT_FOLDER_ID", input_folder_id)

    if not input_folder_id:
        print("Error: INPUT_FOLDER_ID is not set.")
        return

    work_dir = "./workspace"
    
    # Driveからダウンロード
    try:
        service = get_drive_service()
    except Exception as e:
        print(f"Auth Error: {e}")
        return

    print(f"Start downloading from Folder ID: {input_folder_id}")
    download_folder_recursive(service, input_folder_id, work_dir)

    # emath用の環境変数を準備
    tex_env = get_tex_env()

    # .texファイルを探してコンパイル
    found_tex = False
    for root, dirs, files in os.walk(work_dir):
        for file in files:
            if file.endswith(".tex"):
                found_tex = True
                tex_path = pathlib.Path(root) / file
                
                # コンパイル実行 (tex_envを渡す)
                if compile_tex_file(tex_path, tex_env):
                    # 成功したらアップロード
                    pdf_name = tex_path.stem + ".pdf"
                    pdf_path = tex_path.parent / pdf_name
                    
                    if pdf_path.exists():
                        upload_pdf_via_gas(pdf_path, pdf_name, output_folder_id)
                    else:
                        print(f"PDF not found for {file}")

    if not found_tex:
        print("No .tex files found.")

if __name__ == "__main__":
    main()