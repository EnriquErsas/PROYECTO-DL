import os
import socket
import shutil
import uuid
import re
import threading
import requests
from pathlib import Path
from typing import List, Optional, Tuple
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp
import tempfile

app = FastAPI()

# Directorios
BASE_DIR = Path(__file__).resolve().parent
# En Hugging Face/Docker, /tmp es siempre escribible. En Windows, usar carpeta local.
if os.name == 'nt':
    DOWNLOAD_DIR = BASE_DIR / "downloads"
else:
    DOWNLOAD_DIR = Path("/tmp/downloads")
TEMPLATE_DIR = BASE_DIR / "templates"

# Asegurar directorios
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# --- SISTEMA DE COOKIES PARA YOUTUBE ---
# Prioridad 1: Archivo COOKIES.txt en la carpeta del proyecto (subido directamente al repo)
# Prioridad 2: Variable de entorno YOUTUBE_COOKIES (para Railway/HF secrets)
YOUTUBE_COOKIES_FILE = None

_local_cookies = BASE_DIR / "COOKIES.txt"
if _local_cookies.exists():
    YOUTUBE_COOKIES_FILE = str(_local_cookies)
    print(f"[INFO] Cookies cargadas desde archivo local: {YOUTUBE_COOKIES_FILE}")
else:
    _cookies_content = os.environ.get('YOUTUBE_COOKIES', '')
    if _cookies_content.strip():
        try:
            _tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
            _tmp.write(_cookies_content)
            _tmp.close()
            YOUTUBE_COOKIES_FILE = _tmp.name
            print(f"[INFO] Cookies cargadas desde variable de entorno -> {YOUTUBE_COOKIES_FILE}")
        except Exception as _e:
            print(f"[WARN] No se pudo escribir cookies.txt desde env: {_e}")
    else:
        print("[WARN] Sin cookies - YouTube puede bloquear en IPs de servidor.")

# --- CONFIGURACIÓN DE VIDEO EXTRACTOR ---

# Headers para emulación de navegador Chrome
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9,es;q=0.8',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    # 'Referer' se añadirá dinámicamente según la URL
}

