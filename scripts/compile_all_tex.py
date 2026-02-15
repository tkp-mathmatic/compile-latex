import os
import io
import shutil
import base64
import requests
import subprocess
import pathlib
from tqdm import tqdm
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ================================
# 1. Google Drive API & GAS Upload
# ================================

def get_drive_service():
    """サービスアカウントでDrive APIクライアントを作成"""
    credentials_file = "credentials.json"
    # GitHub Actions側で作成されるファイルを読み込む
    creds = Credentials.from_service_account_file(
        credentials_file,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def download_folder_recursive(service, folder_id, local_path):
    """
    Driveの指定フォルダの中身（サブフォルダ含む）を再帰的にダウンロードする
    """
    if not os.path.exists(local_path):
        os.makedirs(local_path)

    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query, fields="files(id, name, mimeType)"
    ).execute()
    files = results.get("files", [])

    print(f"Downloading folder contents: {local_path}")
    for file in tqdm(files, leave=False):
        file_id = file["id"]
        name = file["name"]
        mime_type = file["mimeType"]
        dest_path = os.path.join(local_path, name)

        if mime_type == "application/vnd.google-apps.folder":
            # フォルダなら再帰呼び出し
            download_folder_recursive(service, file_id, dest_path)
        else:
            # ファイルならダウンロード
            request = service.files().get_media(fileId=file_id)
            with io.FileIO(dest_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()

def upload_pdf_via_gas(local_path, new_name, parent_folder_id):
    """GAS Web API経由でPDFをアップロード"""
    gas_url = os.environ.get("GAS_UPLOAD_URL")
    token = os.environ.get("UPLOAD_TOKEN") # YAMLで設定した名前

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

def compile_tex_file(tex_path: pathlib.Path):
    """uplatex -> dvipdfmx でコンパイル"""
    tex_dir = tex_path.parent
    cwd_before = os.getcwd()
    os.chdir(tex_dir)
    
    log_path = tex_dir / f"{tex_path.stem}_compile.log"

    print(f"Compiling: {tex_path.name}")
    try:
        # 1. uplatex
        subprocess.check_call(
            ["uplatex", "-interaction=nonstopmode", tex_path.name],
            stdout=open(log_path, "w"), stderr=subprocess.STDOUT
        )
        # 2. dvipdfmx
        subprocess.check_call(
            ["dvipdfmx", tex_path.stem],
            stdout=open(log_path, "a"), stderr=subprocess.STDOUT
        )
        print(f"Success: {tex_path.name}")
        delete_auxiliary_files(tex_path)
        return True
    except subprocess.CalledProcessError:
        print(f"Failed: {tex_path.name}. Check log: {log_path}")
        return False
    finally:
        os.chdir(cwd_before)

# ================================
# 3. Main Orchestration
# ================================

def main():
    # 環境変数から設定を取得
    input_folder_id = os.environ.get("INPUT_FOLDER_ID")
    output_folder_id = os.environ.get("OUTPUT_FOLDER_ID", input_folder_id)

    if not input_folder_id:
        print("Error: INPUT_FOLDER_ID is not set.")
        return

    work_dir = "./workspace"
    
    # 1. Driveからソースコード一式をダウンロード
    service = get_drive_service()
    print(f"Start downloading from Folder ID: {input_folder_id}")
    download_folder_recursive(service, input_folder_id, work_dir)

    # 2. .texファイルを探してコンパイル
    found_tex = False
    for root, dirs, files in os.walk(work_dir):
        for file in files:
            if file.endswith(".tex"):
                found_tex = True
                tex_path = pathlib.Path(root) / file
                
                # コンパイル実行
                if compile_tex_file(tex_path):
                    # 成功したらPDFを探してアップロード
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