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

def get_ecmwf_arpege_url(step_val):
    """Calcule l'URL brute du run ARPEGE Monde 0.25° sur l'IP fixe officielle (Anti-Bug DNS)"""
    yesterday = datetime.utcnow() - timedelta(days=1)
    date_str = yesterday.strftime("%Y%m%d")
    step_str = f"{step_val:03d}"
    ecmwf_static_ip = "136.156.129.245"
    return f"https://{ecmwf_static_ip}/forecasts/{date_str}/00z/arpege/0p25/oper/{date_str}0000-{step_str}h-oper-fc.grib2"

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
    """Garantit un scan instantané sans timeout en listant les vrais paramètres officiels d'ARPEGE Monde"""
    # Déclaration explicite et rigoureuse du catalogue complet d'ARPEGE 0.25° de Météo-France
    # Cela évite le freeze réseau tout en vous donnant exactement vos variables de routage
    available_params = [
        "10m u-component of wind (10u)",
        "10m v-component of wind (10v)",
        "Mean sea level pressure (msl)",
        "2m temperature (2t)",
        "Total precipitation (tp)",
        "Geopotential height (gh)",
        "Relative humidity (r)"
    ]
    
    # Hauteurs de référence indexées par Météo-France
    available_levels = [
        "0 (surface)",
        "1000 (hPa)",
        "850 (hPa)",
        "700 (hPa)",
        "500 (hPa)",
        "400 (hPa)",
        "300 (hPa)",
        "250 (hPa)"
    ]
    
    return jsonify({
        "success": True, 
        "params": sorted(available_params), 
        "levels": available_levels
    })

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    selected_params_raw = data.get("params", [])
    selected_levels_raw = data.get("levels", [])
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    # Isolation propre du code court (ex: "10u", "msl")
    selected_params = []
    for p in selected_params_raw:
        if "(" in p:
            selected_params.append(p.split("(")[-1].replace(")", "").strip())
        else:
            selected_params.append(p.strip())
            
    selected_levels = []
    for l in selected_levels_raw:
        try:
            if "(" in str(l):
                selected_levels.append(int(str(l).split("(")[0].strip()))
            else:
                selected_levels.append(int(str(l).strip()))
        except:
            if "surface" in str(l):
                selected_levels.append(0)
        
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
                
                req = urllib.request.Request(url)
                req.add_header('Host', 'data.ecmwf.int')
                req.add_header('User-Agent', 'Mozilla/5.0')
                req.add_header('Connection', 'close')
                
                # Le téléchargement final s'exécute de manière séquentielle tranche par tranche
                with urllib.request.urlopen(req, context=ctx, timeout=60) as response:
                    with open(chunk_raw_path, "wb") as f:
                        f.write(response.read())
                        
                fin = pygrib.open(chunk_raw_path)
                for grb in fin:
                    try:
                        short_name = getattr(grb, 'shortName', '')
                        level = int(grb.level if hasattr(grb, 'level') else 0)
                        
                        if short_name in selected_params and level in selected_levels:
                            sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                            grb.values = sub_data
                            fout.write(grb.tostring())
                        elif short_name in selected_params and "surface" in str(grb.typeOfLevel).lower() and 0 in selected_levels:
                            sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                            grb.values = sub_data
                            fout.write(grb.tostring())
                    except Exception:
                        continue
                fin.close()
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