class VideoExtractor:
    def __init__(self, url: str):
        self.original_url = url
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.headers.update({'Referer': self.get_base_url(url)})

    def get_base_url(self, url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def resolve_iframe(self, html_content: str) -> Optional[str]:
        """Busca iframes de video y devuelve el src si es relevante."""
        # Regex para buscar iframes comunes de video
        iframe_pattern = re.compile(
            r'<iframe[^>]+src=["\'](http[s]?://(?:www\.)?(?:youtube\.com|player\.vimeo\.com|dailymotion\.com|ok\.ru|sb\w+\.com)[^"\']+)["\']',
            re.IGNORECASE
        )
        match = iframe_pattern.search(html_content)
        if match:
            return match.group(1)
        return None

    def find_m3u8(self, html_content: str) -> Optional[str]:
        """Busca enlaces directos a manifiestos HLS (.m3u8)."""
        # Patrón para encontrar URLs que terminan en .m3u8 dentro de comillas o JSON
        m3u8_pattern = re.compile(r'["\'](http[s]?://[^"\']+\.m3u8(?:[^"\']*)?)["\']', re.IGNORECASE)
        match = m3u8_pattern.search(html_content)
        if match:
            return match.group(1)
        return None

    def aggressive_resolve(self) -> str:
        """Intenta resolver la URL real del video, saltando iframes y protecciones básicas."""
        print(f"Analizando URL base: {self.original_url}")
        
        try:
            # 1. Petición inicial (imitando navegador)
            response = self.session.get(self.original_url, timeout=10, allow_redirects=True)
            if response.status_code == 403:
                print("Detectado 403 Forbidden. Intentando bypass simple...")
                # A veces ayuda refrescar la sesión o headers
                self.session.headers.update({'Referer': self.original_url})
                response = self.session.get(self.original_url, timeout=10)

            html = response.text

            # 2. Buscar Iframes (Prioridad 1: Si hay un player embebido, es mejor ir a la fuente)
            iframe_src = self.resolve_iframe(html)
            if iframe_src:
                print(f"Iframe detectado saltando a: {iframe_src}")
                # Podríamos hacer recursividad aquí si fuera necesario
                return iframe_src

            # 3. Buscar m3u8 directo (Prioridad 2)
            m3u8_url = self.find_m3u8(html)
            if m3u8_url:
                print(f"Manifiesto HLS detectado: {m3u8_url}")
                # Limpiar: quitar slash escapado, backslashes finales y espacios
                clean_url = m3u8_url.replace(r'\/', '/').replace('\\', '').strip()
                return clean_url

        except Exception as e:
            print(f"Error en resolución agresiva: {e}")
        
        # Si no encontramos nada especial, devolvemos la URL original para que yt-dlp se encargue
        return self.original_url

# --- FIN CONFIGURACIÓN EXTRACTOR ---


# Función de limpieza
def cleanup_file(path: Path):
    try:
        if path.exists():
            os.remove(path)
            print(f"Archivo eliminado: {path}")
    except Exception as e:
        print(f"Error al eliminar archivo {path}: {e}")

def format_size(size_bytes):
    if size_bytes is None:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/analyze")
def analyze_video(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="URL requerida")

    try:
        # 1. Resolución Agresiva
        extractor = VideoExtractor(url)
        target_url = extractor.aggressive_resolve() or url
        
        print(f"URL final a analizar: {target_url}")

        # Base de opciones - SOLO opciones confiables, sin experimentales
        base_ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'geo_bypass': True,
            'socket_timeout': 30,
        }

        # Lista de estrategias a intentar en orden de preferencia
        strategies = []

        if YOUTUBE_COOKIES_FILE:
            # Estrategia 1: tv_embedded + cookies + check_formats=False (funciona para la mayoría)
            strategies.append({
                **base_ydl_opts,
                'ignoreerrors': True,
                'check_formats': False,
                'cookiefile': YOUTUBE_COOKIES_FILE,
                'extractor_args': {'youtube': {'player_client': ['tv_embedded']}},
            })
            # Estrategia 2: ios + cookies (cliente app iOS, diferente handshake)
            strategies.append({
                **base_ydl_opts,
                'ignoreerrors': True,
                'check_formats': False,
                'cookiefile': YOUTUBE_COOKIES_FILE,
                'extractor_args': {'youtube': {'player_client': ['ios']}},
            })
            # Estrategia 3: android + cookies
            strategies.append({
                **base_ydl_opts,
                'ignoreerrors': True,
                'check_formats': False,
                'cookiefile': YOUTUBE_COOKIES_FILE,
                'extractor_args': {'youtube': {'player_client': ['android']}},
            })
            # Estrategia 4: web + cookies sin restricciones adicionales
            strategies.append({
                **base_ydl_opts,
                'ignoreerrors': True,
                'check_formats': False,
                'cookiefile': YOUTUBE_COOKIES_FILE,
            })

        # Estrategia final: sin cookies (funciona si el video es público y la IP no está flaggeada)
        strategies.append({
            **base_ydl_opts,
            'ignoreerrors': False,
            'check_formats': False,
        })

        info = None
        success_strategy = {"client": ["auto"], "cookies": False} # Default

        for i, ydl_opts in enumerate(strategies):
            client = ydl_opts.get('extractor_args', {}).get('youtube', {}).get('player_client', ['auto'])
            has_cookies = 'cookiefile' in ydl_opts
            print(f"[INFO] Intento {i+1}/{len(strategies)}: cliente={client}, cookies={has_cookies}")
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target_url, download=False)
                if info:
                    print(f"[INFO] ✅ Éxito con estrategia {i+1}")
                    success_strategy = {"client": client, "cookies": has_cookies}
                    break
                else:
                    print(f"[WARN] Estrategia {i+1} devolvió None, probando siguiente...")
            except HTTPException:
                raise
            except Exception as e:
                error_str = str(e)
                print(f"[WARN] Estrategia {i+1} falló: {error_str[:200]}")
                # Detectar errores de DNS / red
                if 'NameResolutionError' in error_str or 'Failed to resolve' in error_str or 'No address associated' in error_str:
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc
                    raise HTTPException(
                        status_code=503,
                        detail=f"El servidor no puede conectarse a '{domain}'. Este dominio puede estar bloqueado."
                    )
                continue

        # --- Fuera del loop: procesar formatos del info obtenido ---
        if not info:
            raise HTTPException(status_code=404, detail="No se pudo extraer información del video.")

        video_formats = []
        audio_formats = []

        raw_formats = info.get('formats')
        if raw_formats is None or not isinstance(raw_formats, list):
            raw_formats = []
        if not raw_formats:
            raw_formats = [info]

        # Set para deduplicación por resolución (Video)
        seen_resolutions = set()

        # Ordenar por ALTURA (resolución) primero, luego por bitrate
        def quality_key(f):
            height = f.get('height') or 0
            tbr = f.get('tbr') or 0
            filesize = f.get('filesize') or 0
            return (height, tbr, filesize)

        raw_formats.sort(key=quality_key, reverse=True)
        print(f"Total de formatos encontrados por yt-dlp: {len(raw_formats)}")
        for fmt in raw_formats[:10]:
            print(f"  Format: {fmt.get('format_id')} | {fmt.get('height')}p | vcodec={fmt.get('vcodec')} | acodec={fmt.get('acodec')} | ext={fmt.get('ext')}")

        # Calcular tamaño del mejor audio m4a (se descarga siempre junto al video)
        best_audio_for_video = next(
            (f for f in raw_formats if f.get('vcodec') == 'none' and f.get('ext') == 'm4a'),
            next((f for f in raw_formats if f.get('vcodec') == 'none'), None)
        )
        audio_size_bytes = 0
        if best_audio_for_video:
            audio_size_bytes = best_audio_for_video.get('filesize') or best_audio_for_video.get('filesize_approx') or 0

        for f in raw_formats:
            if not f: continue

            format_id = f.get('format_id')
            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')
            is_video = vcodec != 'none'

            if is_video:
                height = f.get('height')
                if not height: continue

                resolution_key = f"{height}p"

                # Tamaño real = video + audio (porque siempre se descargan y fusionan)
                video_size = f.get('filesize') or f.get('filesize_approx') or 0
                total_size = video_size + audio_size_bytes
                size_str = format_size(total_size) if total_size > 0 else "N/A"

                video_formats.append({
                    "format_id": format_id,
                    "extension": "mp4",
                    "resolution": resolution_key,
                    "filesize_str": size_str,
                    "label": f"{resolution_key} - MP4",
                    "is_video": True
                })

        # Ordenar videos por altura (resolución)
        video_formats.sort(key=lambda x: int(x['resolution'].replace('p', '')), reverse=True)

        # Opción de Audio (MP3)
        best_audio = next((f for f in raw_formats if f.get('vcodec') == 'none'), None)
        audio_size_str = "N/A"
        if best_audio:
            audio_size_str = format_size(best_audio.get('filesize') or best_audio.get('filesize_approx'))

        audio_formats.append({
            "format_id": "best_audio_mp3",
            "extension": "mp3",
            "resolution": "Audio High Quality",
            "filesize_str": audio_size_str,
            "label": "Audio Only - MP3",
            "is_video": False
        })

        return {
            "title": info.get('title') or 'Video Desconocido',
            "thumbnail": info.get('thumbnail'),
            "duration": info.get('duration'),
            "videos": video_formats,
            "audios": audio_formats,
            "url": target_url,
            "original_url": url,
            "strategy": success_strategy
        }

    except HTTPException:
        # Re-lanzar HTTPExceptions sin modificar (no convertirlas en 500)
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error analizando (yt-dlp): {e}")
        raise HTTPException(status_code=500, detail=f"Error al analizar el video: {str(e)}")

