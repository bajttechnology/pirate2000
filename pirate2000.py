import webview
import yt_dlp
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import os
import sys
import threading
import ctypes
import json
import logging

# Pri buildoch s --noconsole je sys.stdout / sys.stderr = None a niektoré knižnice
# by na tom spadli. Preto ich presmerujeme "do prázdna".
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

APP_VERSION = "0.7"

def resource_path(relative_path):
    """Získa absolútnu cestu k zdroju, funguje aj pre PyInstaller --onefile"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def default_downloads():
    cesta = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(cesta, exist_ok=True)
    return cesta

def zapis_log(text):
    """Zapíše technické detaily chýb do Pirate2000_chyby.log v predvolenom priečinku Downloads."""
    try:
        with open(os.path.join(default_downloads(), "Pirate2000_chyby.log"), "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except Exception:
        pass

class TichyLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): zapis_log(f"WARNING: {msg}")
    def error(self, msg): zapis_log(f"ERROR: {msg}")

def zrozumitelna_chyba(text):
    t = text.lower()
    if "unsupported url" in t:
        return "Táto adresa nie je podporovaná. Skontroluj, či je to odkaz na video."
    if "sign in to confirm" in t or "confirm you’re not a bot" in t or "confirm you're not a bot" in t:
        return "YouTube žiada overenie (ochrana proti botom alebo video s vekovým obmedzením)."
    if "private video" in t or "video unavailable" in t or "this video is not available" in t:
        return "Video je súkromné, zmazané alebo nedostupné v tvojej krajine."
    if "members-only" in t or "join this channel" in t:
        return "Video je len pre členov kanála."
    if "live event" in t or "premieres in" in t:
        return "Video ešte nezačalo (premiéra / živý prenos)."
    if "requested format is not available" in t or "only images are available" in t or "n challenge" in t:
        return "Nepodarilo sa získať formáty videa. Najčastejšie je zastaraný yt-dlp alebo chýba JavaScript runtime."
    if "ffmpeg" in t or "ffprobe" in t:
        return "Chýba alebo nefunguje ffmpeg/ffprobe (musia byť zabalené v .exe)."
    if "http error 403" in t:
        return "YouTube zablokoval sťahovanie (403). Skús aktualizovať yt-dlp."
    if "http error 429" in t:
        return "Príliš veľa požiadaviek (429). Chvíľu počkaj a skús znova."
    if "getaddrinfo" in t or "timed out" in t or "unable to download webpage" in t or "network" in t:
        return "Problém s internetom. Skontroluj pripojenie."
    return "Neznáma chyba pri sťahovaní."

def zostav_nastavenia(volba, progress_hook, playlist, download_dir):
    interne = resource_path(".")
    ydl_opts = {
        "outtmpl": os.path.join(download_dir, "%(title).150s.%(ext)s"),
        "noplaylist": not playlist,
        "windowsfilenames": True,
        "quiet": True,
        "noprogress": True,
        "logger": TichyLogger(),
        "progress_hooks": [progress_hook],
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }

    if os.path.isfile(os.path.join(interne, "ffmpeg.exe")):
        ydl_opts["ffmpeg_location"] = interne

    qjs = os.path.join(interne, "qjs.exe")
    if os.path.isfile(qjs):
        ydl_opts["js_runtimes"] = {"quickjs": {"path": qjs}}

    if volba == "mp3":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
        })
    elif volba == "mp4_max":
        ydl_opts.update({
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
        })
    else:  # mp4_compat
        ydl_opts.update({
            "format": "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "merge_output_format": "mp4",
        })
    return ydl_opts


class PirateBackend:
    def __init__(self):
        self.is_downloading = False
        self.download_dir = default_downloads()

    def get_ytdlp_version(self):
        return yt_dlp.version.__version__

    def get_download_dir(self):
        return self.download_dir

    def select_folder(self):
        if webview.windows:
            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=self.download_dir)
            if result and len(result) > 0:
                self.download_dir = result[0]
        return self.download_dir

    def open_folder(self):
        try:
            os.startfile(self.download_dir)
        except Exception as e:
            zapis_log(f"Priečinok sa nepodarilo otvoriť: {e}")

    def _ui_update(self, js_code):
        # Bezpečné odoslanie JS kódu bez ukladania okna v self (predchádza SyncRoot chybe)
        try:
            if webview.windows:
                webview.windows[0].evaluate_js(js_code)
        except Exception:
            pass

    def start_download(self, url, format_key, is_playlist):
        if self.is_downloading:
            return False
        
        if not url:
            self._ui_update("downloadError('Nevložil si žiadnu URL adresu!')")
            return False
            
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url

        self.is_downloading = True
        self._ui_update("setBusy(true)")
        
        threading.Thread(target=self._stiahni, args=(url, format_key, is_playlist), daemon=True).start()
        return True

    def _stiahni(self, url, volba, is_playlist):
        stav = {"dokoncene": 0, "casti": 1}

        def hook(d):
            info = d.get("info_dict") or {}
            
            stav["casti"] = len(info.get("requested_formats") or []) or 1
            
            total_playlist = info.get('playlist_count') or 1
            current_index = info.get('playlist_index') or 1

            if d["status"] == "downloading":
                celkom = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                podiel = (d.get("downloaded_bytes") or 0) / celkom if celkom else 0
                
                aktualny_subor_progres = (stav["dokoncene"] + podiel) / stav["casti"]
                
                if total_playlist > 1:
                    spolu = (current_index - 1 + aktualny_subor_progres) / total_playlist
                else:
                    spolu = min(aktualny_subor_progres, 1.0)
                
                rychlost = d.get("speed")
                speed_str = f"({rychlost / 1048576:.1f} MB/s)" if rychlost else ""
                
                text = f"Sťahujem… {spolu * 100:.0f} %"
                if total_playlist > 1:
                    text += f" (Video {current_index}/{total_playlist})"
                
                js_text = json.dumps(text)
                js_speed = json.dumps(speed_str)
                self._ui_update(f"updateProgress({spolu * 100}, {js_text}, {js_speed})")

            elif d["status"] == "finished":
                stav["dokoncene"] += 1
                if stav["dokoncene"] >= stav["casti"]:
                    self._ui_update("updateProgress(100, 'Spracúvam súbor (ffmpeg)…', '')")

        try:
            with YoutubeDL(zostav_nastavenia(volba, hook, is_playlist, self.download_dir)) as ydl:
                ydl.download([url])
            
            self.is_downloading = False
            js_playlist = "true" if is_playlist else "false"
            js_dir = json.dumps(self.download_dir)
            self._ui_update(f"downloadFinished({js_playlist}, {js_dir})")

        except DownloadError as e:
            zapis_log(f"URL: {url}\n{e}\n")
            err_msg = zrozumitelna_chyba(str(e)) + " (detaily: Pirate2000_chyby.log v Downloads)"
            self.is_downloading = False
            self._ui_update(f"downloadError({json.dumps(err_msg)})")

        except Exception as e:
            zapis_log(f"URL: {url}\n{type(e).__name__}: {e}\n")
            err_msg = f"Neočakaná chyba: {e}"
            self.is_downloading = False
            self._ui_update(f"downloadError({json.dumps(err_msg)})")

# Načítanie SVG animácie
svg_content = ""
try:
    with open(resource_path('pirate icon.svg'), 'r', encoding='utf-8') as f:
        svg_content = f.read()
except Exception:
    svg_content = "<div style='color: #fb7966; font-family: Satoshi, sans-serif; font-weight: 900; font-size: 40px;'>PIRATE2000</div>"

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <style>
        @font-face {
            font-family: 'Satoshi';
            src: local('Satoshi Medium');
            font-weight: 500;
        }
        @font-face {
            font-family: 'Satoshi';
            src: local('Satoshi Black');
            font-weight: 900;
        }

        :root {
            --bg: #0b0d12;
            --bg-gradient: linear-gradient(135deg, #0b0d12 0%, #141821 100%);
            --surface: #141821;
            --surface2: #1b2029;
            --border: #2a3140;
            --text: #eef2f8;
            --muted: #8b93a5;
            --accent: #fb7966;
            --accent-hover: #ff9484;
            --accent-text: #0b0d12;
            --success: #3ecf8e;
            --danger: #f2555a;
            
            --radius-pill: 9999px;
            --radius-card: 24px;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Satoshi', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            user-select: none;
        }

        body {
            background: var(--bg-gradient);
            color: var(--text);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 30px 40px;
        }

        /* SPLASH SCREEN */
        #splash-screen {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: var(--bg);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 99999;
            transition: opacity 0.6s ease-in-out;
        }
        
        #splash-screen.fade-out {
            opacity: 0;
            pointer-events: none;
        }

        #splash-screen svg {
            max-width: 250px;
            max-height: 250px;
            width: 100%;
            height: 100%;
            object-fit: contain;
        }

        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--accent); border-radius: var(--radius-pill); }

        /* HLAVIČKA */
        .header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 25px;
            flex-shrink: 0;
        }

        .header-text {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .header h1 {
            font-family: 'Satoshi';
            font-weight: 900;
            font-size: 32px;
            color: var(--text);
            letter-spacing: 1px;
        }

        .version-badge {
            background: rgba(251, 121, 102, 0.15);
            color: var(--accent);
            padding: 4px 12px;
            border-radius: var(--radius-pill);
            font-size: 12px;
            font-weight: 900;
            border: 1px solid rgba(251, 121, 102, 0.3);
        }

        .header p {
            color: var(--muted);
            font-size: 14px;
            margin-top: 6px;
        }

        /* KARTA VSTUP */
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-card);
            padding: 25px;
            margin-bottom: 20px;
            display: flex;
            flex-direction: column;
            gap: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            flex-shrink: 0;
        }

        .input-group {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        label {
            font-size: 12px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 1px;
            font-weight: 900;
            padding-left: 15px;
        }

        .input-wrapper {
            position: relative;
        }

        input[type="text"] {
            width: 100%;
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 16px 25px;
            border-radius: var(--radius-pill);
            font-size: 16px;
            outline: none;
            transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
        }

        input[type="text"]:focus {
            border-color: var(--accent);
            box-shadow: 0 0 20px rgba(251, 121, 102, 0.2);
            background: rgba(0, 0, 0, 0.5);
        }

        input[type="text"]::placeholder {
            color: #5c6478;
        }

        /* VÝBER PRIEČINKA */
        .folder-input-group {
            display: flex;
            gap: 10px;
            padding-left: 15px;
            padding-right: 15px;
        }

        .folder-input-group input {
            flex: 1;
            padding: 12px 20px;
            font-size: 14px;
            color: var(--muted);
            cursor: default;
        }

        .btn-browse {
            background: var(--surface2);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 0 25px;
            border-radius: var(--radius-pill);
            font-size: 14px;
            font-weight: 900;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-browse:hover {
            background: var(--border);
        }

        /* FORMAT SELECTOR */
        .format-grid {
            display: flex;
            gap: 10px;
            background: rgba(0, 0, 0, 0.2);
            padding: 6px;
            border-radius: var(--radius-pill);
            border: 1px solid var(--border);
        }

        .format-btn {
            flex: 1;
            background: transparent;
            border: none;
            color: var(--muted);
            padding: 14px;
            border-radius: var(--radius-pill);
            font-size: 14px;
            font-weight: 900;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
        }

        .format-btn:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text);
        }

        .format-btn.active {
            background: var(--accent);
            color: var(--accent-text);
            box-shadow: 0 4px 15px rgba(251, 121, 102, 0.3);
        }

        .format-hint {
            font-size: 13px;
            color: var(--muted);
            padding-left: 15px;
            min-height: 20px;
            transition: opacity 0.3s ease;
        }

        /* TOGGLE SWITCH (PLAYLIST) */
        .toggle-container {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 15px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: var(--radius-pill);
            border: 1px solid var(--border);
        }

        .toggle-label {
            font-size: 14px;
            color: var(--text);
            font-weight: 900;
            padding: 0;
            text-transform: none;
        }

        .switch {
            position: relative;
            display: inline-block;
            width: 50px;
            height: 28px;
        }

        .switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }

        .slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background-color: #2a3140;
            transition: .4s;
            border-radius: var(--radius-pill);
        }

        .slider:before {
            position: absolute;
            content: "";
            height: 20px;
            width: 20px;
            left: 4px;
            bottom: 4px;
            background-color: var(--muted);
            transition: .4s;
            border-radius: 50%;
        }

        input:checked + .slider {
            background-color: var(--accent);
        }

        input:checked + .slider:before {
            transform: translateX(22px);
            background-color: var(--accent-text);
        }

        /* PRIEBEH */
        .progress-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-card);
            padding: 25px;
            margin-bottom: 20px;
            display: none;
            animation: slideDown 0.4s ease-out forwards;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            flex-shrink: 0;
        }

        .progress-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }

        .status-text {
            font-size: 14px;
            font-weight: 900;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 8px;
            transition: color 0.3s;
        }

        .percent-text {
            font-family: monospace;
            font-size: 16px;
            font-weight: bold;
            color: var(--text);
            transition: color 0.3s;
        }

        .progress-bar-bg {
            width: 100%;
            height: 12px;
            background: rgba(0, 0, 0, 0.4);
            border-radius: var(--radius-pill);
            overflow: hidden;
            position: relative;
        }

        .progress-bar-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, var(--accent) 0%, var(--accent-hover) 50%, var(--accent) 100%);
            background-size: 200% 100%;
            border-radius: var(--radius-pill);
            transition: width 0.4s cubic-bezier(0.25, 0.8, 0.25, 1), background 0.3s;
            animation: shimmer 2s infinite linear;
        }

        @keyframes shimmer {
            0% { background-position: 100% 0; }
            100% { background-position: -100% 0; }
        }

        /* HLAVNÉ TLAČIDLO */
        .btn-download {
            width: 100%;
            background: var(--accent);
            color: var(--accent-text);
            border: none;
            padding: 20px;
            border-radius: var(--radius-pill);
            font-size: 18px;
            font-weight: 900;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
            box-shadow: 0 10px 25px rgba(251, 121, 102, 0.2);
            letter-spacing: 1px;
            flex-shrink: 0;
        }

        .btn-download:hover {
            transform: translateY(-3px) scale(1.01);
            box-shadow: 0 15px 35px rgba(251, 121, 102, 0.4);
            background: var(--accent-hover);
        }

        .btn-download:active {
            transform: translateY(1px) scale(0.99);
        }

        .btn-download:disabled {
            background: #2a3140;
            color: #5c6478;
            cursor: not-allowed;
            box-shadow: none;
            transform: none;
        }

        /* PÄTIČKA */
        .footer {
            margin-top: auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 20px;
            padding-bottom: 10px;
            flex-shrink: 0;
        }

        .footer-text {
            color: #5c6478;
            font-size: 12px;
        }

        .btn-footer {
            background: transparent;
            border: none;
            color: var(--muted);
            font-size: 13px;
            font-weight: 900;
            cursor: pointer;
            padding: 10px 20px;
            border-radius: var(--radius-pill);
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .btn-footer:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text);
        }

        @keyframes slideDown {
            from { opacity: 0; transform: translateY(-20px); }
            to { opacity: 1; transform: translateY(0); }
        }
    </style>
</head>
<body>

    <!-- SPLASH SCREEN -->
    <div id="splash-screen">
        {SVG_PLACEHOLDER}
    </div>

    <!-- HLAVIČKA -->
    <div class="header">
        <div>
            <div class="header-text">
                <h1>Pirate2000</h1>
                <span class="version-badge">v0.7</span>
            </div>
            <p>Stiahni video alebo hudbu jedným klikom</p>
        </div>
    </div>

    <!-- KARTA VSTUP -->
    <div class="card">
        <div class="input-group">
            <label>Odkaz na video</label>
            <div class="input-wrapper">
                <input type="text" id="url-input" placeholder="Sem vlož URL adresu…" onkeypress="handleEnter(event)">
            </div>
        </div>

        <div class="input-group">
            <label>Uložiť do priečinka</label>
            <div class="folder-input-group">
                <input type="text" id="folder-input" readonly>
                <button class="btn-browse" onclick="selectFolder()">Prezerať…</button>
            </div>
        </div>

        <div class="input-group">
            <label>Formát súboru</label>
            <div class="format-grid">
                <button class="format-btn active" onclick="selectFormat('mp4_compat', this, 'H.264 + AAC – prehrá to každý prehrávač aj strihový program.')">MP4 kompatibilné</button>
                <button class="format-btn" onclick="selectFormat('mp4_max', this, 'Najvyššie rozlíšenie, aké video ponúka (VP9/AV1 nemusia ísť všade).')">MP4 max kvalita</button>
                <button class="format-btn" onclick="selectFormat('mp3', this, 'Iba zvuk, 320 kbps.')">MP3 audio</button>
            </div>
            <div class="format-hint" id="format-hint">H.264 + AAC – prehrá to každý prehrávač aj strihový program.</div>
        </div>

        <div class="toggle-container">
            <label class="toggle-label">Stiahnuť celý playlist</label>
            <label class="switch">
                <input type="checkbox" id="playlist-toggle">
                <span class="slider"></span>
            </label>
        </div>
    </div>

    <!-- PRIEBEH -->
    <div class="progress-card" id="progress-card">
        <div class="progress-header">
            <div class="status-text" id="status-text">Sťahujem…</div>
            <div class="percent-text" id="percent-text">0 %</div>
        </div>
        <div class="progress-bar-bg">
            <div class="progress-bar-fill" id="progress-bar"></div>
        </div>
    </div>

    <!-- HLAVNÉ TLAČIDLO -->
    <button class="btn-download" id="download-btn" onclick="startDownload()">Stiahnuť</button>

    <!-- PÄTIČKA -->
    <div class="footer">
        <div class="footer-text" id="ytdlp-version">yt-dlp ...</div>
        <button class="btn-footer" onclick="openFolder()">
            📂 Otvoriť priečinok
        </button>
    </div>

    <script>
        let currentFormat = 'mp4_compat';
        let isCurrentPlaylist = false;

        window.addEventListener('pywebviewready', async () => {
            const version = await window.pywebview.api.get_ytdlp_version();
            document.getElementById('ytdlp-version').innerText = `yt-dlp ${version}`;
            
            const currentDir = await window.pywebview.api.get_download_dir();
            document.getElementById('folder-input').value = currentDir;

            setTimeout(() => {
                const splash = document.getElementById('splash-screen');
                if (splash) {
                    splash.classList.add('fade-out');
                    setTimeout(() => splash.remove(), 600);
                }
            }, 2000);
        });

        async function selectFolder() {
            const newPath = await window.pywebview.api.select_folder();
            if (newPath) {
                document.getElementById('folder-input').value = newPath;
            }
        }

        function selectFormat(key, btnElement, hintText) {
            currentFormat = key;
            document.querySelectorAll('.format-btn').forEach(btn => btn.classList.remove('active'));
            btnElement.classList.add('active');
            
            const hint = document.getElementById('format-hint');
            hint.style.opacity = 0;
            setTimeout(() => {
                hint.innerText = hintText;
                hint.style.opacity = 1;
            }, 150);
        }

        function handleEnter(e) {
            if (e.key === 'Enter') {
                startDownload();
            }
        }

        async function startDownload() {
            const url = document.getElementById('url-input').value.trim();
            isCurrentPlaylist = document.getElementById('playlist-toggle').checked;
            
            if (!url) {
                showError("Nevložil si žiadnu URL adresu!");
                return;
            }

            document.getElementById('progress-card').style.display = 'block';
            document.getElementById('status-text').style.color = 'var(--text)';
            document.getElementById('status-text').innerText = 'Pripravujem sťahovanie…';
            document.getElementById('percent-text').innerText = '0 %';
            document.getElementById('percent-text').style.color = 'var(--text)';
            document.getElementById('progress-bar').style.width = '0%';
            document.getElementById('progress-bar').style.background = 'linear-gradient(90deg, var(--accent) 0%, var(--accent-hover) 50%, var(--accent) 100%)';

            setBusy(true);
            await window.pywebview.api.start_download(url, currentFormat, isCurrentPlaylist);
        }

        function setBusy(isBusy) {
            const btn = document.getElementById('download-btn');
            const input = document.getElementById('url-input');
            const formatBtns = document.querySelectorAll('.format-btn');
            const playlistToggle = document.getElementById('playlist-toggle');
            const browseBtn = document.querySelector('.btn-browse');

            if (isBusy) {
                btn.disabled = true;
                btn.innerText = 'Sťahujem…';
                input.disabled = true;
                formatBtns.forEach(b => b.disabled = true);
                playlistToggle.disabled = true;
                browseBtn.disabled = true;
            } else {
                btn.disabled = false;
                btn.innerText = 'Stiahnuť';
                input.disabled = false;
                formatBtns.forEach(b => b.disabled = false);
                playlistToggle.disabled = false;
                browseBtn.disabled = false;
            }
        }

        window.updateProgress = function(percent, text, speed) {
            document.getElementById('percent-text').innerText = `${Math.round(percent)} %`;
            document.getElementById('progress-bar').style.width = `${percent}%`;
            document.getElementById('status-text').innerText = `${text} ${speed}`;
        };

        window.downloadFinished = function(isPlaylist, folderPath) {
            document.getElementById('progress-bar').style.width = '100%';
            document.getElementById('progress-bar').style.background = 'var(--success)';
            document.getElementById('percent-text').innerText = '100 %';
            
            document.getElementById('percent-text').style.color = 'var(--text)';
            document.getElementById('status-text').style.color = 'var(--text)';
            
            const textSubor = isPlaylist ? "Súbory" : "Súbor";
            document.getElementById('status-text').innerText = `✅ Hotovo! ${textSubor} nájdeš v priečinku: ${folderPath}`;
            
            document.getElementById('url-input').value = '';
            setBusy(false);
        };

        window.downloadError = function(msg) {
            document.getElementById('progress-bar').style.width = '0%';
            document.getElementById('percent-text').innerText = '0 %';
            document.getElementById('percent-text').style.color = 'var(--text)';
            document.getElementById('status-text').style.color = 'var(--danger)';
            document.getElementById('status-text').innerText = `❌ ${msg}`;
            setBusy(false);
        };

        function showError(msg) {
            document.getElementById('progress-card').style.display = 'block';
            document.getElementById('status-text').style.color = 'var(--danger)';
            document.getElementById('status-text').innerText = `❌ ${msg}`;
        }

        function openFolder() {
            window.pywebview.api.open_folder();
        }
    </script>
</body>
</html>
"""

HTML_CONTENT = HTML_CONTENT.replace("{SVG_PLACEHOLDER}", svg_content)

if __name__ == "__main__":
    logging.getLogger('pywebview').setLevel(logging.ERROR)
    
    backend = PirateBackend()
    
    icon_path = resource_path('ikona2000.ico')

    try:
        user32 = ctypes.windll.user32
        screen_width = user32.GetSystemMetrics(0)
        screen_height = user32.GetSystemMetrics(1)
    except Exception:
        screen_width = 1920
        screen_height = 1080

    w_width = 760
    w_height = 720

    x_pos = (screen_width - w_width) // 2
    y_pos = ((screen_height - w_height) // 2) - 30

    window = webview.create_window(
        title='Pirate2000 0.7',
        html=HTML_CONTENT,
        js_api=backend,
        width=w_width,
        height=w_height,
        x=x_pos,
        y=y_pos,
        resizable=False,
        background_color='#0b0d12'
    )
    
    webview.start(icon=icon_path, private_mode=False)