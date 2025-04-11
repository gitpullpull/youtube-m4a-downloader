# /api/main.py
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import yt_dlp
import asyncio
import os
import re
import tempfile
import pathlib
import logging

# ロギング設定 (Vercelのログで確認しやすくするため)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# --- HTMLテンプレートの設定 ---
# __file__ は api/main.py を指すので、その親の親がプロジェクトルート
# プロジェクトルートにある templates ディレクトリを指定
try:
    templates_dir = pathlib.Path(__file__).parent.parent / "templates"
    if not templates_dir.is_dir():
        logger.error(f"Templates directory not found at: {templates_dir}")
        # ローカル開発時などの代替パス (必要に応じて調整)
        templates_dir = pathlib.Path("templates")
        if not templates_dir.is_dir():
            raise RuntimeError(f"Templates directory not found at expected locations.")

    templates = Jinja2Templates(directory=str(templates_dir))
    logger.info(f"Templates directory set to: {templates_dir}")
except Exception as e:
    logger.exception("Error setting up Jinja2Templates")
    # テンプレートが読み込めない場合、 FastAPIの起動自体は成功させ、
    # ルートアクセス時にエラーを返すようにする
    templates = None


# --- yt-dlpとダウンロード関連 ---
# yt-dlpオプション: ffmpeg不要のm4a (format 140等), 一時ファイルへ出力
# フォーマット'140'は多くの場合 m4a 128kbps (AAC) でffmpeg不要
YDL_OPTS_BASE = {
    'format': '140/bestaudio[ext=m4a]/bestaudio', # 140を優先、なければm4a、それもなければベストな音声
    'noprogress': True,
    'quiet': True,
    'logtostderr': False,
    'no_warnings': True,
    'outtmpl': '', # 後で設定
    'http_chunk_size': 10485760, # 10MBチャンク
    'verbose': True, # デバッグ時に有効化
    'logger': logger, # yt-dlpのログをFastAPIのロガーに出力する場合
    'ffmpeg_location': '/opt/homebrew/bin/ffmpeg' if os.path.exists('/opt/homebrew/bin/ffmpeg') else None # ffmpegがあれば使う (Vercelには基本ない)
}

def sanitize_filename(filename):
    """ファイル名として不適切な文字を除去または置換する"""
    # 不適切な文字を除去"?も加える"
    sanitized = re.sub(r'[\\/*:"<>|]', "", filename)
    # 長すぎるファイル名を切り詰める (例: 100文字)
    return sanitized[:100].strip() or "download" # 空白のみや空文字の場合のフォールバック
    #sanitized = filename
    #return sanitized

async def download_audio(url: str):
    """yt-dlpで指定されたURLからm4a音声をダウンロードし、一時ファイルのパスとファイル名を返す"""
    # 一時ディレクトリの存在確認 (Vercelでは /tmp が使えるはず)
    temp_dir = pathlib.Path(__file__).parent.parent.resolve() / "tmp"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = str(temp_dir)  # pathlib.Pathを文字列に変換
    except Exception as e:
        logger.warning(f"Failed to create temporary directory {temp_dir}: {e}")
        temp_dir = None  # tempfileモジュールに任せる
    
    if not os.path.exists(temp_dir) or not os.path.isdir(temp_dir):
         logger.warning(f"Temporary directory {temp_dir} not found or not a directory. Using default temp dir.")
         temp_dir = None # tempfileモジュールに任せる

    # 一時ファイルを作成 (Vercelの/tmpを使用推奨)
    try:
        # withステートメントで確実にファイルを閉じる
        with tempfile.NamedTemporaryFile(suffix='.m4a', prefix='ytaudio_', dir=temp_dir, delete=False) as temp_file:
            m4a_temp_path = temp_file.name
        logger.info(f"Created temporary file: {m4a_temp_path}")
    except Exception as e:
        logger.exception("Failed to create temporary file.")
        raise HTTPException(status_code=500, detail="Failed to create temporary storage for download.")

    opts = YDL_OPTS_BASE.copy()
    opts['outtmpl'] = m4a_temp_path # 出力先を一時ファイルに指定

    loop = asyncio.get_event_loop()
    downloaded_info = None
    try:
        # yt-dlpは同期的ライブラリなので、run_in_executorで非同期実行する
        logger.info(f"Starting download for URL: {url}")
        with yt_dlp.YoutubeDL(opts) as ydl:
            # まず動画情報を取得 (ファイル名決定のためにも使う)
            # extract_info(download=False) はネットワークアクセスが発生する
            logger.info(f"Extracting info for URL: {url}")
            info_dict = await loop.run_in_executor(
                None, lambda: ydl.extract_info(url, download=False)
            )
            video_title = info_dict.get('title', 'audio')
            video_id = info_dict.get('id', 'unknown_id')
            # 安全なファイル名を生成
            filename = sanitize_filename(f"{video_title}_{video_id}.m4a")
            logger.info(f"Determined filename: {filename}")

            # ダウンロードを実行
            # extract_info(download=True) は情報取得とダウンロードを両方行う
            # または ydl.download([url]) を使う
            logger.info(f"Downloading audio to: {m4a_temp_path}")
            # download=True を渡すことで、ダウンロードまで実行させる
            # すでに情報取得済みなので、再度リクエストされる可能性がある点に注意
            # ydl.download([url]) の方が効率的かもしれない
            # await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            await loop.run_in_executor(None, lambda: ydl.download([url]))


            # ファイルが存在し、サイズが0より大きいか確認
            if not os.path.exists(m4a_temp_path) or os.path.getsize(m4a_temp_path) == 0:
                 logger.error(f"Downloaded file not found or empty: {m4a_temp_path}")
                 # yt-dlpがエラーを出さずに空ファイルを作る場合がある
                 if os.path.exists(m4a_temp_path):
                     os.remove(m4a_temp_path)
                 raise yt_dlp.utils.DownloadError("Downloaded file is empty or missing after download attempt.")

            file_size = os.path.getsize(m4a_temp_path)
            logger.info(f"Audio downloaded successfully to: {m4a_temp_path}, Size: {file_size} bytes")
            return m4a_temp_path, filename, file_size

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp Download Error for {url}: {e}", exc_info=True)
        # エラー時には一時ファイルを削除しようとする
        if os.path.exists(m4a_temp_path):
            try:
                os.remove(m4a_temp_path)
                logger.info(f"Removed temporary file due to download error: {m4a_temp_path}")
            except OSError as remove_err:
                logger.error(f"Error removing temporary file {m4a_temp_path} after download error: {remove_err}")

        # エラーメッセージに基づいて適切なHTTPステータスコードを返す
        err_str = str(e).lower()
        if "video unavailable" in err_str or "private video" in err_str or "video not found" in err_str:
             raise HTTPException(status_code=404, detail="Video not found or unavailable.")
        elif "ffmpeg" in err_str and YDL_OPTS_BASE.get('ffmpeg_location') is None:
             raise HTTPException(status_code=500, detail="The requested format requires ffmpeg, which is not available on this server.")
        elif "unsupported url" in err_str:
             raise HTTPException(status_code=400, detail="Invalid or unsupported URL.")
        elif "no space left on device" in err_str:
             raise HTTPException(status_code=507, detail="Insufficient storage space on server.")
        elif "copyright" in err_str:
              raise HTTPException(status_code=451, detail="Content unavailable due to copyright restrictions.")
        else:
             # その他のyt-dlpエラー
             raise HTTPException(status_code=500, detail=f"Failed to download audio. Error: {e}")

    except Exception as e:
        logger.exception(f"Unexpected Error during download for {url}: {e}")
        if os.path.exists(m4a_temp_path):
             try:
                os.remove(m4a_temp_path)
                logger.info(f"Removed temporary file due to unexpected error: {m4a_temp_path}")
             except OSError as remove_err:
                logger.error(f"Error removing temporary file {m4a_temp_path} after unexpected error: {remove_err}")
        raise HTTPException(status_code=500, detail=f"An unexpected server error occurred: {str(e)}")