@app.get("/download-selected")
def download_selected(url: str, format_id: str, client: str = None, use_cookies: str = 'true'):
    """Inicia la descarga en background y devuelve un file_id para seguir el progreso."""
    if not url or not format_id:
        raise HTTPException(status_code=400, detail="URL y format_id requeridos")

    file_id = str(uuid.uuid4())

    # Procesar parámetros de estrategia
    use_cookies_bool = use_cookies.lower() == 'true'
    
    # Determinar cliente
    # Si viene del frontend, usarlo. Si no, fallback al default robusto.
    if client and client != 'null':
        target_clients = [client] if ',' not in client else client.split(',')
    else:
        target_clients = ['tv_embedded', 'android', 'ios', 'mweb']

    print(f"[INFO] Iniciando descarga ({file_id}) con cliente={target_clients} cookies={use_cookies_bool}")

    # Configuración base con EJS y Node.js para evitar el bot en la descarga también
    base_opts = {
        'noplaylist': True,
        'quiet': True,
        'http_headers': DEFAULT_HEADERS,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'retries': 10,
        'outtmpl': str(DOWNLOAD_DIR / f"{file_id}.%(ext)s"),
        'cookiefile': YOUTUBE_COOKIES_FILE if (YOUTUBE_COOKIES_FILE and use_cookies_bool) else None,
        'js_runtimes': {'nodejs': {}},
        'extractor_args': {'youtube': {'player_client': target_clients}},
    }

    ydl_opts = {**base_opts}

    if format_id == "best_audio_mp3":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
        final_ext = "mp3"
    else:
        # Descarga de Video MP4 - MODO RÁPIDO (Stream Copy)
        # Forzamos EXACTAMENTE el format_id seleccionado + mejor audio
        ydl_opts.update({
            'format': f"{format_id}+bestaudio[ext=m4a]/bestaudio/best",
            'merge_output_format': 'mp4',
            'postprocessor_args': {
                'ffmpeg': [
                    '-c', 'copy',
                    '-map', '0:v:0',
                    '-map', '1:a:0',
                    '-shortest'
                ]
            }
        })
        final_ext = "mp4"

    # Agregar hook de progreso
    ydl_opts['progress_hooks'] = [make_progress_hook(file_id)]

    # Inicializar progreso
    download_progress[file_id] = {
        'percent': 0, 'status': 'downloading',
        'message': 'Iniciando descarga...', 'filename': None, 'path': None
    }

    # Lanzar descarga en hilo separado
    thread = threading.Thread(
        target=_run_download,
        args=(file_id, url, final_ext, ydl_opts),
        daemon=True
    )
    thread.start()

    return JSONResponse({'file_id': file_id})


