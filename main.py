import os
import socket
import shutil
import uuid
import re
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
# En Hugging Face/Docker, /tmp es siempre escribible
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

        # Base de opciones comunes para todos los intentos
        base_ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'geo_bypass': True,
            'socket_timeout': 20,
        }

        # Lista de estrategias a intentar en orden
        strategies = []

        # Estrategia 1: tv_embedded SIN cookies (más permisivo en servidores)
        strategies.append({
            **base_ydl_opts,
            'ignoreerrors': True,
            'extractor_args': {'youtube': {'player_client': ['tv_embedded']}},
        })

        # Estrategia 2: ios + cookies (si están disponibles)
        if YOUTUBE_COOKIES_FILE:
            strategies.append({
                **base_ydl_opts,
                'ignoreerrors': True,
                'cookiefile': YOUTUBE_COOKIES_FILE,
                'extractor_args': {'youtube': {'player_client': ['ios']}},
            })
            # Estrategia 3: android + cookies
            strategies.append({
                **base_ydl_opts,
                'ignoreerrors': True,
                'cookiefile': YOUTUBE_COOKIES_FILE,
                'extractor_args': {'youtube': {'player_client': ['android']}},
            })

        # Estrategia final: sin cliente específico, dejar que yt-dlp decida
        strategies.append({
            **base_ydl_opts,
            'ignoreerrors': False,  # Mostrar error real
        })

        info = None
        for i, ydl_opts in enumerate(strategies):
            client = ydl_opts.get('extractor_args', {}).get('youtube', {}).get('player_client', ['auto'])
            has_cookies = 'cookiefile' in ydl_opts
            print(f"[INFO] Intento {i+1}/{len(strategies)}: cliente={client}, cookies={has_cookies}")
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target_url, download=False)
                if info:
                    print(f"[INFO] ✅ Éxito con estrategia {i+1}")
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

        for f in raw_formats:
            if not f: continue

            format_id = f.get('format_id')
            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')
            is_video = vcodec != 'none'

            filesize = f.get('filesize') or f.get('filesize_approx')
            size_str = format_size(filesize)

            if is_video:
                height = f.get('height')
                if not height: continue

                resolution_key = f"{height}p"

                if resolution_key in seen_resolutions:
                    continue
                seen_resolutions.add(resolution_key)

                video_formats.append({
                    "format_id": format_id,
                    "extension": "mp4",
                    "resolution": resolution_key,
                    "filesize_str": size_str,
                    "label": f"{resolution_key} - MP4",
                    "is_video": True,
                    "tbr": f.get('tbr') or 0
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
            "original_url": url
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
def download_selected(url: str, format_id: str, background_tasks: BackgroundTasks):
    if not url or not format_id:
        raise HTTPException(status_code=400, detail="URL y format_id requeridos")

    file_id = str(uuid.uuid4())
    
    # Configuración base
    ydl_opts = {
        'noplaylist': True,
        'quiet': True,
        'http_headers': DEFAULT_HEADERS,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'retries': 10,
        'outtmpl': str(DOWNLOAD_DIR / f"{file_id}.%(ext)s"),
    }

    # Lógica según el tipo de descarga
    if format_id == "best_audio_mp3":
        # Descarga de Audio MP3
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
        # Descarga de Video MP4
        # Priorizamos audio m4a (AAC) para que sea compatible con todos los reproductores
        ydl_opts.update({
            'format': f"{format_id}+bestaudio[ext=m4a]/bestaudio/best",
            'merge_output_format': 'mp4',
        })
        final_ext = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Descarga
            info = ydl.extract_info(url, download=True)
            video_title = info.get('title', 'video')
            
            final_path = DOWNLOAD_DIR / f"{file_id}.{final_ext}"
            
            # Busqueda de fallback si la extensión cambió (ej: m4a -> mp3)
            if not final_path.exists():
                found = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))
                if found:
                    final_path = found[0]
                    final_ext = final_path.suffix.lstrip('.')
            
            safe_title = "".join([c for c in video_title if c.isalnum() or c in (' ', '-', '_')]).strip()
            user_filename = f"{safe_title}.{final_ext}"

            background_tasks.add_task(cleanup_file, final_path)

            return FileResponse(
                path=final_path, 
                filename=user_filename, 
                media_type='application/octet-stream'
            )

    except Exception as e:
        print(f"Error en descarga: {e}")
        for f in DOWNLOAD_DIR.glob(f"{file_id}.*"):
            try: os.remove(f)
            except: pass
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
