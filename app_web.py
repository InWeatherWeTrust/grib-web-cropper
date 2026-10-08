import sys
import os
import json
import shutil
import urllib.request
import ssl
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

def resolve_ecmwf_ip():
    """Bypasse le bloqueur DNS de Render en demandant l'IP de l'ECMWF directement à Google via HTTPS"""
    try:
        ctx = ssl._create_unverified_context()
        # Requête sécurisée vers le DNS de Google
        dns_url = "https://dns.google"
        req = urllib.request.Request(dns_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            if "Answer" in data and len(data["Answer"]) > 0:
                ip_address = data["Answer"][0]["data"]
                print(f"🎯 IP ECMWF résolue via Google HTTPS : {ip_address}")
                return ip_address
    except Exception as e:
        print(f"⚠️ Échec résolution DNS Google: {e}")
    return None

def get_ecmwf_request(step_val):
    """Construit la requête HTTP propre avec contournement DNS si nécessaire"""
    yesterday = datetime.now() - timedelta(days=1)
    date_str = yesterday.strftime("%Y%m%d")
    step_str = f"{step_val:03d}"
    
    # 1. Tentative avec l'IP brute résolue en amont pour court-circuiter le bug de Render
    ip = resolve_ecmwf_ip()
    if ip:
        url = f"https://{ip}/forecasts/{date_str}/00z/ifs/0p25/oper/{date_str}0000-{step_str}h-oper-sfc.grib2"
        req = urllib.request.Request(url)
        # On injecte l'en-tête Host manuellement pour que le serveur de l'ECMWF accepte la connexion par IP
        req.add_header('Host', 'data.ecmwf.int')
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Connection', 'close')
        return req
        
    # 2. Secours sur l'URL textuelle standard si Google DNS était inaccessible
    url = f"https://ecmwf.int{date_str}/00z/ifs/0p25/oper/{date_str}0000-{step_str}h-oper-sfc.grib2"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Connection': 'close'})
    return req

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/clean', methods=['POST'])
def clean_server_dir():
    try:
        shutil.rmtree(UPLOAD_FOLDER)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        return jsonify({"success": True, "message": "Stockage cloud nettoyé avec succès !"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/scan_ecmwf', methods=['POST'])
def scan_ecmwf():
    data = request.json
    selected_steps = data.get("steps", [])
    if not selected_steps:
        return jsonify({"error": "Veuillez cocher au moins une échéance pour l'analyse."}), 400
        
    steps_ints = sorted([int(s) for s in selected_steps])
    first_step = steps_ints[0]
    
    scan_filename = f"scan_step_{first_step}.grib2"
    scan_path = os.path.join(UPLOAD_FOLDER, scan_filename)
    
    if os.path.exists(scan_path): os.remove(scan_path)
    
    try:
        ctx = ssl._create_unverified_context()
        # On récupère la requête blindée (IP brute + Host)
        req = get_ecmwf_request(first_step)
        
        with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
            with open(scan_path, "wb") as f:
                f.write(response.read())
                
        fin = pygrib.open(scan_path)
        params_set = set()
        levels_set = set()
        
        for grb in fin:
            try:
                name = getattr(grb, 'name', 'Unknown')
                short_name = getattr(grb, 'shortName', '')
                level = getattr(grb, 'level', 0)
                
                if short_name:
                    params_set.add(f"{name} ({short_name})")
                if level is not None:
                    levels_set.add(int(level))
            except Exception:
                continue
        fin.close()
        
        if os.path.exists(scan_path): os.remove(scan_path)
        
        return jsonify({
            "success": True,
            "params": sorted(list(params_set)),
            "levels": sorted(list(levels_set))
        })
        
    except Exception as e:
        if os.path.exists(scan_path): os.remove(scan_path)
        return jsonify({"error": f"Erreur d'accès au catalogue via DoH : {str(e)}"}), 500

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    selected_params_raw = data.get("params", [])
    selected_levels = [int(l) for l in data.get("levels", [])]
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    selected_params = []
    for p in selected_params_raw:
        if "(" in p and ")" in p:
            selected_params.append(p.split("(")[-1].replace(")", "").strip())
        else:
            selected_params.append(p.strip())
            
    total_steps = len(selected_steps)
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    if os.path.exists(output_path): os.remove(output_path)
    
    try:
        ctx = ssl._create_unverified_context()
        
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                req = get_ecmwf_request(step_val)
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, f"temp_step_{step_val}.grib2")
                
                try:
                    with urllib.request.urlopen(req, context=ctx, timeout=35) as response:
                        with open(chunk_raw_path, "wb") as f:
                            f.write(response.read())
                            
                    if not os.path.exists(chunk_raw_path) or os.path.getsize(chunk_raw_path) == 0:
                        continue
                        
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            short_name = getattr(grb, 'shortName', '')
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            
                            param_match = short_name in selected_params if selected_params else True
                            level_match = level in selected_levels if selected_levels else True
                            
                            if param_match and level_match:
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                finally:
                    if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite."}), 400
            
        return jsonify({"success": True, "download_url": f"/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
