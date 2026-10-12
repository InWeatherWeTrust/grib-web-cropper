import sys
import os
import json
import shutil
import urllib.request
import ssl
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')

app = Flask(__name__, template_folder=template_dir)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

UPLOAD_FOLDER = os.path.join(base_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

def check_url_exists(url):
    """Vérifie si une URL par IP répond présente (HEAD request)"""
    ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('Host', 'data.ecmwf.int')
        req.add_header('User-Agent', 'Mozilla/5.0')
        req.add_header('Connection', 'close')
        with urllib.request.urlopen(req, context=ctx, timeout=4):
            return True
    except Exception:
        return False

def get_ecmwf_arpege_url(step_val):
    """Calcule l'URL par IP directe du run 00Z d'aujourd'hui ou d'hier (Zéro DNS)"""
    step_str = f"{step_val:03d}"
    ecmwf_static_ip = "136.156.129.245"
    
    # 1. Tentative sur le run de 00Z d'AUJOURD'HUI
    today_str = datetime.utcnow().strftime("%Y%m%d")
    url_today = f"https://{ecmwf_static_ip}/forecasts/{today_str}/00z/arpege/0p25/oper/{today_str}0000-{step_str}h-oper-fc.grib2"
    
    if check_url_exists(url_today):
        print(f"🎯 Run ARPEGE 00Z d'aujourd'hui trouvé ({today_str})")
        return url_today
        
    # 2. Repli sur le run de 00Z d'HIER
    yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y%m%d")
    print(f"⚠️ Run d'aujourd'hui indisponible, repli sur le 00Z d'hier ({yesterday_str})")
    return f"https://{ecmwf_static_ip}/forecasts/{yesterday_str}/00z/arpege/0p25/oper/{yesterday_str}0000-{step_str}h-oper-fc.grib2"

@app.route('/')
@app.route('/arpege')
def index():
    return render_template('arpege.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/scan_arpege', methods=['POST'])
def scan_arpege():
    data = request.json
    selected_steps = data.get("steps",)
    if not selected_steps:
        return jsonify({"error": "Cochez au moins une échéance."}), 400
        
    steps_ints = sorted([int(s) for s in selected_steps])
    first_step = steps_ints[0]
    
    url = get_ecmwf_arpege_url(first_step)
    scan_filename = f"scan_arp_world_{first_step}.grib2"
    scan_path = os.path.join(UPLOAD_FOLDER, scan_filename)
    
    if os.path.exists(scan_path): os.remove(scan_path)
    
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url)
        req.add_header('Host', 'data.ecmwf.int')
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Connection', 'close')
        
        with urllib.request.urlopen(req, context=ctx, timeout=45) as response:
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
                lev_type = getattr(grb, 'typeOfLevel', 'surface')
                
                if short_name:
                    params_set.add(f"{name} ({short_name})")
                if level is not None:
                    levels_set.add(f"{level} ({lev_type})")
            except Exception:
                continue
        fin.close()
        
        if os.path.exists(scan_path): os.remove(scan_path)
        
        return jsonify({"success": True, "params": sorted(list(params_set)), "levels": sorted(list(levels_set))})
    except Exception as e:
        if os.path.exists(scan_path): os.remove(scan_path)
        return jsonify({"error": f"Erreur de lecture par IP directe ARPEGE : {str(e)}"}), 500

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    selected_params_raw = data.get("params",)
    selected_levels_raw = data.get("levels",)
    selected_steps = sorted(list(set([int(s) for s in data.get("steps",)])))
    
    selected_params = [p.split("(")[-1].replace(")", "").strip() for p in selected_params_raw if "(" in p]
    selected_levels = []
    for l in selected_levels_raw:
        if "(" in str(l):
            selected_levels.append(int(str(l).split("(")[0].strip()))
        else:
            selected_levels.append(int(l))
    
    total_steps = len(selected_steps)
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    try:
        ctx = ssl._create_unverified_context()
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                url = get_ecmwf_arpege_url(step_val)
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, f"temp_arp_{step_val}.grib2")
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                try:
                    req = urllib.request.Request(url)
                    req.add_header('Host', 'data.ecmwf.int')
                    req.add_header('User-Agent', 'Mozilla/5.0')
                    req.add_header('Connection', 'close')
                    
                    with urllib.request.urlopen(req, context=ctx, timeout=45) as response:
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
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
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
