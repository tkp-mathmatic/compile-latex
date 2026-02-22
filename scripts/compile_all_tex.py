import os
import io
import shutil
import base64
import requests
import subprocess
import pathlib
import json
import traceback
from tqdm import tqdm
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ================================
# 1. Google Drive API & GAS Upload
# ================================

def get_drive_service():
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
        raise

    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def download_folder_recursive(service, folder_id, local_path, folder_map):
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
            download_folder_recursive(service, file_id, dest_path, folder_map)
        else:
            request = service.files().get_media(fileId=file_id)
            with io.FileIO(dest_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()

# ★変更: PDF以外(ログ)もアップロードできるように汎用化
def upload_file_via_gas(local_path, new_name, parent_folder_id, mime_type="application/pdf"):
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
        "mimeType": mime_type, # ★引数で受け取る
    }

    print(f"Uploading {new_name} to folder ID: {parent_folder_id} ...")
    try:
        res = requests.post(gas_url, json=payload, timeout=120)
        res.raise_for_status()
        print(f"GAS response: {res.json()}")
    except Exception as e:
        print(f"Failed to upload {new_name}: {e}")

def send_notification_via_gas(status, folder_id, pdf_count=0):
    gas_url = os.environ.get("GAS_UPLOAD_URL")
    token = os.environ.get("UPLOAD_TOKEN")
    
    slack_channel = os.environ.get("SLACK_CHANNEL")
    slack_mention_users = os.environ.get("SLACK_MENTION_USERS")
    repo = os.environ.get("GITHUB_REPO", "unknown")
    run_id = os.environ.get("GITHUB_RUN_ID", "unknown")

    if not gas_url or not token:
        print("Skipping notification: GAS_UPLOAD_URL or UPLOAD_TOKEN missing.")
        return

    payload = {
        "token": token,
        "status": status,
        "folder_id": folder_id,
        "slack_channel": slack_channel,
        "mention_users": slack_mention_users,
        "pdf_count": pdf_count,
        "repo": repo,
        "run_id": run_id
    }

    print(f"Sending {status} notification to GAS...")
    try:
        requests.post(gas_url, json=payload, timeout=30)
    except Exception as e:
        print(f"Failed to send notification: {e}")

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
    script_dir = pathlib.Path(__file__).parent.resolve()
    repo_root = script_dir.parent
    emath_dir = repo_root / "emath_tkp_ver1"
    
    env = os.environ.copy()
    
    if emath_dir.exists():
        env["TEXINPUTS"] = f".:{emath_dir.resolve()}//:"
    return env

def compile_tex_file(tex_path: pathlib.Path, tex_env: dict):
    abs_tex_path = tex_path.resolve()
    tex_dir = abs_tex_path.parent
    cwd_before = os.getcwd()
    
    try:
        os.chdir(tex_dir)
        tex_filename = abs_tex_path.name
        tex_stem = abs_tex_path.stem
        log_name = f"{tex_stem}_compile.log"

        print(f"Compiling: {tex_filename}")

        # ★変更: uplatex を2回実行して相互参照を解決する
        with open(log_name, "w") as f_log:
            subprocess.check_call(
                ["uplatex", "-interaction=nonstopmode", tex_filename],
                stdout=f_log, stderr=subprocess.STDOUT, env=tex_env
            )
        
        with open(log_name, "a") as f_log:
            subprocess.check_call(
                ["uplatex", "-interaction=nonstopmode", tex_filename],
                stdout=f_log, stderr=subprocess.STDOUT, env=tex_env
            )
        
        # dvipdfmx
        with open(log_name, "a") as f_log:
            subprocess.check_call(
                ["dvipdfmx", tex_stem],
                stdout=f_log, stderr=subprocess.STDOUT, env=tex_env
            )

        print(f"Success: {tex_filename}")
        delete_auxiliary_files(pathlib.Path(tex_filename))
        return True

    except subprocess.CalledProcessError:
        print(f"Failed: {abs_tex_path.name}")
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
    default_output_id = os.environ.get("OUTPUT_FOLDER_ID", input_folder_id)

    if not input_folder_id:
        print("Error: INPUT_FOLDER_ID is not set.")
        return

    overall_status = "success"
    pdf_count = 0

    try:
        work_dir = "./workspace"
        service = get_drive_service()
        folder_map = {} 
        
        print(f"Start downloading from Folder ID: {input_folder_id}")
        download_folder_recursive(service, input_folder_id, work_dir, folder_map)

        tex_env = get_tex_env()
        found_tex = False

        for root, dirs, files in os.walk(work_dir):
            abs_root = os.path.abspath(root)
            current_drive_id = folder_map.get(abs_root, default_output_id)

            for file in files:
                if file.endswith(".tex"):
                    found_tex = True
                    tex_path = pathlib.Path(root) / file
                    
                    # コンパイル実行
                    if compile_tex_file(tex_path, tex_env):
                        # ★成功時: PDFをアップロード
                        pdf_name = tex_path.stem + ".pdf"
                        pdf_path = tex_path.parent / pdf_name
                        
                        if pdf_path.exists():
                            upload_file_via_gas(pdf_path, pdf_name, current_drive_id, "application/pdf")
                            pdf_count += 1
                        else:
                            print(f"PDF not found for {file}")
                            overall_status = "failure"
                    else:
                        overall_status = "failure"
                        
                        # ★失敗時: ログファイルを Drive にアップロード
                        log_name = tex_path.stem + "_compile.log"
                        log_path = tex_path.parent / log_name
                        if log_path.exists():
                            print(f"Uploading ERROR LOG for {file}")
                            upload_file_via_gas(log_path, log_name, current_drive_id, "text/plain")

        if not found_tex:
            print("No .tex files found.")
    
    except Exception:
        print("Critial Error occurred in main process:")
        traceback.print_exc()
        overall_status = "failure"
    
    finally:
        send_notification_via_gas(overall_status, input_folder_id, pdf_count)

if __name__ == "__main__":
    main()
