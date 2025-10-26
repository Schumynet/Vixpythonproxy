#!/usr/bin/env python3
"""
run.py - Flask proxy + player + download endpoint
Designed to be deployed on Fly.io (listen on 0.0.0.0:8080).
Routes:
  /proxy    -> rewrites m3u8 manifests and proxies binary segments
  /download -> serves m3u8 rewritten as downloadable file or forces download of binary
  /player   -> simple HLS player page that uses /proxy as source
"""
from flask import Flask, request, Response, stream_with_context
import requests
import urllib.parse
from urllib.parse import urljoin
import os
import sys

app = Flask(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://vixsrc.to"
}
TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", 15))


def forward_request(url, extra_cookie=None, stream=False, verify=True):
    headers = dict(DEFAULT_HEADERS)
    # allow extra headers via query param header_X=...
    for k, v in request.args.items():
        if k.startswith("header_"):
            hname = k[len("header_"):]
            headers[hname] = urllib.parse.unquote(v)
    if extra_cookie:
        headers["Cookie"] = extra_cookie
    elif "cookie" in request.args:
        headers["Cookie"] = urllib.parse.unquote(request.args.get("cookie"))
    try:
        return requests.get(url, headers=headers, stream=stream, timeout=TIMEOUT, allow_redirects=True, verify=verify)
    except Exception:
        return None


def resolve_absolute(uri, base):
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    return urljoin(base, uri)


@app.route("/proxy")
def proxy():
    u = request.args.get("u")
    if not u:
        return "Missing url param 'u'", 400
    target = urllib.parse.unquote(u)
    cookie = request.args.get("cookie")
    cookie = urllib.parse.unquote(cookie) if cookie else None

    # Try normal verify, fallback to verify=False if SSL errors prevent connection
    r = forward_request(target, extra_cookie=cookie, stream=True, verify=True)
    if r is None:
        r = forward_request(target, extra_cookie=cookie, stream=True, verify=False)
        if r is None:
            return "Upstream error", 502

    content_type = (r.headers.get("Content-Type") or "").lower()

    # If manifest, rewrite URIs so client requests go back to /proxy
    if target.lower().endswith(".m3u8") or "mpegurl" in content_type or "application/vnd.apple.mpegurl" in content_type:
        try:
            text = r.text
            base = target.rsplit("/", 1)[0] + "/"
            out_lines = []
            for ln in text.splitlines():
                if not ln or ln.startswith("#"):
                    out_lines.append(ln)
                    continue
                absu = resolve_absolute(ln.strip(), base)
                # prefer https for segments
                if absu.startswith("http://"):
                    absu = "https://" + absu[len("http://"):]
                prox = "/proxy?u=" + urllib.parse.quote(absu, safe='')
                out_lines.append(prox)
            out = "\n".join(out_lines)
            headers_out = {
                "Content-Type": "application/vnd.apple.mpegurl",
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache"
            }
            return Response(out, status=200, headers=headers_out)
        except Exception:
            return "Manifest rewrite failed", 500

    # Binary resource proxy (segments, mp4, etc.)
    headers_out = {
        "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache"
    }
    return Response(stream_with_context(r.iter_content(chunk_size=8192)), status=r.status_code, headers=headers_out)


@app.route("/download")
def download():
    u = request.args.get("u")
    if not u:
        return "Missing url param 'u'", 400
    target = urllib.parse.unquote(u)
    r = forward_request(target, extra_cookie=None, stream=False, verify=True)
    if r is None:
        r = forward_request(target, extra_cookie=None, stream=False, verify=False)
        if r is None:
            return "Upstream error", 502

    content_type = (r.headers.get("Content-Type") or "application/octet-stream").lower()
    filename = urllib.parse.unquote(target.split("/")[-1].split("?")[0]) or "download"

    # m3u8 => rewrite and return as downloadable text file
    if target.lower().endswith(".m3u8") or "mpegurl" in content_type or "application/vnd.apple.mpegurl" in content_type:
        try:
            text = r.text
            base = target.rsplit("/", 1)[0] + "/"
            out_lines = []
            for ln in text.splitlines():
                if not ln or ln.startswith("#"):
                    out_lines.append(ln)
                    continue
                absu = resolve_absolute(ln.strip(), base)
                if absu.startswith("http://"):
                    absu = "https://" + absu[len("http://"):]
                prox = "/proxy?u=" + urllib.parse.quote(absu, safe='')
                out_lines.append(prox)
            out = "\n".join(out_lines)
            headers_out = {
                "Content-Type": "application/vnd.apple.mpegurl",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache"
            }
            return Response(out, status=200, headers=headers_out)
        except Exception:
            return "Failed to rewrite manifest", 500

    # Binary => stream with attachment
    headers_out = {
        "Content-Type": content_type,
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache"
    }
    return Response(stream_with_context(r.iter_content(chunk_size=8192)), status=r.status_code, headers=headers_out)


PLAYER_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Player Proxy</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body{margin:0;background:#111;color:#fff;font-family:Arial,Helvetica,sans-serif}
    .header{padding:12px;text-align:center;font-weight:700}
    .wrap{max-width:1100px;margin:0 auto;padding:12px}
    video{width:100%;height:60vh;background:#000;display:block;border-radius:6px}
    .controls{margin-top:8px;display:flex;gap:8px;align-items:center}
    .btn{background:#222;color:#fff;border:0;padding:8px 10px;border-radius:6px;cursor:pointer}
  </style>
</head>
<body>
  <div class="header">Proxy Player</div>
  <div class="wrap">
    <video id="video" controls crossorigin playsinline></video>
    <div class="controls">
      <div id="meta">Titolo</div>
      <div style="margin-left:auto">
        <button id="openBtn" class="btn">Open in new</button>
        <button id="dlBtn" class="btn">Download manifest</button>
      </div>
    </div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
  <script>
    const params = new URLSearchParams(location.search);
    const u = params.get('u');
    const title = params.get('title') || '';
    document.getElementById('meta').textContent = decodeURIComponent(title);
    const video = document.getElementById('video');
    const prox = '/proxy?u=' + encodeURIComponent(u);
    const dl = '/download?u=' + encodeURIComponent(u);
    document.getElementById('openBtn').onclick = ()=> window.open('/player?u='+encodeURIComponent(u)+'&title='+encodeURIComponent(title), '_blank');
    document.getElementById('dlBtn').onclick = ()=> window.open(dl, '_blank');

    function init(){
      try{
        if (window.Hls && Hls.isSupported()){
          const hls = new Hls({enableWorker:false});
          hls.loadSource(prox);
          hls.attachMedia(video);
        } else if (video.canPlayType('application/vnd.apple.mpegurl')){
          video.src = prox;
        } else {
          alert('HLS non supportato dal browser');
        }
      } catch(e){
        console.error(e);
        alert('Errore init: ' + e.message);
      }
    }
    init();
  </script>
</body>
</html>
"""

@app.route("/player")
def player():
    return Response(PLAYER_HTML, headers={"Content-Type": "text/html"})

if __name__ == "__main__":
    # Port 8080 is the standard for Fly.io containers
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