async def file_streamer(file_path: str):
    """ファイルをチャンクで読み込み、読み込み後にファイルを削除する非同期ジェネレータ"""
    try:
        # StreamingResponse がよしなにやってくれるはずだが念のため
        # logger.info(f"Starting to stream file: {file_path}")
        with open(file_path, mode="rb") as file_like:
            while chunk := file_like.read(65536): # 64KBずつ読み込む (調整可能)
                yield chunk
        # logger.info(f"Finished streaming file: {file_path}")
    except Exception as e:
        logger.error(f"Error streaming file {file_path}: {e}", exc_info=True)
        # ストリーミング中のエラーはクライアント側で切断として検知されることが多い
    finally:
        # ストリーミング完了後（またはエラー時）にファイルを確実に削除
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Removed temporary file after streaming: {file_path}")
        except OSError as e:
            logger.error(f"Error removing temporary file {file_path} after streaming: {e}", exc_info=True)


# --- APIエンドポイント ---
@app.get("/api/download")
async def download_endpoint(url: str = Query(..., min_length=10, description="YouTube Video URL")):
    # より厳密なURL検証 (オプション)
    if not re.match(r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+(&\S*)?$", url):
        logger.warning(f"Invalid YouTube URL format received: {url}")
        raise HTTPException(status_code=400, detail="Invalid YouTube URL format. Please use a valid video URL (e.g., https://www.youtube.com/watch?v=...).")

    try:
        # ダウンロードを実行し、一時ファイルのパス、ファイル名、サイズを取得
        temp_file_path, download_filename, file_size = await download_audio(url)

        # ストリーミングレスポンスを返す
        headers = {
            'Content-Disposition': f'attachment; filename="{download_filename}"',
            'Content-Type': 'audio/m4a',
            'Content-Length': str(file_size) # Content-Length を追加
        }
        return StreamingResponse(
            file_streamer(temp_file_path),
            # media_type="audio/m4a", # Headersで指定したので不要かも
            headers=headers
        )

    except HTTPException as e:
        # download_audio内で発生したHTTPExceptionをそのまま返す
        # エラーログはdownload_audio内で記録済み
        raise e
    except Exception as e:
        # 予期せぬエラー (download_audio以外で発生した場合)
        logger.exception(f"Unexpected error in /api/download endpoint for {url}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected internal server error occurred.")


# --- ルートパス ("/") でHTMLを返す ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """ルートURLアクセス時に index.html を表示する"""
    if templates is None:
         logger.error("Templates not loaded, cannot serve HTML.")
         return HTMLResponse("Server configuration error: Could not load page template.", status_code=500)
    logger.info(f"Serving index.html for request path: {request.url.path}")
    return templates.TemplateResponse("index.html", {"request": request})

# Vercelでは、uvicornを直接起動する必要はありません。
# FastAPIインスタンス `app` が検出され、ASGIサーバーで実行されます。