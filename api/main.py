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

#vercel環境か確認
def is_vercel_environment():
    return os.environ.get("VERCEL") == "1" or bool(os.environ.get("VERCEL_URL"))

#cookieの取得
#vercel環境か否かによってパスを変更
def get_cookie_path():
    if is_vercel_environment():
        return '/var/task/cookie.txt'
    else:
        return 'cookie.txt'

cookie_path = get_cookie_path()

if not os.path.exists(cookie_path):
    logger.warning(f"Cookie file not found at: {cookie_path}")

#FastAPI
app = FastAPI()

# --- HTMLテンプレートの設定 ---
# __file__ は api/main.py を指すので、その親の親がプロジェクトルート
# プロジェクトルートにある templates ディレクトリを指定
try:
    templates_dir = pathlib.Path(__file__).parent.parent / "templates"
    if not templates_dir.is_dir():
        logger.warning(f"Templates directory not found at: {templates_dir}")
        # ローカル開発時の代替パス
        templates_dir = pathlib.Path("templates")
        if not templates_dir.is_dir():
            raise RuntimeError("Templates directory not found at expected locations.")
    templates = Jinja2Templates(directory=str(templates_dir))
    logger.info(f"Templates directory set to: {templates_dir}")
except Exception as e:
    logger.exception("Error setting up Jinja2Templates")
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
    'ffmpeg_location': None,
    'cookiefile': cookie_path,
    'writethumbnail': True, # サムネイルを書き出す
    'postprocessors': [{
        'key': 'EmbedThumbnail',
        'already_have_thumbnail': False,
    }],
}

def sanitize_filename(filename):
    """ファイル名として不適切な文字を除去または置換する"""
    # 不適切な文字を除去"?も加える"
    sanitized = re.sub(r'[\\/*:"<>|]', "", filename)
    # 長すぎるファイル名を切り詰める (例: 100文字)
    return sanitized[:100].strip() or "download" # 空白のみや空文字の場合のフォールバック
    #sanitized = filename
    #return sanitized

async def download_audio(url: str, embed_thumbnail: bool = False): # embed_thumbnail パラメータを追加
    temp_dir = pathlib.Path(__file__).parent.parent / "tmp"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_dir_str = str(temp_dir)
    except Exception as e:
        logger.error(f"Failed to create temporary directory {temp_dir}: {e}")
        temp_dir_str = None  # tempfileのデフォルトを使う

    # ファイル名だけを生成（ファイルは作らない）
    import uuid
    unique_id = uuid.uuid4().hex
    outtmpl_base = os.path.join(temp_dir_str or tempfile.gettempdir(), f"ytaudio_{unique_id}")
    opts = YDL_OPTS_BASE.copy()
    opts['outtmpl'] = outtmpl_base + ".%(ext)s"

    if not embed_thumbnail: # サムネイルを埋め込まない場合
        opts['postprocessors'] = []
        opts['writethumbnail'] = False

    loop = asyncio.get_event_loop()
    actual_m4a_path = outtmpl_base + ".m4a" # Define actual_m4a_path here

    try:
        logger.info(f"Starting download for URL: {url}")
        with yt_dlp.YoutubeDL(opts) as ydl:
            logger.info(f"Extracting info for URL: {url}")
            info_dict = await loop.run_in_executor(
                None, lambda: ydl.extract_info(url, download=False)
            )
            video_title = info_dict.get('title', 'audio')
            filename = sanitize_filename(f"{video_title}.m4a")
            logger.info(f"Determined filename: {filename}")

            logger.info(f"Downloading audio to: {outtmpl_base}.m4a (yt-dlp will set ext)")
            await loop.run_in_executor(None, lambda: ydl.download([url]))

            if not os.path.exists(actual_m4a_path) or os.path.getsize(actual_m4a_path) == 0:
                logger.error(f"Downloaded file not found or empty: {actual_m4a_path}")
                if os.path.exists(actual_m4a_path):
                    os.remove(actual_m4a_path)
                raise yt_dlp.utils.DownloadError("Downloaded file is empty or missing after download attempt.")

            file_size = os.path.getsize(actual_m4a_path)
            logger.info(f"Audio downloaded successfully to: {actual_m4a_path}, Size: {file_size} bytes")
            return actual_m4a_path, filename, file_size

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp Download Error for {url}: {e}", exc_info=True)
        # Attempt to remove the potentially partially downloaded file
        if os.path.exists(actual_m4a_path):
            try:
                os.remove(actual_m4a_path)
                logger.info(f"Removed temporary file due to download error: {actual_m4a_path}")
            except OSError as remove_err:
                logger.error(f"Error removing temporary file {actual_m4a_path} after download error: {remove_err}")
        if "video unavailable" in str(e).lower():
            raise HTTPException(status_code=404, detail="Video not found or unavailable.")
        elif "ffmpeg" in str(e).lower():
            raise HTTPException(status_code=500, detail="FFmpeg is required but not available.")
        else:
            raise HTTPException(status_code=500, detail=f"Download failed: {e}")
    except yt_dlp.utils.ExtractorError as e:
        logger.error(f"yt-dlp Download Error for {url}: {e}", exc_info=True)
        # エラー時には一時ファイルを削除しようとする
        if os.path.exists(actual_m4a_path): # Use actual_m4a_path
            try:
                os.remove(actual_m4a_path) # Use actual_m4a_path
                logger.info(f"Removed temporary file due to download error: {actual_m4a_path}") # Use actual_m4a_path
            except OSError as remove_err:
                logger.error(f"Error removing temporary file {actual_m4a_path} after download error: {remove_err}") # Use actual_m4a_path

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
        if os.path.exists(actual_m4a_path): # Use actual_m4a_path
             try:
                os.remove(actual_m4a_path) # Use actual_m4a_path
                logger.info(f"Removed temporary file due to unexpected error: {actual_m4a_path}") # Use actual_m4a_path
             except OSError as remove_err:
                logger.error(f"Error removing temporary file {actual_m4a_path} after unexpected error: {remove_err}") # Use actual_m4a_path
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
async def download_endpoint(url: str = Query(..., min_length=10, description="YouTube Video URL"), embed_thumbnail: bool = Query(False, description="Embed thumbnail into the audio file")): # embed_thumbnail パラメータを追加
    #より厳密なURL検証
    if not re.match(r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+(&\S*)?$", url):
        logger.warning(f"Invalid YouTube URL format received: {url}")
        raise HTTPException(status_code=400, detail="Invalid YouTube URL format. Please use a valid video URL (e.g., https://www.youtube.com/watch?v=...).")

    try:
        # ダウンロードを実行し、一時ファイルのパス、ファイル名、サイズを取得
        temp_file_path, download_filename, file_size = await download_audio(url, embed_thumbnail) # embed_thumbnail を渡す

        # ストリーミングレスポンスを返す
        import urllib.parse
        quoted_filename = urllib.parse.quote(download_filename)
        headers = {
            # 日本語ファイル名対応: ASCII用filenameとUTF-8用filename*を両方指定
            'Content-Disposition': f'attachment; filename="{quoted_filename}"; filename*=UTF-8''{quoted_filename}',
            'Content-Type': 'audio/m4a',
            'Content-Length': str(file_size)
        }
        return StreamingResponse(
            file_streamer(temp_file_path),
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