# --- STORE GLOBAL DE PROGRESO ---
download_progress = {}  # {file_id: {percent, status, message, filename, path}}


def make_progress_hook(file_id: str):
    """Crea un hook de progreso para yt-dlp que actualiza download_progress."""
    download_count = [0]  # [0]=video, [1]=audio

    def hook(d):
        if d['status'] == 'downloading':
            try:
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                done = d.get('downloaded_bytes', 0)
                pct = (done / total * 100) if total > 0 else 0

                if download_count[0] == 0:  # Descargando video (0-70%)
                    mapped = pct * 0.70
                    msg = f'📥 Descargando video... {pct:.0f}%'
                else:                        # Descargando audio (70-90%)
                    mapped = 70 + pct * 0.20
                    msg = f'🎵 Descargando audio... {pct:.0f}%'

                download_progress[file_id].update({
                    'percent': round(mapped, 1),
                    'status': 'downloading',
                    'message': msg
                })
            except Exception:
                pass

        elif d['status'] == 'finished':
            download_count[0] += 1
            if download_count[0] == 1:
                download_progress[file_id].update({
                    'percent': 70, 'message': '🎵 Descargando audio...'
                })
            else:
                download_progress[file_id].update({
                    'percent': 90, 'status': 'merging',
                    'message': '⚙️ Combinando streams con FFmpeg...'
                })
    return hook


def _run_download(file_id: str, url: str, final_ext: str, ydl_opts: dict):
    """Ejecuta yt-dlp + FFmpeg en un hilo separado."""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_title = (info.get('title') if info else None) or 'video'

        # Buscar el archivo generado
        final_path = DOWNLOAD_DIR / f"{file_id}.{final_ext}"
        if not final_path.exists():
            found = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))
            if found:
                final_path = found[0]
                final_ext = final_path.suffix.lstrip('.')
            else:
                download_progress[file_id].update({
                    'status': 'error', 'message': 'Archivo no encontrado tras la descarga.'
                })
                return

        safe_title = "".join(c for c in video_title if c.isalnum() or c in (' ', '-', '_')).strip()
        user_filename = f"{safe_title}.{final_ext}"

        download_progress[file_id].update({
            'percent': 100, 'status': 'ready',
            'message': '✅ ¡Listo! Descargando archivo...',
            'filename': user_filename,
            'path': str(final_path)
        })

    except Exception as e:
        print(f"[ERROR] Descarga fallida ({file_id}): {e}")
        download_progress[file_id].update({
            'status': 'error', 'message': f'Error: {str(e)[:200]}'
        })
        # Limpiar archivos parciales
        for f in DOWNLOAD_DIR.glob(f"{file_id}.*"):
            try: os.remove(f)
            except: pass


@app.get("/progress/{file_id}")
def get_progress(file_id: str):
    """Devuelve el progreso actual de una descarga."""
    data = download_progress.get(file_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Descarga no encontrada")
    return JSONResponse(data)


@app.get("/get-file/{file_id}")
def get_file(file_id: str, background_tasks: BackgroundTasks):
    """Sirve el archivo cuando está listo. Sólo funciona si status='ready'."""
    data = download_progress.get(file_id)
    if not data or data.get('status') != 'ready':
        raise HTTPException(status_code=404, detail="Archivo no disponible aún")

    path = data['path']
    filename = data['filename']

    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado en disco")

    # Limpiar tras entregar
    background_tasks.add_task(cleanup_file, Path(path))
    # Limpiar entrada del progreso tras un rato
    def _cleanup_progress():
        import time; time.sleep(60)
        download_progress.pop(file_id, None)
    threading.Thread(target=_cleanup_progress, daemon=True).start()

    return FileResponse(path=path, filename=filename, media_type='application/octet-stream')


if __name__ == "__main__":
    import uvicorn
    # En producción (Railway), el puerto se pasa por variable de entorno
    port = int(os.environ.get("PORT", 8080))
    # Importante: host="0.0.0.0" para que sea accesible fuera del contenedor
    uvicorn.run(app, host="0.0.0.0", port=port)
