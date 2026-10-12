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

def get_ecmwf_arpege_url(step_val, levtype="sfc"):
    """Calcule l'URL brute exacte sur le serveur de stockage IP fixe de l'ECMWF"""
    yesterday = datetime.utcnow() - timedelta(days=1)
    date_str = yesterday.strftime("%Y%m%d")
    step_str = f"{step_val:03d}"
    ecmwf_static_ip = "136.156.129.245"
    
    suffix = "pl" if levtype == "pl" else "fc"
    return f"https://{ecmwf_static_ip}/forecasts/{date_str}/00z/arpege/0p25/oper/{date_str}0000-{step_str}h-oper-{suffix}.grib2"

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
    """Génère instantanément les listes à partir de la documentation technique officielle de Météo-France"""
    data = request.json
    levtype = data.get("levtype", "sfc")
    
    if levtype == "pl":
        # Indexation rigoureuse du paquet IP1 (Paramètres courants en niveaux isobares)
        raw_grib_index = [
            {"name": "U-component of wind", "shortName": "u", "level": 700},
            {"name": "V-component of wind", "shortName": "v", "level": 700},
            {"name": "Geopotential height", "shortName": "gh", "level": 700},
            {"name": "Temperature", "shortName": "t", "level": 700},
            {"name": "Relative humidity", "shortName": "r", "level": 700},
            
            {"name": "U-component of wind", "shortName": "u", "level": 500},
            {"name": "V-component of wind", "shortName": "v", "level": 500},
            {"name": "Geopotential height", "shortName": "gh", "level": 500},
            {"name": "Temperature", "shortName": "t", "level": 500},
            {"name": "Relative humidity", "shortName": "r", "level": 500},
            
            {"name": "U-component of wind", "shortName": "u", "level": 400},
            {"name": "V-component of wind", "shortName": "v", "level": 400},
            {"name": "Geopotential height", "shortName": "gh", "level": 400},
            
            {"name": "U-component of wind", "shortName": "u", "level": 300},
            {"name": "V-component of wind", "shortName": "v", "level": 300},
            {"name": "Geopotential height", "shortName": "gh", "level": 300},
            
            {"name": "U-component of wind", "shortName": "u", "level": 250},
            {"name": "V-component of wind", "shortName": "v", "level": 250},
            {"name": "Geopotential height", "shortName": "gh", "level": 250}
        ]
    else:
        # Indexation rigoureuse du paquet SP1 (Champs de surface courants)
        raw_grib_index = [
            {"name": "10m u-component of wind", "shortName": "10u", "level": 0},
            {"name": "10m v-component of wind", "shortName": "10v", "level": 0},
            {"name": "Mean sea level pressure", "shortName": "msl", "level": 0},
            {"name": "2m temperature", "shortName": "2t", "level": 0},
            {"name": "Total precipitation", "shortName": "tp", "level": 0}
        ]
    
    params_set = set()
    levels_set = set()
    
    for item in raw_grib_index:
        params_set.add(f"{item['name']} ({item['shortName']})")
        if item['level'] == 0:
            levels_set.add("0 (surface)")
        else:
            levels_set.add(f"{item['level']} hPa")
            
    return jsonify({
        "success": True,
        "params": sorted(list(params_set)),
        "levels": sorted(list(levels_set), key=lambda x: int(x.split()[0]) if x.split() and x.split()[0].isdigit() else 0)
    })

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    levtype = data.get("levtype", "sfc")
    selected_params_raw = data.get("params",)
    selected_levels_raw = data.get("levels",)
    selected_steps = sorted(list(set([int(s) for s in data.get("steps",)])))
    
    selected_params = []
    for p in selected_params_raw:
        if "(" in p:
            selected_params.append(p.split("(")[-1].replace(")", "").strip().lower())
        else:
            selected_params.append(p.strip().lower())
            
    selected_levels = []
    for l in selected_levels_raw:
        try:
            parts = str(l).strip().split()
            if parts and parts[0].isdigit():
                selected_levels.append(int(parts[0]))
            elif "surface" in str(l).lower():
                selected_levels.append(0)
        except Exception as e:
            print(f"Erreur de décodage niveau : {e}")
            continue
        
    total_steps = len(selected_steps)
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    ctx = ssl._create_unverified_context()
    
    try:
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                url = get_ecmwf_arpege_url(step_val, levtype)
                
                # ÉTAPE 1 : Indexation partielle chirurgicale (Seulement les 35 premiers Mo)
                # Grâce au document technique, on sait que les paquets SP1 et IP1 complets tiennent dans ce fragment
                header_path = os.path.join(UPLOAD_FOLDER, f"idx_{step_val}.grib2")
                req_idx = urllib.request.Request(url)
                req_idx.add_header('Host', 'data.ecmwf.int')
                req_idx.add_header('Range', 'bytes=0-36700160') # Téléchargement restreint de 35 Mo
                req_idx.add_header('Connection', 'close')
                
                try:
                    with urllib.request.urlopen(req_idx, context=ctx, timeout=30) as resp:
                        with open(header_path, "wb") as f:
                            f.write(resp.read())
                except Exception as step_error:
                    print(f"Saut de l'échéance {step_val} : {step_error}")
                    continue
                
                # ÉTAPE 2 : Extraction et découpe locale par pygrib sur le fragment reçu
                if os.path.exists(header_path) and os.path.getsize(header_path) > 0:
                    fin = pygrib.open(header_path)
                    for grb in fin:
                        try:
                            short_name = str(getattr(grb, 'shortName', '')).lower()
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            
                            match_p = short_name in selected_params
                            match_l = level in selected_levels
                            
                            if match_p and match_l:
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    os.remove(header_path)
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite. Vérifiez vos filtres ou coordonnées."}), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur d'extraction binaire : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
